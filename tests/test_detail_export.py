"""详情页控件树直读回归测试：14 项全量解析 + 普通称重兼容 + 导出（不需要真手机）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "desktop"))

from detail_export import (collect_detail_data, detail_data_to_measurement,  # noqa: E402
                           detail_to_measurement, parse_detail,
                           parse_number, pair_index_metrics)
from phone import UiNode, parse_ui_xml  # noqa: E402
from sync_tool.fit import build_weight_fit, parse_weight_scale_fit  # noqa: E402
from sync_tool.garmin_csv import build_garmin_csv  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def bodyfat_nodes():
    return parse_ui_xml((FIXTURES / "phone_detail_bodyfat_real.xml")
                        .read_text(encoding="utf-8"))


def weightonly_nodes():
    return parse_ui_xml((FIXTURES / "phone_detail_weightonly_real.xml")
                        .read_text(encoding="utf-8"))


class ParseNumberTests(unittest.TestCase):
    def test_plain_and_percent(self):
        self.assertEqual(parse_number("86.6"), 86.6)
        self.assertEqual(parse_number("28.1%"), 28.1)
        self.assertEqual(parse_number("3.23"), 3.23)

    def test_thousands_separator_is_not_decimal(self):
        # 基础代谢率真机显示 '1,767'，逗号是千分位，不能变成 1.767
        self.assertEqual(parse_number("1,767"), 1767.0)
        self.assertEqual(parse_number("2,045"), 2045.0)

    def test_units_and_empty(self):
        self.assertEqual(parse_number("35 岁"), 35.0)
        self.assertEqual(parse_number("13.0"), 13.0)
        self.assertIsNone(parse_number(""))
        self.assertIsNone(parse_number("肥胖型"))


class PairIndexTests(unittest.TestCase):
    def test_long_title_pairs_by_left_edge(self):
        # 「内脏脂肪等级」中心点右偏，按中心配不上，必须按左边界配对
        pairs = pair_index_metrics(bodyfat_nodes())
        self.assertEqual(pairs.get("内脏脂肪等级"), "13.0")
        self.assertEqual(pairs.get("BMI"), "27.0")
        self.assertEqual(pairs.get("脂肪率"), "28.1%")
        self.assertEqual(pairs.get("基础代谢率"), "1,767")


class DetailToMeasurementTests(unittest.TestCase):
    def test_full_14_fields(self):
        record = detail_to_measurement(bodyfat_nodes())
        self.assertIsNotNone(record)
        self.assertEqual(record.timestamp.strftime("%Y-%m-%d %H:%M"),
                         "2026-09-24 08:27")
        self.assertEqual(record.weight_kg, 86.6)
        self.assertEqual(record.fat_pct, 28.1)
        self.assertEqual(record.bmi, 27.0)
        self.assertEqual(record.muscle_kg, 33.6)
        self.assertEqual(record.water_pct, 45.7)
        self.assertEqual(record.bone_kg, 3.23)
        self.assertEqual(record.visceral_fat, 13.0)
        self.assertEqual(record.basal_met, 1767.0)
        self.assertEqual(record.protein_pct, 22.5)
        self.assertEqual(record.metabolic_age, 35.0)
        self.assertEqual(record.body_score, 70.0)
        self.assertEqual(record.source, "huawei_detail_controltree")

    def test_weight_only_is_compatible(self):
        record = detail_to_measurement(weightonly_nodes())
        self.assertIsNotNone(record)
        self.assertEqual(record.timestamp.strftime("%Y-%m-%d %H:%M"),
                         "2026-09-25 14:04")
        self.assertEqual(record.weight_kg, 86.2)
        self.assertIsNone(record.fat_pct)
        self.assertIsNone(record.muscle_kg)

    def test_p30_pro_chinese_12_hour_time(self):
        # 华为 P30 Pro / EMUI 10 真机详情页显示「2026/3/28 晚上8:02」。
        record = detail_data_to_measurement({
            "time_text": "2026/3/28 晚上8:02",
            "weight_text": "89.6",
            "score_text": "68",
            "metrics": {"BMI": "27.9", "脂肪率": "30.2%", "基础代谢率": "1,782"},
        })
        self.assertIsNotNone(record)
        self.assertEqual(record.timestamp.strftime("%Y-%m-%d %H:%M"),
                         "2026-03-28 20:02")
        self.assertEqual(record.weight_kg, 89.6)
        self.assertEqual(record.fat_pct, 30.2)
        self.assertEqual(record.basal_met, 1782.0)

    def test_huawei_midnight_detail_time(self):
        # 华为报告里 23 点后的记录显示成「半夜」，例如「2025年5月2日半夜11:08」。
        record = detail_data_to_measurement({
            "time_text": "2025/5/2 半夜11:08",
            "weight_text": "89.9",
            "score_text": "68",
            "metrics": {"BMI": "28.3", "脂肪率": "31.0%", "骨骼肌量": "33.4"},
        })
        self.assertIsNotNone(record)
        self.assertEqual(record.timestamp.strftime("%Y-%m-%d %H:%M"),
                         "2025-05-02 23:08")
        self.assertEqual(record.weight_kg, 89.9)
        self.assertEqual(record.fat_pct, 31.0)
        self.assertEqual(record.muscle_kg, 33.4)

    def test_not_detail_returns_none(self):
        self.assertIsNone(detail_to_measurement([
            UiNode("今日", "", "", (0, 0, 100, 100), False)]))

    def test_missing_weight_returns_none(self):
        nodes = [UiNode("测量记录", "", "action_bar_title", (0, 0, 100, 40), False),
                 UiNode("2026/9/24 08:27", "",
                        "hw_show_health_data_weight_bodyindex_mid_time",
                        (0, 40, 100, 80), False)]
        self.assertIsNone(detail_to_measurement(nodes))

    def test_raw_keeps_non_mapping_fields(self):
        record = detail_to_measurement(bodyfat_nodes())
        self.assertEqual(record.raw.get("去脂体重"), "62.3")
        self.assertEqual(record.raw.get("身体类型"), "肥胖型")


class SplitScreenMergeTests(unittest.TestCase):
    """真机上 14 项指标分几屏渲染，合并后必须还原成一条完整记录。"""

    @staticmethod
    def _split_by_y():
        """把完整详情控件树按 y 切成 3 屏，模拟逐级下滚看到的画面。

        第 1 屏：顶部时间/体重 + 第一行指标（BMI/脂肪率/骨骼肌量）
        第 2 屏：中间指标（内脏脂肪/基础代谢/身体类型...）
        第 3 屏：底部指标（水分率/骨盐量/蛋白质/去脂体重/身体年龄）
        """
        nodes = bodyfat_nodes()
        head_rids = ("hw_show_health_data_weight_bodyindex_mid_time",
                     "level_value_text", "hw_show_health_data_weight_mid_weight_score")
        head = [n for n in nodes if any(n.rid.endswith(r) for r in head_rids)]
        metrics = [n for n in nodes if n.rid.endswith("body_index_recycle_title")
                   or n.rid.endswith("body_index_recycle_value")]
        band1 = [n for n in metrics if n.bounds[1] < 1700]
        band2 = [n for n in metrics if 1700 <= n.bounds[1] < 2000]
        band3 = [n for n in metrics if n.bounds[1] >= 2000]
        return [head + band1, band2, band3]

    def test_single_first_screen_is_incomplete(self):
        """只 dump 第一屏会漏字段 —— 复现用户截图里下半部分为空的成因。"""
        first = self._split_by_y()[0]
        record = detail_data_to_measurement(collect_detail_data([first]))
        self.assertIsNotNone(record)
        self.assertEqual(record.bmi, 27.0)
        self.assertIsNone(record.water_pct, "第一屏不该读到水分率")
        self.assertIsNone(record.basal_met, "第一屏不该读到基础代谢率")

    def test_merged_screens_restore_all_fields(self):
        """三屏合并后，所有指标补齐，和一次性完整 dump 结果一致。"""
        batches = self._split_by_y()
        merged = collect_detail_data(batches)
        record = detail_data_to_measurement(merged)
        full = detail_to_measurement(bodyfat_nodes())
        self.assertIsNotNone(record)
        self.assertEqual(record.weight_kg, full.weight_kg)
        self.assertEqual(record.fat_pct, full.fat_pct)
        self.assertEqual(record.bmi, full.bmi)
        self.assertEqual(record.muscle_kg, full.muscle_kg)
        self.assertEqual(record.water_pct, full.water_pct)
        self.assertEqual(record.bone_kg, full.bone_kg)
        self.assertEqual(record.visceral_fat, full.visceral_fat)
        self.assertEqual(record.basal_met, full.basal_met)
        self.assertEqual(record.protein_pct, full.protein_pct)
        self.assertEqual(record.metabolic_age, full.metabolic_age)
        self.assertEqual(record.body_score, full.body_score)

    def test_merge_keeps_first_screen_time_weight(self):
        """时间/体重只在第一屏出现，后续屏不能把它们冲掉。"""
        batches = self._split_by_y()
        record = detail_data_to_measurement(collect_detail_data(batches))
        self.assertEqual(record.timestamp.strftime("%Y-%m-%d %H:%M"),
                         "2026-09-24 08:27")
        self.assertEqual(record.weight_kg, 86.6)


class ExportTests(unittest.TestCase):
    def test_full_details_export_to_csv_and_fit(self):
        records = [detail_to_measurement(bodyfat_nodes()),
                   detail_to_measurement(weightonly_nodes())]
        csv_text = build_garmin_csv(records, height_m=None)
        self.assertIn("86.6", csv_text)
        self.assertIn("28.1", csv_text)
        fit = build_weight_fit(records)
        parsed = parse_weight_scale_fit(fit)
        self.assertEqual(len(parsed), 2)
        full = next(p for p in parsed if p["weight"] == 86.6)
        self.assertAlmostEqual(full["percent_fat"], 28.1)
        self.assertAlmostEqual(full["percent_hydration"], 45.7)
        self.assertAlmostEqual(full["muscle_mass"], 33.6)
        self.assertAlmostEqual(full["basal_met"], 1767.0)
        light = next(p for p in parsed if p["weight"] == 86.2)
        self.assertNotIn("percent_fat", light)


if __name__ == "__main__":
    unittest.main()
