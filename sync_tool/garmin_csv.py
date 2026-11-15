"""生成 Garmin Connect 网页版「导入数据」接受的 Body CSV。

格式（第一行必须是没有逗号的 Body，第二行才是表头）：
    Body
    Date,Weight,BMI,Fat
    2026-11-15,70.5,22.9,18.3

CSV 通道只支持 体重 / BMI / 体脂率；肌肉量、水分、骨量等需要走 FIT 通道。
"""
from __future__ import annotations

from typing import Any

from .models import Measurement

HEADER = "Body\r\nDate,Weight,BMI,Fat\r\n"


def _fmt(value: float | None, digits: int = 2) -> str:
    if value is None:
        return ""
    return f"{round(float(value), digits):.{digits}f}".rstrip("0").rstrip(".")


def build_garmin_csv(records: list[Measurement], *, height_m: float | None = None,
                     date_format: str = "%Y-%m-%d") -> str:
    lines = [HEADER.rstrip("\r\n")]
    for record in records:
        bmi = record.computed_bmi(height_m)
        lines.append(",".join((
            record.timestamp.strftime(date_format),
            _fmt(record.weight_kg, 2),
            _fmt(bmi, 2),
            _fmt(record.fat_pct, 1),
        )))
    return "\r\n".join(lines) + "\r\n"


def build_standard_csv(records: list[Measurement]) -> str:
    """便于本地留档 / 二次编辑的宽表。"""
    columns = (
        "timestamp", "weight_kg", "fat_pct", "bmi", "water_pct", "muscle_kg",
        "muscle_total_kg", "bone_kg", "visceral_fat", "basal_met", "metabolic_age",
        "heart_rate", "protein_pct", "body_score", "height_cm", "user",
        "is_sub_user", "source",
    )
    lines = [",".join(columns)]
    for record in records:
        item = record.to_dict()
        lines.append(",".join(
            str(item.get(column) if item.get(column) is not None else "")
            for column in columns
        ))
    return "\r\n".join(lines) + "\r\n"


def build_standard_json(records: list[Measurement]) -> str:
    import json
    return json.dumps([record.to_dict() for record in records],
                      ensure_ascii=False, indent=2)


def summary(records: list[Measurement]) -> dict[str, Any]:
    if not records:
        return {"count": 0}
    weights = [r.weight_kg for r in records]
    return {
        "count": len(records),
        "first": records[0].date_key,
        "last": records[-1].date_key,
        "min_weight": round(min(weights), 2),
        "max_weight": round(max(weights), 2),
        "with_fat": sum(1 for r in records if r.fat_pct is not None),
        "with_muscle": sum(1 for r in records if r.muscle_kg is not None),
    }
