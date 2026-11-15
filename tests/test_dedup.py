"""清单去重回归测试：按「测量时间+体重」去除重复采集（不需要真手机/GUI）。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "desktop"))

from main import Entry, dedup_entries, dedup_key  # noqa: E402
from sync_tool.models import Measurement  # noqa: E402


def kv_entry(stamp: str, weight: str, *, fat: str = "", checked: bool = True,
             muscle: str = "") -> Entry:
    """构造一条控件树来源的清单记录。"""
    return Entry(
        None,
        values={"timestamp": stamp, "weight_kg": weight, "fat_pct": fat,
                "skeletal_muscle_kg": muscle},
        checked=checked,
        source="控件树直读",
    )


class DedupTests(unittest.TestCase):

    def test_key_uses_time_and_weight(self) -> None:
        e = kv_entry("2026-08-02 01:33", "85.9")
        self.assertEqual(dedup_key(e), ("2026-08-02 01:33", "85.9"))

    def test_duplicate_same_time_same_weight_removed(self) -> None:
        """截图场景：同时间同体重同脂肪率的两条 → 只留一条。"""
        entries = [
            kv_entry("2026-08-08 19:00", "86", fat="24.3"),
            kv_entry("2026-08-02 01:33", "85.9", fat="28.2"),
            kv_entry("2026-08-02 01:33", "85.9", fat="28.2"),
            kv_entry("2026-08-02 14:39", "85", fat="28.7"),
        ]
        kept, removed = dedup_entries(entries)
        self.assertEqual(removed, 1)
        self.assertEqual(len(kept), 3)
        self.assertEqual([e.values["timestamp"] for e in kept],
                         ["2026-08-08 19:00", "2026-08-02 01:33",
                          "2026-08-02 14:39"])

    def test_keeps_richest_fields(self) -> None:
        """同 key 时保留 14 项全量的那条，而非只有体重的那条。"""
        thin = kv_entry("2026-08-02 01:33", "85.9")  # 无脂肪率
        rich = Measurement(
            timestamp=datetime(2026, 8, 2, 1, 33),
            weight_kg=85.9, fat_pct=28.2, bmi=27.1, water_pct=50.0,
            muscle_kg=30.5, bone_kg=3.0, visceral_fat=11, basal_met=1700,
        )
        rich_entry = Entry.from_measurement(rich)
        kept, removed = dedup_entries([thin, rich_entry])
        self.assertEqual(removed, 1)
        self.assertIs(kept[0], rich_entry)
        self.assertAlmostEqual(kept[0].measurement.fat_pct, 28.2)

    def test_checked_is_union(self) -> None:
        """一条勾选一条未勾选 → 保留勾选，避免丢导出。"""
        a = kv_entry("2026-08-02 01:33", "85.9", checked=False)
        b = kv_entry("2026-08-02 01:33", "85.9", checked=True)
        kept, removed = dedup_entries([a, b])
        self.assertEqual(removed, 1)
        self.assertTrue(kept[0].checked)

    def test_same_time_different_weight_not_removed(self) -> None:
        """真机存在同一分钟两种体重，不能误删。"""
        entries = [
            kv_entry("2026-09-18 10:41", "87.0"),
            kv_entry("2026-09-18 10:41", "89.7"),
        ]
        kept, removed = dedup_entries(entries)
        self.assertEqual(removed, 0)
        self.assertEqual(len(kept), 2)

    def test_missing_time_or_weight_not_deduped(self) -> None:
        """时间或体重缺失的记录原样保留，不会被当成同一条。"""
        entries = [
            kv_entry("", "85.9"),
            kv_entry("", "85.9"),
            kv_entry("2026-08-02 01:33", ""),
        ]
        kept, removed = dedup_entries(entries)
        self.assertEqual(removed, 0)
        self.assertEqual(len(kept), 3)

    def test_no_duplicates_returns_same_order(self) -> None:
        entries = [
            kv_entry("2026-08-08 19:00", "86"),
            kv_entry("2026-08-02 14:39", "85"),
        ]
        kept, removed = dedup_entries(entries)
        self.assertEqual(removed, 0)
        self.assertEqual([e.values["timestamp"] for e in kept],
                         ["2026-08-08 19:00", "2026-08-02 14:39"])


if __name__ == "__main__":
    unittest.main()
