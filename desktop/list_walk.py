"""历史列表的小步重叠翻页器（纯逻辑，两个采集器共用）。

丢数据的根因不是「没去重」，而是「整屏翻页」：一次手势滑过一屏甚至更多，
中间那几行从来没有出现在任何一次 uiautomator dump 里，之后再也没机会读到。

本模块把翻页收敛成三条硬规则：
  1. 每次只移动 1～1.5 行（scroll_distance_px），前后两屏必然有重叠行；
  2. 每次滚动后比对上一屏的行 key，若无重叠 → 自动回退半步并把步长减半；
  3. 连续两屏完全相同才判定到底；第一次不动时先加大一点步长再试。
"""
from __future__ import annotations

import time
from typing import Callable

from phone import PhoneBridge, scroll_distance_px

EventHandler = Callable[[str], None]


class ListWalker:
    """只负责「怎么翻页、什么时候算到底」，不关心记录怎么处理。"""

    def __init__(
        self,
        bridge: PhoneBridge,
        *,
        max_scrolls: int = 200,
        on_event: EventHandler | None = None,
        should_stop: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.bridge = bridge
        self.max_scrolls = max(0, int(max_scrolls))
        self.on_event = on_event
        self.should_stop = should_stop or (lambda: False)
        self.sleep = sleep
        self.scrolls = 0
        self.recoveries = 0
        self.distances: list[int] = []
        self._prev_keys: set[str] | None = None
        self._stall = 0
        self._empty = 0
        self._scale = 1.0
        self._last_distance = 240

    def _emit(self, text: str) -> None:
        if self.on_event:
            self.on_event(text)

    def _pause(self, seconds: float) -> None:
        for _ in range(max(1, int(seconds * 10))):
            if self.should_stop():
                return
            self.sleep(0.1)

    def reset(self) -> None:
        """从详情页返回列表后重新建立重叠基准。"""
        self._prev_keys = None
        self._stall = 0
        self._empty = 0

    def advance(self, rows: list[dict]) -> bool:
        """翻一页；返回 False 表示到底 / 到上限 / 用户停止，应结束循环。"""
        if self.should_stop():
            return False
        if not rows:
            # 控件树偶发为空（页面还在加载）：给两次机会，之后按到底处理
            self._empty += 1
            if self._empty >= 2:
                self._emit("列表里已没有可见记录，结束翻页")
                return False
            self._pause(0.6)
            return True
        self._empty = 0
        keys = {row["key"] for row in rows}
        # 规则 2：滑过头了（两屏没有任何共同行），回退半步补读
        if self._prev_keys and keys and not (keys & self._prev_keys):
            self.recoveries += 1
            self._scale = max(0.25, self._scale / 2)
            self._emit("检测到翻页跨度过大，回退补读中间记录…")
            self.bridge.scroll_by(max(100, self._last_distance // 2), up=True)
            self._prev_keys = None
            self._stall = 0
            self._pause(0.7)
            return True
        # 规则 3：两屏完全相同才算到底
        if self._prev_keys and keys and keys == self._prev_keys:
            self._stall += 1
            if self._stall >= 2:
                self._emit("已翻到列表底部，没有更多记录了")
                return False
        else:
            self._stall = 0
        if self.scrolls >= self.max_scrolls:
            self._emit(f"已达翻页上限 {self.max_scrolls}，停止")
            return False
        _, height = self.bridge.screen_size() or (1080, 2400)
        distance = int(scroll_distance_px(rows, height) * self._scale)
        if self._stall:  # 上一屏没动，稍微加大一点再试，避免误判到底
            distance = int(distance * 1.5)
        distance = max(100, distance)
        self._last_distance = distance
        self.bridge.scroll_down(distance)
        self.distances.append(distance)
        self.scrolls += 1
        self._prev_keys = keys or self._prev_keys
        self._emit(f"小步翻页：下移 {distance}px（约 1 行，第 {self.scrolls} 次）")
        self._pause(0.9)
        return True
