"""快速清单：只读控件树，不点分享、不出长图、不做 OCR。

与「★ 全自动取报告」的分工：
  ┌ 全自动取报告 ── 逐条打开 → 分享 → 保存长图 → 离线 OCR → 14 项全量
  └ 快速清单 ────── 小步翻页 → 直接读列表里的日期/体重/脂肪率/骨骼肌量
                    （含「只有体重」的普通称重），秒级拿到全部历史

全程只用本机 USB 的 uiautomator dump，不联网、不上传、不碰华为账号接口。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from history_export import row_to_measurement, rows_to_measurements  # noqa: E402
from list_walk import ListWalker  # noqa: E402
from phone import (HEALTH_PACKAGES, PhoneBridge, PhoneError,  # noqa: E402
                   collect_readable_rows, detect_page, find_node)
from sync_tool.models import Measurement  # noqa: E402

MAX_STEPS = 1000
EventHandler = Callable[[str, str], None]


class FastCollector:
    """把「所有数据」列表里每一行的结构化数字一次读回来。"""

    def __init__(
        self,
        bridge: PhoneBridge,
        *,
        max_scrolls: int = 200,
        on_event: EventHandler | None = None,
        should_stop: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        seek_top: bool = True,
        on_record: Callable[[Measurement], None] | None = None,
    ) -> None:
        self.bridge = bridge
        self.on_event = on_event
        self.should_stop = should_stop or (lambda: False)
        self.sleep = sleep
        self.seek_top = seek_top
        # 每读到一条就回调一次，界面上表格可以边读边显示，不必等整份历史读完
        self.on_record = on_record
        self.rows: list[dict] = []
        self.records: list[Measurement] = []
        self._logged: set[tuple[str, float]] = set()
        self._screen_h = 2400  # run() 里 attach 之后按真机分辨率刷新
        self._row_state: dict = {}
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

    @property
    def weight_only_count(self) -> int:
        # 以合并后的最终记录为准，避免滚动中产生的重复伪行把计数抬高
        if self.records:
            return sum(1 for r in self.records if r.fat_pct is None)
        return sum(1 for r in rows_to_measurements(self.rows)
                   if r.fat_pct is None)

    def _prepare_rows(self, nodes: list) -> list[dict]:
        """只收「完整行」；分组头滚出屏幕时，用跨屏记住的日期把行补全。

        真机上屏幕边缘会渲染出半行（缺体重或脂肪率那一截），同一次记录
        还可能因为分组头不在本屏而丢日期。若照单全收，就会出现重复伪行、
        把有体脂的记录误标成「只有体重」。小步重叠翻页让每行必有一屏完整。
        """
        return collect_readable_rows(nodes, screen_height=self._screen_h,
                                     state=self._row_state)

    def _screen_keys(self, nodes: list) -> str:
        return "|".join(row["key"] for row in collect_readable_rows(
            nodes, screen_height=self._screen_h, state={}))

    def _seek_to_top(self) -> None:
        """从任意位置先回到列表顶部，保证「读全部历史」不漏掉最新的记录。"""
        if not self.seek_top:
            self._emit("log", "从手机当前停留的位置开始往下读（不回到顶部）…")
            return
        self._emit("log", "先把列表滚回顶部，确保从最新一条开始…")
        last, stable, rounds = "", 0, 0
        for _ in range(40):
            if self.should_stop():
                return
            self.bridge.ensure_foreground(HEALTH_PACKAGES)
            self.bridge.scroll_toward_top()
            self._pause(0.3)
            rounds += 1
            signature = self._screen_keys(self.bridge.ui_nodes())
            # 连续两屏完全一样 = 已经在顶部，再滑也滑不动了
            stable = stable + 1 if signature and signature == last else 0
            if stable >= 2:
                break
            last = signature
        self._emit("log", f"已回到列表顶部（上滑 {rounds} 次），开始逐行读取")

    def _ensure_history(self) -> None:
        nodes = self.bridge.ui_nodes()
        if detect_page(nodes) == "history":
            return
        # 从详情页 / 预览页逐级点页面「返回」回到列表（HyperOS 拦按键注入）
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
            self.bridge.ensure_foreground(HEALTH_PACKAGES)
            self.bridge.tap(*back.center)
            self._pause(1.0)
            nodes = self.bridge.ui_nodes()
        if detect_page(nodes) != "history":
            raise PhoneError(
                "请先打开华为运动健康 →「体重」卡片 →「所有数据」历史列表，"
                "再点「快速清单」。为避免在别的页面乱点，已自动停止。")

    def run(self) -> list[Measurement]:
        self._emit("start", "正在连接手机并读取界面…")
        self.bridge.attach()
        self.bridge.ensure_foreground(HEALTH_PACKAGES)
        self._screen_h = (self.bridge.screen_size() or (1080, 2400))[1]
        self._ensure_history()
        self._seek_to_top()

        seen: set[str] = set()
        for _ in range(MAX_STEPS):
            if self.should_stop():
                break
            self.bridge.ensure_foreground(HEALTH_PACKAGES)
            nodes = self.bridge.ui_nodes()
            if detect_page(nodes) != "history":
                raise PhoneError("翻页时离开了体重历史列表，已停止以免误读。")
            rows = self._prepare_rows(nodes)
            fresh = [row for row in rows if row["key"] not in seen]
            for row in fresh:
                seen.add(row["key"])
                self.rows.append(row)
                record = row_to_measurement(row)
                if record is not None and self.on_record:
                    self.on_record(record)
                marker = (row["stamp"], row["weight_kg"])
                if marker in self._logged:
                    continue  # 滚动中同一条记录的半行版本，只留一条日志
                self._logged.add(marker)
                self._emit("row", f"读取 {row['stamp']} · {row['weight_kg']} kg"
                           + ("（只有体重）" if row.get("bodyfat") is False else ""))
            if not self.walker.advance(rows):
                break

        self.records = rows_to_measurements(self.rows)
        self._emit("done", f"共读到 {len(self.records)} 条记录，"
                   f"其中 {self.weight_only_count} 条只有体重（仍会写入 Garmin）")
        return self.records
