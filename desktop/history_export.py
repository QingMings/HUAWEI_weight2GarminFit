"""控件树直读 → Garmin 记录（纯逻辑，不含 Qt / 不含 adb）。

为什么这条通道比 OCR 快、也比抓云端接口稳：
  uiautomator dump 回来的 XML 里，列表每行本来就是 App 自己渲染的结构化数字
  （日期 / 时间 / 体重 / 脂肪率 / 骨骼肌量），不需要逐条点「分享」、
  不需要生成长图、不需要 OCR、不需要方程验算，也不碰任何网络请求。
  代价是只有列表展示的这 4 项；14 项全量仍走「全自动取报告 + OCR」通道。

「只有体重」的普通称重在这里是天然兼容的：Measurement 只强制要求
timestamp + weight_kg，FIT 的可选字段写 invalid、CSV 的 BMI/Fat 留空即可。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

# 允许从 desktop/ 目录直接运行脚本（不依赖调用方预先把项目根加进 sys.path）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sync_tool.models import Measurement, normalize_time_text, parse_time  # noqa: E402

STAMP_RE = re.compile(
    r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?")

_OPTIONAL = (
    "fat_pct", "bmi", "water_pct", "muscle_kg", "muscle_total_kg", "bone_kg",
    "visceral_fat", "basal_met", "metabolic_age", "heart_rate",
    "protein_pct", "body_score", "height_cm",
)


def normalize_stamp(value: str) -> str:
    """把 '2026/9/25 14:04' / '2026-9-25 14:04' 统一成 '2026-09-25 14:04'。"""
    match = re.match(
        r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})",
        normalize_time_text(value))
    if not match:
        return ""
    y, mo, d, h, mi = (int(part) for part in match.groups())
    return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}"


def row_to_measurement(row: dict) -> Measurement | None:
    """一条列表行（history_rows 的返回值）转成 Measurement。"""
    stamp = normalize_stamp(str(row.get("stamp") or ""))
    if not stamp:
        # 详情页兜底场景可能只有 label 里的自然时间文本
        found = STAMP_RE.search(normalize_time_text(row.get("label") or ""))
        stamp = normalize_stamp(found.group(0)) if found else ""
    timestamp = parse_time(stamp) if stamp else None
    weight = row.get("weight_kg")
    if timestamp is None or not isinstance(weight, (int, float)) or weight <= 0:
        return None
    return Measurement(
        timestamp=timestamp,
        weight_kg=round(float(weight), 3),
        fat_pct=row.get("fat_pct"),
        muscle_kg=row.get("muscle_kg"),
        source="huawei_history_controltree",
        raw={k: v for k, v in row.items()
             if k in ("label", "date", "time", "stamp", "bodyfat")},
    )


def rows_to_measurements(rows: list[dict]) -> list[Measurement]:
    """按「时间 + 体重」合并，并取各屏里更全的字段。

    真机滚动时控件树偶发少渲染半行，同一条记录会出现「只有体重」和
    「带脂肪率」两个版本；若只留先读到的那个，会平白丢掉体成分数据。
    """
    merged: dict[tuple[str, float], Measurement] = {}
    for row in rows:
        measurement = row_to_measurement(row)
        if measurement is None:
            continue
        key = (measurement.timestamp.strftime("%Y-%m-%d %H:%M"),
               measurement.weight_kg)
        current = merged.get(key)
        if current is None:
            merged[key] = measurement
            continue
        for name in ("fat_pct", "muscle_kg", "bmi", "water_pct", "bone_kg",
                     "visceral_fat", "basal_met"):
            value = getattr(measurement, name, None)
            if getattr(current, name, None) is None and value is not None:
                setattr(current, name, value)
    return [merged[key] for key in sorted(merged)]


def richness(record: Measurement) -> int:
    """字段越多越可信：同一时刻既有控件树记录又有 OCR 全量报告时，保留全量。"""
    return sum(1 for name in _OPTIONAL
               if getattr(record, name, None) is not None)


def merge_rich(records: list[Measurement]) -> list[Measurement]:
    """按「秒 + 体重」去重，同一条记录优先保留信息更全的版本。

    不只用秒做 key：真机列表里确实存在同一分钟两次读数但体重不同（9/18
    10:41 的 87.0 与 89.7），只按秒去重会把其中一条真实记录丢掉。
    """
    best: dict[tuple[int, float], Measurement] = {}
    for record in records:
        key = (int(record.timestamp.timestamp()), round(record.weight_kg, 3))
        current = best.get(key)
        if current is None or (richness(record), record.timestamp) >= \
                (richness(current), current.timestamp):
            best[key] = record
    return [best[key] for key in sorted(best)]


def values_for_fields(record: Measurement) -> dict[str, Any]:
    """回填桌面右侧可编辑表格：只填已知的，其余留空。"""
    return {
        "timestamp": record.timestamp.strftime("%Y-%m-%d %H:%M"),
        "weight_kg": f"{record.weight_kg:g}",
        "fat_pct": "" if record.fat_pct is None else f"{record.fat_pct:g}",
        "skeletal_muscle_kg": ("" if record.muscle_kg is None
                               else f"{record.muscle_kg:g}"),
        "bmi": "" if record.bmi is None else f"{record.bmi:g}",
    }
