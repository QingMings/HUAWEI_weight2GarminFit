"""全自动取报告：基于手机控件树的状态机（不含 Qt，便于单测）。

用户只要把华为运动健康停在体重页面，剩下的「逐条打开记录 → 分享 → 保存 →
拉回电脑 → 离线 OCR → 进清单」全部自动完成。

三个页面状态：
  history  历史列表（ExpandableListView，逐行点开）
  detail   测量记录详情（点「分享」）
  share    报告预览（点「保存」，长图落到 /sdcard/Pictures/sporthealth-*.jpg）

「只有体重、没有体脂」的普通称重记录（例如站上去没握电极）没有可分享的
人体成分报告，两层跳过，不报错、不中断：
  1) 列表页：行内没有 child_item_fat / 骨骼肌量 → 直接标记跳过，连页面都不打开；
  2) 详情页：横幅「未测量到脂肪率？」/ 没有「分享」入口 → 点页面「返回」回列表，
     继续下一条。跳过的条数单独统计，不计入「最多 N 条」。
collect_weight_only=True（默认）时，跳过的记录会把控件树读到的「时间 + 体重」
放进 weight_only_rows，由上层转成只有体重的 Garmin 记录，不是真的丢弃。

翻页统一交给 ListWalker：每次只滚 1～1.5 行、前后两屏必须有重叠行，
杜绝「一次滑过一屏、中间几条从来没出现过」造成的永久丢数据。

安全护栏：
  * 每次点击前校验前台包名必须是 com.huawei.health，绝不在别的 App 上乱点；
  * 每个动作后重新读控件树确认页面确实切换了，否则立即停止；
  * 记录行用「日期 + 行内文字」做稳定 key，翻页滚动不会重复处理；
  * 证据不足（半屏行、控件没加载）时不猜测，照常打开详情页再判；
  * 列表到底 / 达到条数上限 / 用户点停止，都会干净结束。
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Callable

from list_walk import ListWalker
from phone import (HEALTH_PACKAGES, NUMBER_RE, PhoneBridge, PhoneError, UiNode,
                   collect_readable_rows, detect_page, diff_new, find_node,
                   is_weight_only_detail, row_in_stable_zone)
from sync_tool.models import normalize_time_text

TIMESTAMP_RE = re.compile(r"20\d{2}[/-]\d{1,2}[/-]\d{1,2}\s+\d{1,2}:\d{2}")
SAVE_TIMEOUT = 14.0
POLL_INTERVAL = 0.8
MAX_STEPS = 2000

EventHandler = Callable[[str, str, Path | None], None]


class AutoCollector:
    """把手机里的一条条体重报告自动保存到电脑。"""

    def __init__(
        self,
        bridge: PhoneBridge,
        *,
        limit: int = 10,
        max_scrolls: int = 200,
        on_event: EventHandler | None = None,
        should_stop: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        collect_weight_only: bool = True,
    ) -> None:
        self.bridge = bridge
        self.limit = max(1, int(limit))
        self.max_scrolls = max(0, int(max_scrolls))
        self.on_event = on_event
        self.should_stop = should_stop or (lambda: False)
        self.sleep = sleep
        # True：普通称重虽然不出报告，但把「时间 + 体重」也带回来，
        # 由上层转成只有体重的 Garmin 记录（CSV/FIT 都允许其它指标为空）。
        self.collect_weight_only = collect_weight_only
        self.weight_only_rows: list[dict] = []
        self.paths: list[Path] = []
        self.saved = 0
        self.skipped = 0
        self._processed_rows: set[str] = set()
        self._skipped_keys: set[str] = set()
        self._processed_times: set[str] = set()
        self._seen_hashes: set[str] = set()
        self._known: set[str] = set()
        self._just_saved = False
        self._row_state: dict = {}
        self.walker = ListWalker(
            bridge, max_scrolls=self.max_scrolls,
            on_event=lambda text: self._emit("log", text),
            should_stop=self.should_stop, sleep=self.sleep)

    # ---------- 基础 ----------
    def _emit(self, kind: str, text: str, path: Path | None = None) -> None:
        if self.on_event:
            self.on_event(kind, text, path)

    def _stopped(self) -> bool:
        return bool(self.should_stop())

    def _pause(self, seconds: float) -> None:
        """分段等待，保证点「停止」时最多 0.1 秒就能响应。"""
        for _ in range(max(1, int(seconds * 10))):
            if self._stopped():
                return
            self.sleep(0.1)

    def _guard(self) -> None:
        self.bridge.ensure_foreground(HEALTH_PACKAGES)

    def _nodes(self) -> list[UiNode]:
        return self.bridge.ui_nodes()

    def _tap_node(self, nodes: list[UiNode], **kwargs) -> bool:
        node = find_node(nodes, **kwargs)
        if not node:
            return False
        self._guard()
        self.bridge.tap(*node.center)
        return True

    def _skip_weight_only(self, label: str, key: str | None = None,
                          row: dict | None = None) -> None:
        """普通称重单独计数并写状态，不计入「最多 N 条」的报告数；同一条只记一次。

        开了 collect_weight_only 时顺手把控件树里的「时间 + 体重」带回去，
        这样这类记录不是被丢掉，而是以「只有体重」的形式进入 Garmin 清单。
        """
        dedupe = key or label
        if dedupe in self._skipped_keys:
            self._emit("log", f"已跳过：{label or '一条测量记录'}，返回列表")
            return
        self._skipped_keys.add(dedupe)
        self.skipped += 1
        if self.collect_weight_only:
            payload = dict(row) if row else {}
            payload.setdefault("label", label)
            if payload.get("weight_kg"):
                self.weight_only_rows.append(payload)
        self._emit("skip", f"跳过：{label or '一条测量记录'}"
                   "（只有体重、没有体脂数据）→ 继续下一条")

    # ---------- 新图检测 ----------
    def _wait_new_images(self, timeout: float = SAVE_TIMEOUT) -> list:
        """点「保存」后等手机把长图写进相册；只取本次新出现的文件。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stopped():
                return []
            try:
                images = self.bridge.scan_images()
                self._known, fresh = diff_new(self._known, images)
            except PhoneError:
                fresh = []
            if fresh:
                preferred = [item for item in fresh
                             if "sporthealth" in item.name.lower()
                             or "health" in item.name.lower()]
                return preferred or fresh
            self._pause(POLL_INTERVAL)
        return []

    def _pull(self, items: list) -> list[Path]:
        """按内容哈希去重：同一条报告被重复保存时绝不重复进清单。"""
        pulled: list[Path] = []
        for item in items:
            dest = self.bridge.pull(item.remote,
                                    self.bridge.unique_dest(item.remote))
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
            if digest in self._seen_hashes:
                self._emit("log", f"报告图 {dest.name} 与已导入的重复，已跳过")
                continue
            self._seen_hashes.add(digest)
            pulled.append(dest)
            self.paths.append(dest)
            self._emit("image", f"已拉取报告图 {dest.name}", dest)
        return pulled

    @staticmethod
    def _detail_row(nodes: list[UiNode], stamp: str) -> dict:
        """从「只有体重」的详情页也能抠出时间 + 体重（控件树直读）。"""
        node = find_node(nodes, rid="level_value_text")
        match = NUMBER_RE.search(node.text) if node else None
        detail_time = re.match(
            r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})\s+(\d{1,2}):(\d{2})",
            normalize_time_text(stamp))
        normalized = (f"{int(detail_time.group(1)):04d}-"
                      f"{int(detail_time.group(2)):02d}-"
                      f"{int(detail_time.group(3)):02d} "
                      f"{int(detail_time.group(4)):02d}:"
                      f"{int(detail_time.group(5)):02d}") if detail_time else ""
        return {
            "stamp": normalized, "weight_kg": float(match.group()) if match else None,
            "fat_pct": None, "muscle_kg": None,
            "label": normalized or stamp or "测量记录",
        }

    def _go_back(self, nodes: list[UiNode]) -> bool:
        """HyperOS 拦掉按键注入，但页面上的「返回」按钮点按是有效的。"""
        back = (find_node(nodes, desc="返回")
                or find_node(nodes, rid="hwappbarpattern_cancel_icon"))
        if not back:
            return False
        self._guard()
        self.bridge.tap(*back.center)
        self._pause(1.0)
        return True

    def _return_to_history(self) -> None:
        """保存后明确回到历史列表，避免停在预览页把同一条重复保存。"""
        for _ in range(4):
            if self._stopped():
                return
            nodes = self._nodes()
            page = detect_page(nodes)
            if page == "history":
                return
            if page not in ("share", "detail"):
                return
            self._emit("log", f"从「{'预览' if page == 'share' else '测量记录'}」返回…")
            if not self._go_back(nodes):
                return

    # ---------- 三个页面 ----------
    def _handle_share(self, nodes: list[UiNode]) -> None:
        if self._just_saved:
            self._just_saved = False
            self._emit("log", "保存已完成，正在返回历史列表…")
            self._return_to_history()
            return
        save = find_node(nodes, rid="share_save_to_local_layout")
        if not save or not find_node(nodes, text="保存"):
            raise PhoneError("当前不是报告预览页（未找到「保存」按钮），已停止以免误点。")
        self._emit("log", f"第 {self.saved + 1}/{self.limit} 条：点「保存」…")
        self._guard()
        self.bridge.tap(*save.center)
        fresh = self._wait_new_images()
        if not fresh:
            self._emit("log", "没有检测到新保存的图片（可能权限被拒），跳过这条")
        else:
            self._pull(fresh)
        self.saved += 1
        self._pause(0.8)
        self._return_to_history()

    def _handle_detail(self, nodes: list[UiNode]) -> None:
        stamp = ""
        for node in nodes:
            match = TIMESTAMP_RE.search(normalize_time_text(node.text))
            if match:
                stamp = match.group(0)
                break
        # 详情页兜底：直接从详情页启动、或列表控件还没加载完时会走到这里。
        # 这类记录根本没有报告预览页，点「分享」只会被「更多选项」吃掉并报错。
        if is_weight_only_detail(nodes):
            self._skip_weight_only(f"记录 {stamp}" if stamp else "当前测量记录",
                                   key=f"detail|{stamp}",
                                   row=self._detail_row(nodes, stamp))
            if stamp:
                self._processed_times.add(stamp)
            self._return_to_history()
            return
        if stamp and stamp in self._processed_times:
            self._emit("log", f"记录 {stamp} 已处理过，返回列表")
            self._go_back(nodes)
            self._return_to_history()
            return
        if stamp:
            self._processed_times.add(stamp)
        self._emit("log", f"打开测量记录 {stamp}，点「分享」…")
        share = (find_node(nodes, desc="分享")
                 or find_node(nodes, rid="hwappbarpattern_menu_icon")
                 or find_node(nodes, desc="更多选项")
                 or find_node(nodes, rid="hwappbarpattern_ok_icon"))
        if not share:
            raise PhoneError("测量记录页找不到「分享」按钮，已停止以免误点。")
        self._guard()
        self.bridge.tap(*share.center)
        self._pause(1.5)
        after = self._nodes()
        if detect_page(after) != "share":
            # 有的版本「分享」在「更多选项」里，再试一次另一个入口
            alternative = (find_node(after, rid="hwappbarpattern_ok_icon")
                           or find_node(after, desc="更多选项"))
            if alternative:
                self._guard()
                self.bridge.tap(*alternative.center)
                self._pause(1.5)
                after = self._nodes()
        if detect_page(after) != "share":
            # 某些版本会先弹「更多选项」菜单，或列表标记拿不到时点进了无体脂记录：
            # 再读一次控件树，确认是普通称重就正常跳过，而不是整轮报错停止。
            if is_weight_only_detail(after):
                self._skip_weight_only(f"记录 {stamp}" if stamp else "当前测量记录",
                                       key=f"detail|{stamp}",
                                       row=self._detail_row(after, stamp))
                self._return_to_history()
                return
            raise PhoneError(
                "点「分享」后没有进入报告预览页。请把手机停在体重相关页面后重试；"
                "为避免误点，已自动停止。（只有体重、没有体脂的记录现在会自动跳过）")

    def _handle_history(self, nodes: list[UiNode]) -> bool:
        """返回 False 表示已经翻到底，主循环应结束。"""
        height = (self.bridge.screen_size() or (1080, 2400))[1]
        rows = collect_readable_rows(nodes, screen_height=height,
                                     state=self._row_state)
        for row in rows:
            if row["key"] in self._processed_rows:
                continue
            self._processed_rows.add(row["key"])
            if (row.get("bodyfat") is False
                    and row_in_stable_zone(row, height)):
                # 只在屏幕中部、控件渲染稳定的行上直接跳过；边缘行交给
                # 详情页的横幅/分享入口做终判，绝不因为半行误丢一份报告
                self._skip_weight_only(row["label"], key=f"row|{row['key']}",
                                       row=row)
                continue
            self._emit("log", f"打开 {row['label']}")
            self._guard()
            self.bridge.tap(*row["center"])
            self._pause(1.2)
            if detect_page(self._nodes()) != "detail":
                raise PhoneError(
                    "点开历史记录后没有进入「测量记录」详情页，已停止以免误点。")
            self.walker.reset()  # 从详情页回来，重新建立重叠基准
            return True
        # 本屏已处理完（含跳过），交给小步重叠翻页器找下一屏
        self._guard()
        return self.walker.advance(rows)

    # ---------- 主循环 ----------
    def run(self) -> list[Path]:
        self._emit("start", "正在连接手机并读取界面…")
        self.bridge.attach()
        self._guard()
        self._known = {item.key for item in self.bridge.scan_images()}
        self._emit("log", f"点击方式：{self.bridge.injection_status()}"
                   if self.bridge.inject_backend != "auto"
                   else "点击引擎：自动检测（自动绕过小米注入限制）")

        nodes = self._nodes()
        page = detect_page(nodes)
        if page == "unknown":
            # 受限自动入口：只点明确写着「所有数据 / 体重记录」的入口，
            # 找不到就停，绝不在主首页上瞎点。
            for label in ("所有数据", "体重记录", "历史记录", "查看全部"):
                node = find_node(self._nodes(), contains=label)
                if node and node.height <= 120:
                    self._emit("log", f"发现「{label}」入口，正在进入…")
                    self._guard()
                    self.bridge.tap(*node.center)
                    self._pause(1.5)
                    if detect_page(self._nodes()) != "unknown":
                        break
            page = detect_page(self._nodes())
        if page == "unknown":
            self._emit("log", "当前不在体重页面，尝试拉起华为运动健康主界面…")
            self.bridge.launch(HEALTH_PACKAGES[0])
            self._pause(2.0)
            self._guard()
            page = detect_page(self._nodes())
        if page in ("detail", "share"):
            # 统一从历史列表开始，保证每条记录都由列表行 key 追踪，不会重复点开
            self._emit("log", "先回到体重历史列表，再从第一条开始逐条导出…")
            self._return_to_history()
            page = detect_page(self._nodes())
        if page == "unknown":
            raise PhoneError(
                "请先进入华为运动健康的体重页面：\n"
                "打开 App → 点「体重」卡片 → 点「所有数据」进入历史列表，\n"
                "（或直接停在任意一条「测量记录」详情页），然后再点「全自动取报告」。\n"
                "为避免在其它页面误点，已自动停止。")

        stopped_by_bottom = False
        for _ in range(MAX_STEPS):
            if self._stopped() or self.saved >= self.limit:
                break
            self._guard()
            nodes = self._nodes()
            page = detect_page(nodes)
            if page == "share":
                self._handle_share(nodes)
            elif page == "detail":
                self._handle_detail(nodes)
            elif page == "history":
                if not self._handle_history(nodes):
                    stopped_by_bottom = True
                    break
            else:
                raise PhoneError(
                    "当前页面不是「历史列表 / 测量记录 / 报告预览」，已停止以免误点。"
                    "请切回华为运动健康的体重页面后重试。")

        reason = ("已到列表底部" if stopped_by_bottom else
                  "用户停止" if self._stopped() else
                  f"达到设定条数 {self.limit}")
        skipped = (f"，跳过 {self.skipped} 条只有体重的普通称重"
                   if self.skipped else "")
        self._emit("done", f"完成（{reason}）：保存 {self.saved} 条，"
                   f"拉取 {len(self.paths)} 张报告图{skipped}")
        return self.paths
