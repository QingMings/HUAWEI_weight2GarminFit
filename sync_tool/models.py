"""统一数据模型：一条体脂秤测量记录。"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any

# 华为运动健康 / 常见导出格式中同义字段名（小写化后匹配）
# 注意：skeletalMusclelMass 是华为导出里真实存在的拼写错误（多一个 l），
# 归一化后是 skeletalmusclelmass，必须显式列出，否则会落到总肌肉量 muscleMass 上。
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "weight_kg": (
        "bodyweight", "body_weight", "weight", "weightkg", "weight_kg",
        "weightinkg", "体重", "weightvalue",
    ),
    "fat_pct": (
        "bodyfatrate", "body_fat_rate", "bodyfat", "body_fat", "fatpct",
        "fat_pct", "fat", "percent_fat", "体脂率", "体脂肪率",
    ),
    "bmi": ("bmi", "bodymassindex", "体质指数"),
    "water_pct": (
        "moisturerate", "moisture_rate", "bodywaterrate", "body_water_rate",
        "waterpct", "water_pct", "percent_hydration", "体水分率", "水分率",
    ),
    # FIT muscle_mass 的口径是「骨骼肌」，与 Garmin Index 秤一致。
    # 优先取华为骨骼肌量，确实没有时才回退到总肌肉量。
    "muscle_kg": (
        "skeletalmusclelmass", "skeletalmusclemass", "skeletal_muscle_mass",
        "skeletalmuscle", "骨骼肌量", "骨骼肌",
        "musclemass", "muscle_mass", "肌肉量", "muscle",
    ),
    # 华为 muscleMass 是总肌肉量（约 56 kg），只用于留档，默认不写入 FIT。
    "muscle_total_kg": ("musclemass", "muscle_mass", "肌肉量", "muscle"),
    "bone_kg": (
        "bonesalt", "bone_salt", "bonemass", "bone_mass",
        "bonemineralcontent", "无机盐", "骨盐量", "骨量",
    ),
    "visceral_fat": (
        "visceralfatlevel", "visceral_fat_level", "visceralfat",
        "visceral_fat_rating", "内脏脂肪等级", "内脏脂肪",
    ),
    "basal_met": (
        "basalmetabolism", "basal_metabolism", "basalmet", "basal_met",
        "bmr", "基础代谢率", "基础代谢",
    ),
    "metabolic_age": ("bodyage", "body_age", "metabolicage", "metabolic_age", "身体年龄"),
    "heart_rate": ("heartrate", "heart_rate", "hr", "心率"),
    "protein_pct": ("proteinrate", "protein_rate", "蛋白质率"),
    "body_score": ("bodyscore", "body_score", "身体得分"),
    "height_cm": ("height", "heightcm", "height_cm", "身高"),
}

# 时间字段名（用于 CSV / 非标准 JSON）
TIME_ALIASES = (
    "timestamp", "starttime", "start_time", "time", "datetime", "date",
    "measurementtime", "measurement_time", "测量时间", "日期",
)


def _norm_key(key: Any) -> str:
    return "".join(ch for ch in str(key).lower() if ch.isalnum() or ch == "_")


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    for suffix in ("kg", "公斤", "斤", "%", "kcal", "岁", "lb", "lbs"):
        if text.lower().endswith(suffix.lower()):
            text = text[: -len(suffix)].strip()
    try:
        return float(text)
    except ValueError:
        return None


def _local_tz():
    return datetime.now().astimezone().tzinfo


def parse_utc_offset(value: Any) -> int | None:
    """解析华为导出的 timeZone，如 '+0800' / '+08:00' / -0500，返回分钟。"""
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", str(value or "").strip())
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    return sign * (int(match.group(2)) * 60 + int(match.group(3)))


def half_up(value: float, scale: float | None = None) -> int:
    """按 FIT 字段分辨率做 half-up 量化。

    Python 的 round() 是银行家舍入（10.5 -> 10），华为内脏脂肪等级会出现 .5，
    FIT 规范与参考实现都要求 .5 进位，故统一用 floor(x + 0.5)。
    JS 侧对应 Math.floor(x + 0.5)，保证跨语言逐字节一致。
    """
    scaled = value * scale if scale is not None else value
    return math.floor(scaled + 0.5)


_DAY_PERIOD_RE = re.compile(
    r"(凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|深夜|半夜)\s*(\d{1,2})(?=\s*[:：])")
# 这些时段的「12 点」是午夜 0 点（华为把 23 点后也叫「半夜」）
_MIDNIGHT_PERIODS = ("凌晨", "清晨", "早上", "上午", "晚上", "深夜", "半夜")
# 这些时段的小时数要加 12 才是 24 小时制
_EVENING_PERIODS = ("中午", "下午", "傍晚", "晚上")


def normalize_time_text(value: Any) -> str:
    """把华为中文 12 小时制（如「晚上8:02」「半夜11:08」）转成 24 小时制。"""
    text = str(value or "").strip()

    def convert(match: re.Match) -> str:
        period, raw_hour = match.groups()
        hour = int(raw_hour)
        if not 1 <= hour <= 12:
            return match.group(0)
        if period in ("半夜", "深夜"):
            # 半夜/深夜横跨 23 点与 0 点：11:08 → 23:08，12:08 → 00:08，
            # 1:08 仍是 01:08，不能像「下午」那样一律加 12。
            if hour == 12:
                hour = 0
            elif hour == 11:
                hour = 23
        elif period in _EVENING_PERIODS and hour < 12:
            hour += 12
        elif period in _MIDNIGHT_PERIODS and hour == 12:
            hour = 0
        return f"{hour:02d}"

    return _DAY_PERIOD_RE.sub(convert, text).replace("：", ":")


def parse_time(value: Any, tz_offset_min: int | None = None) -> datetime | None:
    """兼容 epoch 毫秒 / 秒 / ISO 字符串 / 常见中文日期；一律返回带时区的时间。

    epoch 时间戳是 UTC 绝对值；若源数据给了 timeZone，就用该偏移构成本地墙上时间，
    避免"人在 A 时区导出、在 B 时区转换"导致 CSV 日期和月份分组错位。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=_local_tz())
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = float(value)
        if number > 1e11:  # epoch 毫秒
            number /= 1000.0
        if 946684800 < number < 4102444800:  # 2000-01-01 ~ 2100-01-01
            utc = datetime.fromtimestamp(number, tz=timezone.utc)
            if tz_offset_min is not None:
                return utc.astimezone(timezone(timedelta(minutes=tz_offset_min)))
            return utc.astimezone()  # 按该时刻的本机偏移（夏令时地区也正确）
    text = normalize_time_text(str(value)).replace("年", "-").replace("月", "-")
    # 「2025年5月2日半夜11:08」去掉「日」后日期和时间会粘在一起，
    # 改成空格再清理，保证 strptime 能按「日期 时间」解析。
    text = text.replace("日", " ").replace("/", "-").replace(".", "-").strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d-%m-%Y %H:%M:%S", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=_local_tz())
    return None


@dataclass
class Measurement:
    timestamp: datetime
    weight_kg: float
    fat_pct: float | None = None
    bmi: float | None = None
    water_pct: float | None = None
    muscle_kg: float | None = None
    muscle_total_kg: float | None = None
    bone_kg: float | None = None
    visceral_fat: float | None = None
    basal_met: float | None = None
    metabolic_age: float | None = None
    heart_rate: float | None = None
    protein_pct: float | None = None
    body_score: float | None = None
    height_cm: float | None = None
    tz_offset_min: int | None = None
    user: str = ""
    is_sub_user: bool = False
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def date_key(self) -> str:
        """本地墙上日期（timestamp 已按 timeZone 构造）。"""
        return self.timestamp.strftime("%Y-%m-%d")

    @property
    def month_key(self) -> str:
        return self.timestamp.strftime("%Y-%m")

    def computed_bmi(self, height_m: float | None) -> float | None:
        if self.bmi:
            return self.bmi
        if height_m is None and self.height_cm:
            height_m = self.height_cm / 100 if self.height_cm > 3 else self.height_cm
        if height_m and height_m > 0.5:
            return round(self.weight_kg / (height_m ** 2), 2)
        return None

    def fit_muscle(self, mode: str = "skeletal") -> float | None:
        """按口径返回写入 FIT muscle_mass 的数值。"""
        if mode == "total":
            return self.muscle_total_kg if self.muscle_total_kg is not None else self.muscle_kg
        return self.muscle_kg

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("raw", None)
        data["timestamp"] = self.timestamp.isoformat()
        return data


def from_mapping(data: dict[str, Any], *, jin_to_kg: bool = False,
                 tzinfo: Any = None, tz_offset_min: int | None = None,
                 is_sub_user: bool = False) -> Measurement | None:
    """把任意来源的一条记录映射为 Measurement。"""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        flat[_norm_key(key)] = value

    def pick(field_name: str) -> float | None:
        # 先把标准字段名本身当候选，保证留档 CSV / 手工录入的 muscle_kg 等能回读
        for alias in (field_name, *FIELD_ALIASES[field_name]):
            norm = _norm_key(alias)
            if norm in flat:
                number = _to_float(flat[norm])
                if number is not None:
                    return number
        return None

    weight = pick("weight_kg")
    if weight is None or weight <= 0:
        return None
    if jin_to_kg or weight > 250:  # 明显是「斤」
        weight = round(weight / 2, 3)

    raw_time = None
    for alias in TIME_ALIASES:
        if _norm_key(alias) in flat:
            raw_time = flat[_norm_key(alias)]
            break
    timestamp = parse_time(raw_time, tz_offset_min) or datetime.now().astimezone()
    if tzinfo is not None and timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=tzinfo)

    return Measurement(
        timestamp=timestamp,
        weight_kg=round(weight, 3),
        fat_pct=pick("fat_pct"),
        bmi=pick("bmi"),
        water_pct=pick("water_pct"),
        muscle_kg=pick("muscle_kg"),
        muscle_total_kg=pick("muscle_total_kg"),
        bone_kg=pick("bone_kg"),
        visceral_fat=pick("visceral_fat"),
        basal_met=pick("basal_met"),
        metabolic_age=pick("metabolic_age"),
        heart_rate=pick("heart_rate"),
        protein_pct=pick("protein_pct"),
        body_score=pick("body_score"),
        height_cm=pick("height_cm"),
        tz_offset_min=tz_offset_min,
        user=str(flat.get("user") or flat.get("subuser") or flat.get("userid") or ""),
        is_sub_user=is_sub_user,
        raw=dict(data),
    )


def dedupe_by_second(records: list[Measurement]) -> list[Measurement]:
    """华为导出里同一条读数会出现两次（record type 8 与 10006，同 recordId）。

    按 FIT 时间戳的 1 秒分辨率去重：既合并重复，又保留同一天早晚两次称重。
    """
    by_second: dict[int, Measurement] = {}
    for record in records:
        key = int(record.timestamp.timestamp())
        current = by_second.get(key)
        if current is None or record.timestamp >= current.timestamp:
            by_second[key] = record
    return [by_second[key] for key in sorted(by_second)]


def dedupe_per_day(records: list[Measurement]) -> list[Measurement]:
    """同一天只保留最后一次测量（Garmin 体重曲线通常按天显示）。"""
    latest: dict[str, Measurement] = {}
    for record in records:
        current = latest.get(record.date_key)
        if current is None or record.timestamp >= current.timestamp:
            latest[record.date_key] = record
    return [latest[key] for key in sorted(latest)]
