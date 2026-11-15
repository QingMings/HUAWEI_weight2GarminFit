"""华为体成分报告图片 / OCR 文本解析测试（与 JS 共用同一份基准）。"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from sync_tool.fit import build_weight_fit, parse_weight_scale_fit
from sync_tool.garmin_csv import build_garmin_csv
from sync_tool.models import parse_time
from sync_tool.report import (
    _find_timestamp, _mass_candidates, _solve_masses, analyze, normalize_text,
    parse_report_text, to_measurement,
)

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = json.loads(
    (ROOT / "tests" / "fixtures" / "report_expected.json").read_text(encoding="utf-8")
)


def _read_sample(relative: str) -> str:
    """真实报告样例含个人数据，不随公开仓库分发；缺失时跳过这一组测试。"""
    path = ROOT / relative
    if not path.exists():
        raise unittest.SkipTest(f"本地真实样例不存在，跳过：{relative}")
    return path.read_text(encoding="utf-8")


KV_TEXT = _read_sample(EXPECTED["source"])
OCR_TEXT = _read_sample(EXPECTED["ocr_source"])


class ReportParsingTests(unittest.TestCase):
    def test_normalize_ocr_artifacts(self):
        self.assertEqual(normalize_text("28 ． 2 ％"), "28.2 %")
        self.assertEqual(normalize_text("87 · 3 ，"), "87.3 ,")
        self.assertEqual(normalize_text("内 脏 脂 肪 等 级"), "内脏脂肪等级")

    def test_p30_pro_chinese_12_hour_timestamp(self):
        self.assertEqual(_find_timestamp("2026/3/28 晚上8:02"),
                         "2026-03-28 20:02")
        self.assertEqual(_find_timestamp("2026/3/28 上午12:05"),
                         "2026-03-28 00:05")

    def test_huawei_midnight_timestamp(self):
        # 华为报告里 23 点后的记录显示成「半夜」，例如「2025年5月2日半夜11:08」。
        self.assertEqual(_find_timestamp("2025年5月2日半夜11:08"),
                         "2025-05-02 23:08")
        self.assertEqual(_find_timestamp("2025/5/3 半夜12:08"),
                         "2025-05-03 00:08")
        self.assertEqual(_find_timestamp("2025/5/3 深夜1:08"),
                         "2025-05-03 01:08")
        self.assertEqual(parse_time("2025年5月2日半夜11:08").strftime("%Y-%m-%d %H:%M"),
                         "2025-05-02 23:08")

    def test_mass_scale_recovery(self):
        # OCR 把 19.49 的小数点吃掉变成 1949
        candidates = _mass_candidates({"protein_mass_kg": 1949.0},
                                      "protein_mass_kg", 87.3)
        self.assertIn((19.49, "scaled", 0.3), candidates)

    def test_keyvalue_equation_is_self_consistent(self):
        result = parse_report_text(KV_TEXT)
        self.assertTrue(result.equation_ok)
        self.assertFalse(result.requires_review())
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.timestamp_text, EXPECTED["timestamp_text"])
        self.assertAlmostEqual(result.inferred_height_cm,
                               EXPECTED["inferred_height_cm"], places=1)
        for key, value in EXPECTED["resolved"].items():
            self.assertAlmostEqual(result.resolved[key], value, places=2,
                                   msg=f"字段 {key} 不符")
        self.assertTrue(all(c.status == "ok" for c in result.checks))

    def test_measurement_and_outputs(self):
        result = parse_report_text(KV_TEXT)
        record = to_measurement(result)
        self.assertAlmostEqual(record.weight_kg, 87.3)
        self.assertAlmostEqual(record.muscle_kg, 33.8)
        self.assertAlmostEqual(record.bone_kg, 3.25)
        self.assertEqual(record.source, "huawei_report_image")
        self.assertEqual(
            build_garmin_csv([record], date_format="%Y-%m-%d"), EXPECTED["csv"]
        )
        decoded = parse_weight_scale_fit(build_weight_fit([record]))
        self.assertEqual(len(decoded), 1)
        self.assertAlmostEqual(decoded[0]["weight"], 87.3, places=2)
        self.assertAlmostEqual(decoded[0]["percent_fat"], 28.2, places=2)
        self.assertAlmostEqual(decoded[0]["muscle_mass"], 33.8, places=2)

    def test_metabolic_age_not_written_by_default(self):
        """真实报告身体年龄 34，但 FIT 默认省略该字段（Garmin 会显示 1072935484000）。"""
        record = to_measurement(parse_report_text(KV_TEXT))
        self.assertEqual(record.metabolic_age, 34.0)
        default = parse_weight_scale_fit(build_weight_fit([record]))
        enabled = parse_weight_scale_fit(
            build_weight_fit([record], include_metabolic_age=True))
        self.assertNotIn("metabolic_age", default[0])
        self.assertEqual(enabled[0]["metabolic_age"], 34.0)
        self.assertEqual({k: v for k, v in default[0].items()},
                         {k: v for k, v in enabled[0].items() if k != "metabolic_age"})

    def test_raw_ocr_rejects_false_closure(self):
        """取整后的 OCR 数字能被「凑」成闭合解，但复现不出百分比，必须拒绝。"""
        result = parse_report_text(OCR_TEXT)
        self.assertFalse(result.equation_ok)
        self.assertTrue(result.requires_review())
        self.assertTrue(any("方程校验已停用" in w for w in result.warnings))
        self.assertTrue(any(c.status != "ok" for c in result.checks))
        self.assertAlmostEqual(result.raw["weight_kg"], 87.3)

    def test_false_closure_synthetic(self):
        """更极端：只给一个组分 + 三个百分比，不允许靠反推项伪装成有效解。"""
        raw = {"weight_kg": 87.3, "fat_mass_kg": 24.62,
               "fat_pct": 28.2, "water_pct": 45.0, "protein_pct": 21.0}
        solution = _solve_masses(raw, 87.3)
        self.assertIsNotNone(solution)
        result = analyze(raw, "2026-11-12 08:10")
        self.assertFalse(result.equation_ok, "不应把低精度凑数当成自洽解")

    def test_missing_weight_is_safe(self):
        result = analyze({}, "2026-11-12 08:10")
        self.assertFalse(result.equation_ok)
        self.assertTrue(result.warnings)

    def test_draft_roundtrip_from_good_report(self):
        """草稿必须自带四组分，否则修正后的草稿永远无法闭合方程。"""
        draft = parse_report_text(KV_TEXT).as_keyvalue_text()
        for label in ("水分 = ", "蛋白质 = ", "脂肪量 = ", "骨盐量 = "):
            self.assertIn(label, draft)
        again = parse_report_text(draft)
        self.assertTrue(again.equation_ok)
        self.assertFalse(again.requires_review())
        self.assertAlmostEqual(again.resolved["fat_pct"], 28.2, places=2)

    def test_ocr_draft_is_fixable(self):
        """真实 OCR 草稿 -> 只把四组分照原图改对 -> 应通过校验并可以出文件。"""
        draft = parse_report_text(OCR_TEXT).as_keyvalue_text()
        fixed = {"水分": 39.94, "蛋白质": 19.49, "脂肪量": 24.62, "骨盐量": 3.25}
        for label, value in fixed.items():
            draft, count = re.subn(rf"^{label} = .*$", f"{label} = {value}",
                                   draft, flags=re.MULTILINE)
            self.assertEqual(count, 1, f"草稿缺少 {label} 行")
        again = parse_report_text(draft)
        self.assertTrue(again.equation_ok, "修正四组分后方程应闭合")
        self.assertFalse(again.requires_review(), "可选指标缺失不该阻止生成")

    def test_optional_missing_fields_do_not_block(self):
        """骨骼肌量/身体年龄等可选指标没读到，只是导入后为空，不该卡住流程。"""
        text = ("测量时间 = 2026-11-12 08:10\n体重 = 87.3\n水分 = 39.94\n"
                "蛋白质 = 19.49\n脂肪量 = 24.62\n骨盐量 = 3.25\n"
                "脂肪率 = 28.2\n水分率 = 45.8\nBMI = 27.2\n")
        result = parse_report_text(text)
        self.assertTrue(result.equation_ok)
        self.assertFalse(result.requires_review())
        self.assertTrue(any(c.status == "missing" for c in result.checks))


if __name__ == "__main__":
    unittest.main()
