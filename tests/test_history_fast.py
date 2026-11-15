"""小步重叠翻页 / 控件树直读 / 无体脂兼容 的回归测试（不需要真手机）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from auto_collect import AutoCollector  # noqa: E402
from fast_collect import FastCollector  # noqa: E402
from history_export import merge_rich, rows_to_measurements  # noqa: E402
from list_walk import ListWalker  # noqa: E402
from phone import (Device, PhoneBridge, UiNode, build_monkey_script,  # noqa: E402
                   detect_page, history_rows, monkey_swipe_lines,
                   parse_ui_xml, scroll_distance_px)
from sync_tool.fit import build_weight_fit, parse_weight_scale_fit  # noqa: E402
from sync_tool.garmin_csv import build_garmin_csv  # noqa: E402
from sync_tool.models import Measurement, parse_time  # noqa: E402
from test_phone import FakeAutoBridge, FakeBridge  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROW_H = 208
SCREEN_H = 2772


def real_history_nodes():
    return parse_ui_xml((FIXTURES / "phone_history_real.xml")
                        .read_text(encoding="utf-8"))


class PagedBridge(PhoneBridge):
    """一台会真正翻页的假手机：30 行，一屏放 12 行左右。"""

    def __init__(self, count: int = 30, *, jump_factor: int = 1) -> None:
        super().__init__(adb=Path("fake-adb.exe"))
        self.serial = "FAKEPAGED"
        self.inject_backend = "monkey"
        self.count = count
        self.jump_factor = jump_factor
        self.offset = 0
        # 滚到底时最后一行仍完整可见（真机列表底部有留白）
        self.max_offset = max(1, 300 + (count - 1) * 390 + ROW_H
                              - SCREEN_H + 200)
        self.distances: list[int] = []
        self.scrolls = 0

    def attach(self) -> Device:
        return Device("FAKEPAGED", "device", model="FAKEPAGED")

    def current_package(self) -> str:
        return "com.huawei.health"

    def ensure_foreground(self, packages=None) -> None:
        return None

    def screen_size(self) -> tuple[int, int]:
        return (1280, SCREEN_H)

    def _move(self, pixels: int) -> None:
        self.offset = max(0, min(self.max_offset,
                                 self.offset + pixels * self.jump_factor))
        self.distances.append(pixels)
        self.scrolls += 1

    def scroll_down(self, pixels=None) -> None:
        self._move(int(pixels if pixels is not None else SCREEN_H * 0.12))

    def scroll_by(self, pixels: int, *, up: bool = False, ms: int = 700) -> None:
        self._move(-int(pixels) if up else int(pixels))

    def scroll_toward_top(self, ms: int = 900) -> None:
        self._move(-1000)

    def ui_nodes(self) -> list[UiNode]:
        nodes: list[UiNode] = []
        prefix = "com.huawei.health:id/"
        for index in range(self.count):
            # uiautomator dump 给的是「当前屏幕坐标」，会随滚动向上平移
            top = 300 + index * 390 - self.offset
            if top + ROW_H < -ROW_H or top > SCREEN_H:
                continue
            date = f"2026/10/{1 + index // 3}"
            nodes += [
                # 分组头落在行上方的空隙里，不落进行框，稳定 key 与滚动位置无关
                UiNode(f"{date}，日均 公斤", "", prefix + "health_data_weight_father_ll",
                       (0, top - 156, 1280, top), False),
                UiNode("", "", prefix + "child_item_linear_layout",
                       (208, top, 1202, top + ROW_H), False),
                UiNode(f"{80 + index * 0.1:.1f}", "", prefix + "child_item_weight",
                       (208, top + 60, 314, top + 130), False),
                UiNode("公斤", "", prefix + "child_item_weight_unit",
                       (327, top + 60, 446, top + 130), False),
                UiNode(f"{8 + index % 12:02d}:{index:02d}", "",
                       prefix + "child_item_time",
                       (1030, top + 70, 1150, top + 140), False),
            ]
            if index % 2 == 0:  # 一半有体脂、一半只有体重
                nodes += [
                    UiNode(f"{20 + index * 0.1:.1f}%", "", prefix + "child_item_fat",
                           (280, top + 100, 396, top + 160), False),
                    UiNode("33.6", "", prefix + "child_item_skeletal_muscle",
                           (481, top + 100, 573, top + 160), False),
                ]
        # 真实 dump 里被屏幕上下边缘裁掉的控件不会出现
        return [n for n in nodes
                if n.bounds[3] > 0 and n.bounds[1] < SCREEN_H
                and n.bounds[3] - n.bounds[1] > 0]


class ScrollFixTest(unittest.TestCase):
    def test_scroll_step_is_about_one_row(self) -> None:
        rows = history_rows(real_history_nodes())
        distance = scroll_distance_px(rows, SCREEN_H)
        row_height = min(row["bounds"][3] - row["bounds"][1] for row in rows)
        self.assertLessEqual(distance, row_height * 1.5)
        self.assertLess(distance, SCREEN_H * 0.25)
        self.assertEqual(distance, 312, "真机 208px 行高 → 312px，远小于旧的 1356px")

    def test_swipe_is_slow_drag_not_fling(self) -> None:
        lines = monkey_swipe_lines(640, 2100, 640, 1788,
                                   steps=28, step_wait_ms=25)
        moves = [line for line in lines if "DispatchPointer(0,0,2," in line]
        self.assertEqual(len(moves), 28, "28 个小步而不是 8 步高速甩动")
        self.assertIn("UserWait(25)", lines)
        script = build_monkey_script(lines)
        self.assertIn(f"count= {len(lines)}", script,
                      "脚本 count 覆盖全部事件，不会被截断")

    def test_swipe_honors_duration_on_monkey_backend(self) -> None:
        bridge = FakeBridge(inject_blocked=True)
        bridge.swipe(640, 2100, 640, 1788, 700)
        script = (Path(tempfile.gettempdir()) / "garmin_weight_monkey.mrs") \
            .read_text(encoding="utf-8")
        self.assertGreaterEqual(
            script.count("DispatchPointer(0,0,2,"), 20,
            "700ms 滑动拆成 ≥20 步，避免 HyperOS 上产生惯性大跳")
        self.assertIn("UserWait(", script)

    def test_walker_recovers_when_a_screen_is_skipped(self) -> None:
        bridge = PagedBridge(jump_factor=12)
        walker = ListWalker(bridge, max_scrolls=8,
                            on_event=lambda _text: None,
                            sleep=lambda _seconds: None)
        walker.advance(history_rows(bridge.ui_nodes()))
        walker.advance(history_rows(bridge.ui_nodes()))
        self.assertGreater(walker.recoveries, 0, "两屏无重叠时自动回退补读")
        self.assertLess(walker._scale, 1.0, "并把翻页步长减半")

    def test_walker_bottom_requires_two_identical_screens(self) -> None:
        bridge = PagedBridge()
        walker = ListWalker(bridge, max_scrolls=5,
                            on_event=lambda _text: None,
                            sleep=lambda _seconds: None)
        rows = history_rows(bridge.ui_nodes())
        self.assertTrue(walker.advance(rows))  # 第一次：滚动
        self.assertTrue(walker.advance(rows))  # 第二次：画面没变，先重试
        self.assertFalse(walker.advance(rows))  # 连续两屏一致才判到底


class FastCollectTest(unittest.TestCase):
    def test_every_row_is_read_without_losing_any(self) -> None:
        bridge = PagedBridge(count=30)
        collector = FastCollector(bridge, max_scrolls=200, seek_top=False,
                                  on_event=lambda kind, text: None,
                                  sleep=lambda _seconds: None)
        records = collector.run()
        self.assertEqual(len(records), 30, "30 行一条不少：小步重叠翻页不漏数据")
        self.assertEqual(len(collector.rows), 30)
        self.assertGreater(collector.walker.scrolls, 5)
        self.assertTrue(all(d <= 312 * 1.5 for d in collector.walker.distances),
                        "每次位移都在 1～1.5 行量级")
        self.assertEqual(collector.weight_only_count, 15)
        self.assertEqual(sum(1 for r in records if r.fat_pct is None), 15)

    def test_records_are_streamed_while_reading(self) -> None:
        """快速清单要边读边入表：每读到一条就回调一次，不用等全部读完。"""
        bridge = PagedBridge(count=6)
        streamed = []
        collector = FastCollector(bridge, max_scrolls=50, seek_top=False,
                                  on_event=lambda kind, text: None,
                                  on_record=streamed.append,
                                  sleep=lambda _seconds: None)
        records = collector.run()
        self.assertEqual(len(streamed), len(records),
                         "回调条数与最终记录一致，且顺序读到就发")
        self.assertEqual(len({(r.timestamp, r.weight_kg) for r in streamed}),
                         len(records))

    def test_real_snapshot_becomes_weight_only_garmin_records(self) -> None:
        rows = history_rows(real_history_nodes())
        records = rows_to_measurements(rows)
        self.assertEqual(len(records), 6)
        self.assertEqual([r.weight_kg for r in records],
                         [87.0, 89.7, 87.1, 86.8, 86.6, 86.2])
        self.assertEqual([r.fat_pct for r in records],
                         [None, None, None, 28.4, 28.1, None])
        csv = build_garmin_csv(records)
        self.assertIn("2026-09-25,86.2,,", csv, "只有体重时 BMI/Fat 留空即可")
        self.assertIn("2026-09-24,86.6,,28.1", csv)
        parsed = parse_weight_scale_fit(build_weight_fit(records))
        self.assertEqual(len(parsed), 6, "FIT 里 6 条都能回读")
        self.assertEqual(parsed[0], {"timestamp": parsed[0]["timestamp"],
                                     "weight": 87.0},
                         "无体脂记录只写体重，其余可选字段为 invalid")
        self.assertEqual(parsed[3]["percent_fat"], 28.4)
        self.assertEqual(parsed[3]["muscle_mass"], 33.5)

    def test_merge_rich_prefers_full_report_but_keeps_distinct_weights(self) -> None:
        stamp = parse_time("2026-09-25 14:04")
        thin = Measurement(timestamp=stamp, weight_kg=86.2,
                           source="huawei_history_controltree")
        rich = Measurement(timestamp=stamp, weight_kg=86.2, fat_pct=28.1,
                           muscle_kg=33.6, source="huawei_report_image")
        other = Measurement(timestamp=stamp, weight_kg=89.7,
                            source="huawei_history_controltree")
        merged = merge_rich([thin, other, rich])
        self.assertEqual(len(merged), 2, "同一分钟不同体重的两次读数都保留")
        self.assertEqual([r.weight_kg for r in merged], [86.2, 89.7])
        self.assertEqual(merged[0].fat_pct, 28.1, "同秒同体重时保留信息更全的 OCR 报告")
        self.assertEqual(merge_rich([rich, thin])[0].fat_pct, 28.1,
                         "顺序无关")

    def test_fast_collector_rejects_unknown_page(self) -> None:
        bridge = PagedBridge()
        bridge.ui_nodes = lambda: [UiNode("今日", "", "", (0, 0, 100, 100), False)]
        collector = FastCollector(bridge, seek_top=False,
                                  sleep=lambda _seconds: None)
        with self.assertRaises(Exception) as ctx:
            collector.run()
        self.assertIn("所有数据", str(ctx.exception))

    def test_seek_top_restarts_from_latest_records(self) -> None:
        bridge = PagedBridge(count=30)
        bridge.offset = bridge.max_offset  # 用户停在很旧的位置
        collector = FastCollector(bridge, max_scrolls=0, seek_top=True,
                                  on_event=lambda kind, text: None,
                                  sleep=lambda _seconds: None)
        records = collector.run()
        self.assertEqual(bridge.offset, 0, "开始前自动滚回列表顶部")
        self.assertEqual(records[0].weight_kg, 80.0,
                         "从最新一条开始读，不漏掉顶部记录")

    def test_start_from_current_position_keeps_scroll_offset(self) -> None:
        bridge = PagedBridge(count=30)
        bridge.offset = 1200  # 用户已经往下翻到较早的记录
        collector = FastCollector(bridge, max_scrolls=0, seek_top=False,
                                  on_event=lambda kind, text: None,
                                  sleep=lambda _seconds: None)
        records = collector.run()
        self.assertEqual(bridge.offset, 1200, "不回到顶部，直接从当前位置读")
        self.assertFalse(any(r.weight_kg == 80.0 for r in records),
                         "顶部的最新记录不会被读取")


class WeightOnlyInAutoModeTest(unittest.TestCase):
    def test_auto_mode_keeps_weight_only_rows(self) -> None:
        bridge = FakeAutoBridge(
            weights=("86.2", "86.6", "86.8"),
            bodyfat_flags=(False, True, True))
        collector = AutoCollector(bridge, limit=2, max_scrolls=2,
                                  collect_weight_only=True,
                                  on_event=lambda kind, text, path: None,
                                  sleep=lambda _seconds: None)
        collector.run()
        self.assertEqual(collector.saved, 2)
        self.assertEqual(collector.skipped, 1)
        self.assertEqual(len(collector.weight_only_rows), 1)
        records = rows_to_measurements(collector.weight_only_rows)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].weight_kg, 86.2)
        self.assertIsNone(records[0].fat_pct)
        self.assertEqual(records[0].source, "huawei_history_controltree")

    def test_auto_mode_can_still_just_skip(self) -> None:
        bridge = FakeAutoBridge(
            weights=("86.2", "86.6"), bodyfat_flags=(False, True))
        collector = AutoCollector(bridge, limit=1, max_scrolls=2,
                                  collect_weight_only=False,
                                  on_event=lambda kind, text, path: None,
                                  sleep=lambda _seconds: None)
        collector.run()
        self.assertEqual(collector.skipped, 1)
        self.assertEqual(collector.weight_only_rows, [])


if __name__ == "__main__":
    unittest.main()
