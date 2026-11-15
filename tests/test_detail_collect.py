"""详情页直读采集器回归测试：实时回调 + 当前位置起读（不需要真手机）。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from detail_collect import DetailCollector  # noqa: E402
from phone import UiNode, parse_ui_xml  # noqa: E402
from sync_tool.models import Measurement  # noqa: E402
from test_phone import FakeBridge  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
detail_nodes = parse_ui_xml(
    (FIXTURES / "phone_detail_bodyfat_real.xml").read_text(encoding="utf-8"))

PREFIX = "com.huawei.health:id/"


def history_nodes():
    """两行完整的历史列表，模拟用户当前停留的位置。"""
    nodes: list[UiNode] = []
    for index, weight in enumerate(("80.0", "79.5")):
        top = 300 + index * 300
        nodes += [
            UiNode("2026/11/15，日均 公斤", "", PREFIX + "health_data_weight_father_ll",
                   (0, top - 150, 1080, top), False),
            UiNode("", "", PREFIX + "child_item_linear_layout",
                   (200, top, 1000, top + 200), False),
            UiNode(weight, "", PREFIX + "child_item_weight",
                   (200, top + 50, 320, top + 120), False),
            UiNode("公斤", "", PREFIX + "child_item_weight_unit",
                   (330, top + 50, 450, top + 120), False),
            UiNode(f"0{8 + index}:00", "", PREFIX + "child_item_time",
                   (900, top + 60, 1010, top + 130), False),
            UiNode("20.0%", "", PREFIX + "child_item_fat",
                   (280, top + 100, 400, top + 160), False),
        ]
    return nodes


def make_record(kg: float) -> Measurement:
    return Measurement(timestamp=datetime(2026, 11, 15, 8, 0,
                                          tzinfo=timezone.utc),
                       weight_kg=kg, fat_pct=20.0)


class DetailCollectorRealtimeTest(unittest.TestCase):
    def test_on_record_fires_once_per_record(self) -> None:
        """每读一条就实时回调一条，不再攒到最后一次性发。"""
        bridge = FakeBridge()
        got: list[Measurement] = []
        # 打桩：停在 history、读两条详情、翻一次页后到底
        collector = DetailCollector(
            bridge, seek_top=False,
            on_record=lambda rec: got.append(rec),
            sleep=lambda _seconds: None)
        collector._ensure_history = lambda: None  # type: ignore[method-assign]
        collector._seek_to_top = lambda: None  # type: ignore[method-assign]
        collector._nodes = history_nodes  # type: ignore[method-assign]
        collector._return_to_history = lambda: None  # type: ignore[method-assign]
        record_iter = iter([make_record(80.0), make_record(79.5)])
        collector._read_one = lambda _row: next(record_iter, None)  # type: ignore[method-assign]
        page = {"n": 0}

        def advance(_rows):
            page["n"] += 1
            return page["n"] < 2

        collector.walker.advance = advance  # type: ignore[method-assign]
        collector.run()
        self.assertEqual([r.weight_kg for r in got], [80.0, 79.5],
                         "表格应在采集过程中逐条收到记录")
        self.assertEqual(len(collector.records), 2)

    def test_seek_top_false_does_not_scroll(self) -> None:
        """默认从当前位置起读：seek_top=False 时一屏都不往上滚。"""
        bridge = FakeBridge()
        collector = DetailCollector(bridge, seek_top=False,
                                    sleep=lambda _seconds: None)
        before = len(bridge.commands)
        collector._seek_to_top()
        self.assertEqual(len(bridge.commands), before,
                         "从当前位置起读时不应产生任何回滚指令")

    def test_seek_top_true_scrolls(self) -> None:
        """显式从头开始时确实会往上滚。"""
        bridge = FakeBridge()
        collector = DetailCollector(
            bridge, seek_top=True, sleep=lambda _seconds: None)
        calls = {"n": 0}

        def fake_scroll(*_args, **_kwargs):
            calls["n"] += 1

        bridge.scroll_toward_top = fake_scroll  # type: ignore[method-assign]
        collector._screen_keys = lambda _nodes: "stable"  # type: ignore[method-assign]
        collector._guard = lambda: None  # type: ignore[method-assign]
        collector._seek_to_top()
        self.assertGreater(calls["n"], 0, "从头开始时必须发出上滑指令")

    def test_default_seek_top_is_false(self) -> None:
        collector = DetailCollector(FakeBridge(), sleep=lambda _s: None)
        self.assertFalse(collector.seek_top,
                         "默认应从用户当前停留位置开始，而不是强制回顶")

    def test_read_detail_batches_no_scroll_when_one_screen_full(self) -> None:
        """真机常态：第一屏就收齐 11 项 → 一屏都不滚，直接返回（提速关键）。"""
        bridge = FakeBridge()
        collector = DetailCollector(bridge, sleep=lambda _s: None)
        scrolls = {"n": 0}
        bridge.scroll_down = lambda _pixels=None: scrolls.__setitem__(  # type: ignore[method-assign]
            "n", scrolls["n"] + 1)
        collector._guard = lambda: None  # type: ignore[method-assign]
        collector._nodes = lambda: detail_nodes  # type: ignore[method-assign]
        batches = collector._read_detail_batches(detail_nodes)
        self.assertEqual(scrolls["n"], 0, "首屏已收齐时不应下滚（每次 dump 2.5s）")
        self.assertEqual(len(batches), 1)

    def test_read_detail_batches_scrolls_when_first_screen_partial(self) -> None:
        """兜底：首屏只渲染出 3 项时，下滚补齐后再停。"""
        bridge = FakeBridge()
        collector = DetailCollector(bridge, sleep=lambda _s: None)
        scrolls = {"n": 0}

        def fake_scroll(_pixels=None):
            scrolls["n"] += 1

        bridge.scroll_down = fake_scroll  # type: ignore[method-assign]
        collector._guard = lambda: None  # type: ignore[method-assign]
        # 第一屏模拟只有顶部 3 项；下滚后的每一屏都是完整 11 项
        partial = [n for n in detail_nodes
                   if not ("body_index_recycle" in n.rid and n.bounds[1] >= 1750)]
        collector._nodes = lambda: detail_nodes  # type: ignore[method-assign]
        batches = collector._read_detail_batches(partial)
        self.assertGreater(scrolls["n"], 0, "首屏不全时必须下滚补字段")
        self.assertLessEqual(scrolls["n"], collector._DETAIL_MAX_SCROLLS)
        # 补齐后指标应达到完整值
        self.assertGreaterEqual(
            len(__import__("detail_export").collect_detail_data(batches)["metrics"]),
            collector._DETAIL_ENOUGH)

    def test_return_uses_cached_back_to_save_one_dump(self) -> None:
        """缓存了返回按钮坐标时，先直接点一次再校验，省一次 2.5s dump。"""
        bridge = FakeBridge()
        collector = DetailCollector(bridge, sleep=lambda _s: None)
        pages = iter([detail_nodes, history_nodes()])  # 点后第一次 dump 已回列表
        collector._nodes = lambda: next(pages, history_nodes())  # type: ignore[method-assign]
        collector._guard = lambda: None  # type: ignore[method-assign]
        collector._detail_back = (117, 243)
        taps_before = len([c for c in bridge.commands if "tap" in " ".join(c)])
        collector._return_to_history()
        taps_after = len([c for c in bridge.commands if "tap" in " ".join(c)])
        self.assertGreater(taps_after, taps_before, "应直接用缓存坐标点一次返回")
        self.assertIsNone(collector._detail_back, "用过的缓存应清空")

    def test_read_detail_batches_stops_when_leaving_detail(self) -> None:
        """一旦下滚后离开了详情页，立即停止，避免误采别的页面。"""
        bridge = FakeBridge()
        collector = DetailCollector(bridge, sleep=lambda _s: None)
        collector._guard = lambda: None  # type: ignore[method-assign]
        collector.bridge.scroll_down = lambda *_a, **_k: None  # type: ignore[method-assign]
        # 第一屏是详情，第二屏起变成未知页（滚过头）→ 只收 1 屏
        screens = iter([None, [UiNode("x", "", "", (0, 0, 1, 1), False)]])
        real_nodes = collector._nodes

        def scripted():
            v = next(screens, None)
            return real_nodes() if v is None else v

        collector._nodes = scripted  # type: ignore[method-assign]
        batches = collector._read_detail_batches(real_nodes())
        self.assertEqual(len(batches), 1)


if __name__ == "__main__":
    unittest.main()
