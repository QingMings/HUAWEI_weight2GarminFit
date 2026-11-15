"""详情页控件树直读采集器：逐条点开 → 读 14 项 → 返回 → 下一条。

和另外两条通道的区别：
  * 「⚡ 快速清单」只读列表行，秒级但只有 4 项；
  * 「★ 全自动取报告」分享长图 + OCR，14 项但慢；
  * 本通道点开详情页直接读控件树 —— 14 项、无 OCR 误差、不依赖相册，
    速度介于两者之间，是「全量字段」场景的新首选。

流程完全只走本机 USB 的 uiautomator dump，不联网、不上传、不碰账号接口。
HyperOS 拦按键注入，返回一律点页面「返回」控件；每次点击前校验前台包名。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from detail_export import collect_detail_data, detail_data_to_measurement  # noqa: E402
from history_export import merge_rich  # noqa: E402
from list_walk import ListWalker  # noqa: E402
from phone import (HEALTH_PACKAGES, PhoneBridge, PhoneError, UiNode,  # noqa: E402
                   collect_readable_rows, detect_page, find_node)
from sync_tool.models import Measurement  # noqa: E402

MAX_STEPS = 1200
EventHandler = Callable[[str, str], None]


class DetailCollector:
    def __init__(
        self,
        bridge: PhoneBridge,
        *,
        max_scrolls: int = 200,
        on_event: EventHandler | None = None,
        should_stop: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        seek_top: bool = False,
        on_record: Callable | None = None,
    ) -> None:
        self.bridge = bridge
        self.on_event = on_event
        self.should_stop = should_stop or (lambda: False)
        self.sleep = sleep
        self.seek_top = seek_top
        # 每读到一条就立即回调（GUI 据此实时刷新表格），不再等全部读完
        self.on_record = on_record
        self.records: list[Measurement] = []
        self.detail_count = 0      # 成功从详情页读到全量的条数
        self.weight_only_count = 0  # 只有体重的普通称重
        self._screen_h = 2400
        self._row_state: dict = {}
        # 读完详情时顺手缓存返回按钮坐标，返回时省一次 dump（2.5s）
        self._detail_back: tuple[int, int] | None = None
        self.walker = ListWalker(
            bridge, max_scrolls=max_scrolls,
            on_event=lambda text: self._emit("log", text),
            should_stop=self.should_stop, sleep=self.sleep)

    def _emit(self, kind: str, text: str) -> None:
        if self.on_event:
            self.on_event(kind, text)

    def _pause(self, seconds: float) -> None:
        for _ in range(max(1, int(seconds * 10))):
            if self.should_stop():
                return
            self.sleep(0.1)

    def _guard(self) -> None:
        package = self.bridge.current_package()
        if package not in HEALTH_PACKAGES:
            raise PhoneError(f"前台已切到 {package or '其他应用'}，停止自动操作以免误点。")

    def _nodes(self) -> list[UiNode]:
        return self.bridge.ui_nodes()

    # ---------- 定位到历史列表顶部 ----------
    def _screen_keys(self, nodes: list[UiNode]) -> str:
        return "|".join(row["key"] for row in collect_readable_rows(
            nodes, screen_height=self._screen_h, state={}))

    def _ensure_history(self) -> None:
        nodes = self._nodes()
        if detect_page(nodes) == "history":
            return
        for _ in range(4):
            page = detect_page(nodes)
            if page == "history":
                return
            if page not in ("detail", "share"):
                break
            back = find_node(nodes, desc="返回") or find_node(
                nodes, rid="hwappbarpattern_cancel_icon")
            if not back:
                break
            self._guard()
            self.bridge.tap(*back.center)
            self._pause(1.0)
            nodes = self._nodes()
        if detect_page(nodes) != "history":
            raise PhoneError(
                "请先打开华为运动健康 →「体重」卡片 →「所有数据」历史列表，"
                "再点「详情全量」。为避免在别的页面乱点，已自动停止。")

    def _seek_to_top(self) -> None:
        if not self.seek_top:
            return
        self._emit("log", "先把列表滚回顶部，确保从最新一条开始…")
        last, stable, rounds = "", 0, 0
        for _ in range(40):
            if self.should_stop():
                return
            self._guard()
            self.bridge.scroll_toward_top()
            self._pause(0.3)
            rounds += 1
            signature = self._screen_keys(self._nodes())
            stable = stable + 1 if signature and signature == last else 0
            if stable >= 2:
                break
            last = signature
        self._emit("log", f"已回到列表顶部（上滑 {rounds} 次），开始逐条读详情")

    # ---------- 详情页读取与返回 ----------
    def _return_to_history(self) -> None:
        # 快路径：刚读完详情，返回按钮坐标已知，先直接点一次，省一次 dump。
        # 点完统一走下面的 dump 校验；万一没回到列表，循环还会继续点返回兜底。
        if self._detail_back is not None:
            self._guard()
            self.bridge.tap(*self._detail_back)
            self._detail_back = None
            self._pause(0.6)
        for _ in range(4):
            if self.should_stop():
                return
            nodes = self._nodes()
            page = detect_page(nodes)
            if page == "history":
                return
            if page not in ("detail", "share"):
                return
            back = find_node(nodes, desc="返回") or find_node(
                nodes, rid="hwappbarpattern_cancel_icon")
            if not back:
                return
            self._detail_back = back.center
            self._guard()
            self.bridge.tap(*back.center)
            self._detail_back = None
            self._pause(0.7)
        self.walker.reset()  # 返回列表后重建重叠基准

    # 详情页 14 项指标排成一列（真机 y=1551~2610）远超一屏，控件树只渲染
    # 可视区。刚点开时下面的指标还没进树 —— 必须小步下滚、多次 dump，
    # 按指标名合并，才能把水分率/骨盐量/蛋白质/内脏脂肪/身体年龄等收齐。
    # 真机实测（2026-11-15，屏高 2772）：点开详情后第一次 dump 就能拿到
    # 全部 11~12 项指标（y≤2558，一屏放得下）。所以「一屏收齐就立刻走」是
    # 常态，下滚只在渲染慢/机型差异导致第一屏不全时兜底——把每条详情的
    # dump 次数从固定 4 次降到 1 次（每次 2.5s，省约 7~10s/条）。
    _DETAIL_ENOUGH = 10      # 一屏指标数达到此值即视为收齐，立即返回
    _DETAIL_MAX_SCROLLS = 8
    _DETAIL_STABLE_STOP = 2  # 兜底路径：连续 N 屏指标数不增 → 停

    def _read_detail_batches(self, first: list[UiNode]) -> list[list[UiNode]]:
        """收齐详情页指标。快路径一屏即走；仅当第一屏不全才下滚兜底合并。"""
        batches: list[list[UiNode]] = [first]
        count = len(collect_detail_data(batches)["metrics"])
        if count >= self._DETAIL_ENOUGH:
            return batches  # 一屏已收齐（真机常态），不再多 dump 一次

        # 兜底：第一屏没渲染全（渲染延迟或机型差异）→ 小步下滚，按指标名合并。
        last_count = count
        stable = 0
        for _ in range(self._DETAIL_MAX_SCROLLS):
            if self.should_stop():
                break
            count = len(collect_detail_data(batches)["metrics"])
            stable = stable + 1 if count == last_count else 0
            if stable >= self._DETAIL_STABLE_STOP or count >= self._DETAIL_ENOUGH:
                break  # 连续几屏没新增，或已收齐
            last_count = count
            self._guard()
            self.bridge.scroll_down(360)  # 约一屏指标行高，慢拖不甩飞
            self._pause(0.5)
            nodes = self._nodes()
            if detect_page(nodes) != "detail":
                break  # 滚出了详情页（异常），用已收的屏解析
            batches.append(nodes)
        return batches

    def _read_one(self, row: dict) -> Measurement | None:
        """点开一条历史记录，多次下滚直读详情页全部字段，返回并转成 Measurement。"""
        self._guard()
        self.bridge.tap(*row["center"])
        self._pause(0.9)
        nodes = self._nodes()
        if detect_page(nodes) != "detail":
            self._emit("log", f"{row['stamp'] or row['label']} 未进入详情页，跳过本条")
            self._return_to_history()
            return None
        # 手上有详情页控件树，顺手记下返回按钮坐标，返回时省一次 dump
        back = find_node(nodes, desc="返回") or find_node(
            nodes, rid="hwappbarpattern_cancel_icon")
        self._detail_back = back.center if back else None
        batches = self._read_detail_batches(nodes)
        merged = collect_detail_data(batches)
        record = detail_data_to_measurement(merged)
        if record is None:
            self._emit("log", f"{row['stamp'] or row['label']} 详情页未读到时间/体重，跳过")
        return record

    def run(self) -> list[Measurement]:
        self._emit("start", "正在连接手机并读取界面…")
        self.bridge.attach()
        self.bridge.ensure_foreground(HEALTH_PACKAGES)
        self._screen_h = (self.bridge.screen_size() or (1080, 2400))[1]
        self._ensure_history()
        self._seek_to_top()

        seen: set[str] = set()
        collected: list[Measurement] = []
        for _ in range(MAX_STEPS):
            if self.should_stop():
                break
            self._guard()
            nodes = self._nodes()
            if detect_page(nodes) != "history":
                self._return_to_history()
                nodes = self._nodes()
                if detect_page(nodes) != "history":
                    raise PhoneError("翻页时离开了体重历史列表，已停止以免误读。")
            rows = collect_readable_rows(nodes, screen_height=self._screen_h,
                                         state=self._row_state)
            fresh = [row for row in rows if row["key"] not in seen]
            for row in fresh:
                if self.should_stop():
                    break
                seen.add(row["key"])
                record = self._read_one(row)
                if record is None:
                    self._return_to_history()
                    continue
                if record.fat_pct is None:
                    self.weight_only_count += 1
                    tag = "（只有体重）"
                else:
                    self.detail_count += 1
                    tag = "（14 项全量）"
                self._emit("row", f"读取 {record.timestamp:%Y-%m-%d %H:%M} · "
                           f"{record.weight_kg:g} kg{tag}")
                collected.append(record)
                # 实时回调：GUI 每收到一条就往表格加一行，用户能立刻看到进度
                if self.on_record:
                    self.on_record(record)
                self._return_to_history()
            if self.should_stop():
                break
            if not self.walker.advance(rows):
                break

        self.records = merge_rich(collected)
        self._emit("done", f"详情页直读完成：{len(self.records)} 条记录，"
                   f"其中 {self.detail_count} 条全量 / "
                   f"{self.weight_only_count} 条只有体重")
        return self.records
