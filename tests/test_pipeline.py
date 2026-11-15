"""端到端自测： python -m unittest discover -s tests -v"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sync_tool.fit import build_weight_fit, parse_weight_scale_fit  # noqa: E402
from sync_tool.garmin_csv import build_garmin_csv, summary  # noqa: E402
from sync_tool.huawei import inspect_source, load_path, manual_record, parse_any_text  # noqa: E402
from sync_tool.models import dedupe_by_second, dedupe_per_day, half_up  # noqa: E402

REALISTIC = ROOT / "samples" / "huawei_export_realistic"


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_path(ROOT / "samples" / "huawei_health_sample.json")
        cls.records.sort(key=lambda r: r.timestamp)

    def test_parse_huawei_privacy_export(self):
        self.assertEqual(len(self.records), 3)
        self.assertEqual(self.records[0].weight_kg, 70.8)
        self.assertEqual(self.records[2].fat_pct, 17.9)
        self.assertEqual(self.records[0].muscle_kg, 52.4)
        self.assertEqual(self.records[0].bmi, 23.1)
        self.assertEqual(self.records[0].source, "huawei_privacy_export")
        self.assertEqual(self.records[0].tz_offset_min, 480)

    def test_dedupe_per_day(self):
        extra = manual_record("2026-11-15 21:00", 69.4, fat_pct=17.5)
        merged = dedupe_per_day(self.records + [extra])
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[-1].weight_kg, 69.4)

    def test_jin_conversion(self):
        rows = parse_any_text("timestamp,weight_kg,fat_pct\n2026-11-15 07:30,140,18.0\n",
                              jin_to_kg=True)
        self.assertEqual(rows[0].weight_kg, 70.0)

    def test_bmi_from_height(self):
        record = manual_record("2026-11-15 07:30", 70.0)
        self.assertAlmostEqual(record.computed_bmi(1.75), 22.86, places=2)

    def test_manual_muscle_and_archive_roundtrip(self):
        """标准字段名必须能被回读，否则手工/留档 CSV 的肌肉量会被静默丢弃。"""
        record = manual_record("2026-11-15 07:30", 70.5,
                               fat_pct=18.3, muscle_kg=31.2)
        self.assertEqual(record.muscle_kg, 31.2)
        archive = parse_any_text(
            "timestamp,weight_kg,fat_pct,muscle_kg,bone_kg\n"
            "2026-11-15 07:30,70.5,18.3,31.2,2.9\n")
        self.assertEqual(archive[0].muscle_kg, 31.2)
        self.assertEqual(archive[0].bone_kg, 2.9)

    def test_garmin_csv(self):
        csv_text = build_garmin_csv(self.records, date_format="%Y-%m-%d")
        self.assertEqual(csv_text.splitlines()[0], "Body")
        self.assertEqual(csv_text.splitlines()[1], "Date,Weight,BMI,Fat")
        self.assertIn("2026-11-15,69.9,22.8,17.9", csv_text)
        self.assertIn("15-11-2026", build_garmin_csv(self.records, date_format="%d-%m-%Y"))
        self.assertEqual(summary(self.records)["with_fat"], 3)

    def test_fit_roundtrip(self):
        data = build_weight_fit(self.records, height_m=1.75)
        self.assertEqual(data[8:12], b".FIT")
        decoded = parse_weight_scale_fit(data)
        self.assertEqual(len(decoded), 3)
        self.assertEqual(decoded[2]["weight"], 69.9)
        self.assertEqual(decoded[2]["percent_fat"], 17.9)
        self.assertEqual(decoded[0]["bmi"], 23.1)
        self.assertEqual(decoded[0]["bone_mass"], 2.9)
        self.assertEqual(decoded[0]["visceral_fat_rating"], 8.0)

    def test_metabolic_age_omitted_by_default(self):
        """Garmin Connect 显示回归：34 岁会被渲染成 1072935484000 年。"""
        record = manual_record("2026-11-15 07:30", 70.0, metabolic_age=34)
        self.assertEqual(34 * 31556926000, 1072935484000)
        default = build_weight_fit([record])
        enabled = build_weight_fit([record], include_metabolic_age=True)
        self.assertNotIn("metabolic_age", parse_weight_scale_fit(default)[0])
        self.assertEqual(parse_weight_scale_fit(enabled)[0]["metabolic_age"], 34.0)
        self.assertEqual(len(enabled) - len(default), 4)  # 定义 3 字节 + 数据 1 字节
        self.assertNotEqual(default, enabled)

    def test_fit_crc_detects_tamper(self):
        data = bytearray(build_weight_fit(self.records))
        data[20] ^= 0x01
        with self.assertRaises(ValueError):
            parse_weight_scale_fit(bytes(data))

    def test_cross_language_output(self):
        py_fit = build_weight_fit(self.records)
        js_path = ROOT / "out" / "js_garmin_weight.fit"
        if js_path.exists():
            self.assertEqual(py_fit, js_path.read_bytes(),
                             "浏览器与命令行生成的 FIT 应逐字节一致")
        js_age = ROOT / "out" / "js_garmin_weight_with_age.fit"
        if js_age.exists():
            self.assertEqual(
                build_weight_fit(self.records, include_metabolic_age=True),
                js_age.read_bytes(),
                "显式启用 metabolic_age 时两侧也应逐字节一致")
        js_csv = ROOT / "out" / "js_garmin_import.csv"
        if js_csv.exists():
            self.assertEqual(
                build_garmin_csv(self.records).encode("utf-8"), js_csv.read_bytes())

    def test_upload_dry_run(self):
        from sync_tool.uploader import upload_records
        result = upload_records(self.records[:1], email="x", password="y",
                                dry_run=True, height_m=1.75, progress=lambda _: None)
        self.assertTrue(result[0]["dry_run"])
        self.assertEqual(result[0]["weight"], 70.8)
        self.assertEqual(result[0]["bmi"], 23.1)


class TestRealisticExport(unittest.TestCase):
    """贴近华为隐私副本的真实结构（信息来自 imrahil/huawei-to-garmin-weight 的实证）。"""

    @classmethod
    def setUpClass(cls):
        cls.all_records = load_path(REALISTIC, include_sub_users=True)
        cls.main = load_path(REALISTIC)  # 默认排除家人/访客
        cls.main.sort(key=lambda r: r.timestamp)
        cls.deduped = dedupe_by_second(cls.main)

    def test_raw_and_sub_user_filter(self):
        self.assertEqual(len(self.all_records), 7)   # type 8 ×2 + type 10006 ×5
        self.assertEqual(len(self.main), 5)         # 排除 subUser 与 extendAttribute 各 1 条
        self.assertEqual(sum(1 for r in self.all_records if r.is_sub_user), 2)

    def test_dedupe_keeps_two_readings_same_day(self):
        self.assertEqual(len(self.deduped), 4)      # 合并 1 条重复读数，保留早晚两次
        self.assertEqual([r.weight_kg for r in self.deduped], [72.1, 75.4, 75.1, 74.8])

    def test_skeletal_muscle_alias(self):
        oct_reading = self.deduped[1]
        self.assertEqual(oct_reading.muscle_kg, 31.1)        # skeletalMusclelMass
        self.assertEqual(oct_reading.muscle_total_kg, 56.1)  # muscleMass 仅留档
        self.assertEqual(oct_reading.fit_muscle("total"), 56.1)
        self.assertEqual(oct_reading.height_cm, 175.0)

    def test_timezone_and_timestamp_fallback(self):
        self.assertEqual(self.deduped[1].tz_offset_min, 480)
        self.assertEqual(self.deduped[1].date_key, "2026-10-03")
        self.assertEqual(self.deduped[1].month_key, "2026-10")
        # 这条 sample 里没有 startTime，必须回退到 record.startTime
        self.assertEqual(self.deduped[2].timestamp.strftime("%Y-%m-%d %H:%M"),
                         "2026-10-03 21:10")
        self.assertEqual(self.deduped[0].timestamp.year, 2019)
        self.assertIsNone(self.deduped[0].fat_pct)  # 早期记录只有 bodyWeight

    def test_half_up_rounding(self):
        self.assertEqual(half_up(10.5, 1), 11)      # 银行家舍入会得到 10
        decoded = parse_weight_scale_fit(build_weight_fit(self.deduped))
        self.assertEqual(decoded[1]["visceral_fat_rating"], 11.0)
        self.assertEqual(decoded[1]["muscle_mass"], 31.1)

    def test_inspect_source(self):
        info = inspect_source(REALISTIC)
        self.assertEqual(info["raw_records"], 7)
        self.assertEqual(info["sub_user_skipped"], 2)
        self.assertEqual(info["duplicates"], 1)
        self.assertEqual(info["by_type"], {"8": 2, "10006": 5})
        self.assertIn(("skeletalMusclelMass", 4), info["fields"])

    def test_cross_language_realistic(self):
        """同一份真实格式数据，浏览器与命令行产物必须逐字节一致。"""
        out = ROOT / "out" / "realistic"
        py_fit = build_weight_fit(self.deduped)
        py_csv = build_garmin_csv(self.deduped).encode("utf-8")
        js_fit = out / "js_garmin_weight.fit"
        js_csv = out / "js_garmin_import.csv"
        if js_fit.exists():
            self.assertEqual(py_fit, js_fit.read_bytes())
        if js_csv.exists():
            self.assertEqual(py_csv, js_csv.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
