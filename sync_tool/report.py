"""解析华为运动健康「人体成分分析报告」分享图片（经 OCR 得到的文本）。

为什么可以自动纠错（2026-11-12 真实报告实证）：
报告「人体成分」区块给出四个绝对量，单位 kg：

    水分 + 蛋白质 + 脂肪量 + 骨盐量 == 体重

其余百分比与派生量都由这四个数算出：

    脂肪率   = 脂肪量 / 体重 * 100      水分率   = 水分 / 体重 * 100
    蛋白质率 = 蛋白质 / 体重 * 100      去脂体重 = 水分 + 蛋白质 + 骨盐量
    BMI      = 体重 / 身高^2   ->  身高 = sqrt(体重 / BMI) * 100

OCR 会误读数字（实测 3.25 -> 5.25、34 -> 54、19.49 -> 1949），
但上面这组方程能把它检验出来甚至反推回来。所以本模块定位是
「OCR 抽数 + 方程校验 + 人工确认」，绝不把未经验证的识别结果直接写进 Garmin。
"""
from __future__ import annotations

import base64
import itertools
import math
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Measurement, normalize_time_text, parse_time

# 报告字段名 -> 版面标签（长标签优先，避免「水分」抢走「水分率」）
LABELS: dict[str, tuple[str, ...]] = {
    "body_score": ("身体得分",),
    "weight_kg": ("体重", "测量体重"),
    "bmi": ("BMI", "体质指数"),
    "fat_pct": ("脂肪率", "体脂率"),
    "skeletal_muscle_kg": ("骨骼肌量", "骨骼肌"),
    "visceral_fat": ("内脏脂肪等级", "内脏脂肪"),
    "basal_met": ("基础代谢率", "基础代谢"),
    "water_pct": ("水分率", "体水分率"),
    "bone_kg": ("骨盐量", "骨量"),
    "protein_pct": ("蛋白质率",),
    "lean_mass_kg": ("去脂体重",),
    "metabolic_age": ("身体年龄",),
    "water_mass_kg": ("水分",),
    "protein_mass_kg": ("蛋白质",),
    "fat_mass_kg": ("脂肪量",),
    "bone_mass_kg": ("骨盐量",),
}

MASS_KEYS = ("water_mass_kg", "protein_mass_kg", "fat_mass_kg", "bone_mass_kg")
PERCENT_KEYS = ("fat_pct", "water_pct", "protein_pct")
OUTPUT_KEYS = (
    "weight_kg", "fat_pct", "bmi", "water_pct", "skeletal_muscle_kg",
    "bone_kg", "visceral_fat", "basal_met", "metabolic_age",
    "protein_pct", "body_score", "lean_mass_kg",
)

# 物理合理区间，用于标出明显误读（不自动改写，只提示核对原图）
PLAUSIBLE = {
    "body_score": (1, 100),
    "skeletal_muscle_kg": (10, 60),
    "visceral_fat": (1, 30),
    "basal_met": (500, 4000),
    "metabolic_age": (10, 100),
    "bmi": (12, 60),
}

_TRANS = {ord(a): ord(b) for a, b in zip(
    "０１２３４５６７８９．：％（）〔〕，－",
    "0123456789.:%()(),-",
)}
_NUM = re.compile(r"\d+(?:\.\d+)?")
_DATE = re.compile(
    r"(20\d{2})\s*[-年./]\s*(\d{1,2})\s*[-月./]\s*(\d{1,2})\s*日?\s*"
    r"(\d{1,2})\s*[:：]\s*(\d{2})"
)


def normalize_text(text: str) -> str:
    """全角转半角、数字间的间隔号还原成小数点、去掉汉字间的 OCR 空格。"""
    s = text.translate(_TRANS).replace("\u00a0", " ")
    s = re.sub(r"(?<=\d)\s*[·・]\s*(?=\d)", ".", s)
    s = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", s)  # OCR 把 28.2 拆成 "28 . 2"
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)
    s = s.replace("·", " ").replace("・", " ")
    return normalize_time_text(s)


@dataclass
class OcrLine:
    y: int
    x: int
    text: str


@dataclass
class Check:
    key: str
    label: str
    read: float | None
    expected: float | None
    status: str  # ok | suspicious | derived | missing | check
    note: str = ""


@dataclass
class ReportResult:
    timestamp_text: str | None
    raw: dict[str, float | None]
    resolved: dict[str, float | None] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    inferred_height_cm: float | None = None
    equation_ok: bool = False
    source_kind: str = ""
    # 四组分（kg）的采纳值，写回草稿时要用，否则人工修正后的草稿无法再闭合方程。
    masses: dict[str, float | None] = field(default_factory=dict)

    def requires_review(self) -> bool:
        # 「缺失」只意味着导入后该指标为空，不该阻止生成；
        # 真正需要人工确认的是可疑读数与超出合理范围的读数。
        return any(c.status in ("suspicious", "check") for c in self.checks)

    def summary_lines(self) -> list[str]:
        out = []
        for c in self.checks:
            r = "—" if c.read is None else f"{c.read:g}"
            e = "—" if c.expected is None else f"{c.expected:g}"
            mark = {"ok": "OK", "derived": "推导", "suspicious": "可疑",
                    "missing": "缺失", "check": "请核对"}[c.status]
            out.append(f"{c.label:<12} 读数 {r:>10}   采纳 {e:>10}   [{mark}] {c.note}")
        return out

    @staticmethod
    def _fmt(value: float | None) -> str:
        return "" if value is None else f"{value:g}"

    def as_keyvalue_text(self) -> str:
        """输出可编辑的键值文本；人工修正后重新喂给 report 即可。

        四组分必须写回：它们是方程校验的基准，若草稿里只剩百分比，
        再怎么改也凑不出 >=3 个独立读数，会永远卡在「待复核」。
        """
        lines = ["# 华为体成分报告解析结果：对照原图改掉有误的数值，保存后重新运行即可",
                 "# 下面四行是四组分（kg），是自动验算的基准，务必照原图填写",
                 f"测量时间 = {self.timestamp_text or ''}",
                 f"体重 = {self._fmt(self.raw.get('weight_kg'))}"]
        masses = self.masses or {}
        for key, label in (("water_mass_kg", "水分"),
                           ("protein_mass_kg", "蛋白质"),
                           ("fat_mass_kg", "脂肪量"),
                           ("bone_mass_kg", "骨盐量")):
            value = masses.get(key)
            if value is None and key == "bone_mass_kg":
                value = self.resolved.get("bone_kg")
            lines.append(f"{label} = {self._fmt(value)}")
        lines.append("# 以下为报告上的原始读数，用来交叉核对；留空不影响生成")
        aliases = {
            "fat_pct": "脂肪率", "bmi": "BMI", "water_pct": "水分率",
            "skeletal_muscle_kg": "骨骼肌量", "visceral_fat": "内脏脂肪等级",
            "basal_met": "基础代谢率", "metabolic_age": "身体年龄",
            "protein_pct": "蛋白质率", "body_score": "身体得分",
            "lean_mass_kg": "去脂体重",
        }
        for key, label in aliases.items():
            lines.append(f"{label} = {self._fmt(self.resolved.get(key))}")
        return "\n".join(lines) + "\n"


def _first_number(text: str, label: str = "") -> float | None:
    s = normalize_text(text)
    if label:
        s = s.replace(label, " ")
    m = _NUM.search(s)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def parse_ocr_lines(text: str) -> list[OcrLine]:
    """接受 'YYYY|XXXX|文字'、'YYYY|文字' 或纯文本；统一成带坐标的行。"""
    lines: list[OcrLine] = []
    for index, raw in enumerate(text.splitlines()):
        if not raw.strip():
            continue
        parts = raw.split("|", 2)
        if len(parts) == 3 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
            lines.append(OcrLine(int(parts[0]), int(parts[1]), parts[2]))
        elif len(parts) == 2 and parts[0].strip().isdigit():
            lines.append(OcrLine(int(parts[0]), 0, parts[1]))
        else:
            lines.append(OcrLine(index * 24, 0, raw))
    return sorted(lines, key=lambda line: (line.y, line.x))


def _sections(lines: list[OcrLine]) -> tuple[list[OcrLine], list[OcrLine]]:
    """以「人体成分」「同龄人对比」两个标题把版面切成卡片区 / 四组分区。"""
    norm = [(line, normalize_text(line.text)) for line in lines]
    starts = [line.y for line, text in norm if "人体成分" in text]
    ends = [line.y for line, text in norm if "同龄人对比" in text]
    if not starts:
        return lines, []
    y0 = min(starts)
    y1 = min((y for y in ends if y > y0), default=10 ** 9)
    return ([line for line in lines if line.y < y0],
            [line for line in lines if y0 < line.y < y1])


def _find_value(lines: list[OcrLine], labels: tuple[str, ...]) -> float | None:
    """标签行的数值，或同行右侧 / 正下方最近的一个数字。"""
    for label in sorted(labels, key=len, reverse=True):
        for i, line in enumerate(lines):
            norm = normalize_text(line.text)
            if label not in norm:
                continue
            own = _first_number(norm, label)
            if own is not None:
                return own
            candidates = []
            for other in lines:
                if other is line or _DATE.search(normalize_text(other.text)):
                    continue  # 日期行的年份不是指标值
                dy, dx = other.y - line.y, other.x - line.x
                number = _first_number(other.text)
                if number is None:
                    continue
                if abs(dy) <= 20 and dx > 20:      # 人体成分：图例左、数值右，同一行
                    candidates.append((abs(dy) * 2 + dx / 1000, number))
                elif 0 < dy <= 90 and abs(dx) < 70:  # 卡片：标签在上、数值在下
                    candidates.append((100 + dy + abs(dx) / 100, number))
            if candidates:
                return min(candidates, key=lambda item: item[0])[1]
    return None


def _find_weight(lines: list[OcrLine]) -> float | None:
    """环形仪表的大号体重没有中文标签；取版面最靠前的 20–300 kg 小数。"""
    for line in lines:
        norm = normalize_text(line.text)
        if "去脂" in norm or not re.search(r"\d+\.\d+", norm):
            continue
        value = _first_number(norm)
        if value and 20 <= value <= 300:
            return value
    return None


def _parse_keyvalue(text: str) -> dict[str, float | None]:
    """支持人工修正后的键值文本，也支持英文标准字段名。"""
    values: dict[str, float | None] = {}
    lookup: dict[str, str] = {}
    for key, labels in LABELS.items():
        for label in labels:
            lookup[label] = key
        lookup[key] = key
    for raw in text.splitlines():
        line = normalize_text(raw).strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([^=:]+?)\s*[=:]\s*(.+)$", line)
        if not m:
            continue
        key = m.group(1).strip()
        target = lookup.get(key)
        if target:
            number = _first_number(m.group(2))
            if number is not None:
                values[target] = number
    return values


def _find_timestamp(text: str) -> str | None:
    m = _DATE.search(normalize_text(text))
    if not m:
        return None
    y, mo, d, h, mi = (int(g) for g in m.groups())
    return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}"


def _scale_mass(value: float | None) -> float | None:
    """OCR 常丢小数点（19.49 -> 1949）；按人体成分 kg 量级还原。"""
    if value is None:
        return None
    for divisor in (1, 10, 100, 1000):
        scaled = value / divisor
        if 0.2 <= scaled <= 70:
            return round(scaled, 3)
    return None


def _mass_candidates(raw: dict[str, float | None], key: str,
                     weight: float) -> list[tuple[float, str, float]]:
    """一个组分的所有可能取值：(数值, 来源, 代价)。代价越低越可信。"""
    pct_key = {
        "water_mass_kg": "water_pct",
        "protein_mass_kg": "protein_pct",
        "fat_mass_kg": "fat_pct",
    }.get(key)
    out: list[tuple[float, str, float]] = []
    ocr = raw.get(key)
    if key == "bone_mass_kg" and ocr is None:
        ocr = raw.get("bone_kg")
    if ocr is not None:
        for divisor in (1, 10, 100, 1000):
            value = round(ocr / divisor, 3)
            if 0.2 <= value <= 70:
                out.append((value, "ocr" if divisor == 1 else "scaled",
                            0.0 if divisor == 1 else 0.3))
                break
    if pct_key and raw.get(pct_key) is not None:
        value = round(raw[pct_key] / 100 * weight, 3)
        if 0.2 <= value <= 70:
            out.append((value, "percent", 0.5))
    if key == "bone_mass_kg" and raw.get("bone_kg") is not None:
        value = round(raw["bone_kg"], 3)
        if 0.2 <= value <= 70 and value != ocr:
            out.append((value, "card", 0.2))
    seen: set[float] = set()
    unique = []
    for value, origin, penalty in out:
        if value not in seen:
            seen.add(value)
            unique.append((value, origin, penalty))
    return unique


def _solve_masses(raw: dict[str, float | None], weight: float):
    """在候选中找一组使「四组分和 = 体重」闭合、且改动代价最小的解。

    允许一项缺失并由方程反推（代价最高），这样即便某个数字完全没被 OCR 认出，
    也能靠其余三项把它算回来。找不到闭合解时返回 None，绝不硬凑。
    """
    options = {key: _mass_candidates(raw, key, weight) + [(None, "derived", 1.0)]
               for key in MASS_KEYS}
    best = None
    for combo in itertools.product(*(options[key] for key in MASS_KEYS)):
        values = {key: item[0] for key, item in zip(MASS_KEYS, combo)}
        missing = [key for key, value in values.items() if value is None]
        if len(missing) > 1:
            continue
        fixed = sum(value for value in values.values() if value is not None)
        if missing:
            derived = round(weight - fixed, 3)
            if not (0.2 <= derived <= 70):
                continue
            values[missing[0]] = derived
        error = abs(sum(values.values()) - weight)
        if error > 0.35:
            continue
        penalty = sum(item[2] for item in combo) + len(missing) * 0.5
        score = error * 10 + penalty
        if best is None or score < best[0]:
            best = (score, values,
                    {key: item[1] for key, item in zip(MASS_KEYS, combo)})
    return best


def _solution_consistent(masses: dict[str, float | None],
                         raw: dict[str, float | None], weight: float) -> bool:
    """闭合解还必须能复现报告上的百分比/去脂体重，否则只是数字凑出来的假解。"""
    values = [masses.get(key) for key in MASS_KEYS]
    if any(value is None for value in values):
        return False
    water, protein, fat, bone = values
    derived = {
        "fat_pct": fat / weight * 100,
        "water_pct": water / weight * 100,
        "protein_pct": protein / weight * 100,
        "lean_mass_kg": water + protein + bone,
    }
    compared = 0
    for key, expected in derived.items():
        read = raw.get(key)
        if read is None:
            continue
        compared += 1
        if abs(read - expected) > 0.5:
            return False
    return compared >= 2


def analyze(raw: dict[str, float | None], timestamp_text: str | None,
            source_kind: str = "") -> ReportResult:
    result = ReportResult(timestamp_text=timestamp_text, raw=dict(raw),
                          source_kind=source_kind)
    resolved: dict[str, float | None] = {k: raw.get(k) for k in OUTPUT_KEYS}
    checks: list[Check] = []

    def add(key: str, label: str, read: float | None, expected: float | None,
            status: str, note: str = "") -> None:
        checks.append(Check(key, label, read, expected, status, note))

    weight = raw.get("weight_kg")
    if weight is None:
        result.warnings.append("没有读到体重，无法做方程校验。")
        result.checks = checks
        result.resolved = resolved
        return result

    # ---- 方程：四组分和 = 体重（候选来自 OCR / 量级还原 / 百分比反推） ----
    solution = _solve_masses(raw, weight)
    masses: dict[str, float | None]
    origins: dict[str, str] = {}
    independent = 0
    if solution:
        _, _, origins = solution
        independent = sum(1 for k in MASS_KEYS
                          if origins.get(k) in ("ocr", "scaled", "card"))
    # 只允许 3 个以上「独立读数」的组分撑起方程。若允许任意反推项，方程必然闭合，
    # 会把 OCR 取整后的错数凑成一组看似自洽的假解 —— 这比直接报错更危险。
    consistent = False
    if solution:
        consistent = _solution_consistent(solution[1], raw, weight)
    if solution and independent >= 3 and consistent:
        _, masses, _ = solution
        result.equation_ok = True
    else:
        masses = {k: _scale_mass(raw.get(k)) for k in MASS_KEYS}
        origins = {}
        reason = (f"独立组分 {independent} 项" if independent < 3
                  else "闭合解无法复现报告上的百分比")
        result.warnings.append(
            f"方程校验已停用（{reason}，OCR 数字精度不够）；"
            "请核对自动生成的 report_draft.txt 后重新运行。")

    for key in MASS_KEYS:
        origin = origins.get(key, "missing")
        label = LABELS[key][0] + "(组分)"
        read, value = raw.get(key), masses.get(key)
        if origin == "ocr":
            add(key, label, read, value, "ok", "")
        elif origin == "scaled":
            add(key, label, read, value, "suspicious", "OCR 丢了小数点，按 kg 量级还原")
        elif origin == "percent":
            add(key, label, read, value, "derived", "由对应百分比反推")
        elif origin == "card":
            add(key, label, read, value, "derived", "取卡片区骨盐量读数")
        elif origin == "derived":
            add(key, label, read, value, "derived", "由体重减去其余三项反推，请对照原图确认")
        else:
            add(key, label, read, value, "missing", "未识别")

    if result.equation_ok:
        water, protein, fat, bone = (masses[k] for k in MASS_KEYS)
        derived_map = {
            "fat_pct": round(fat / weight * 100, 2),
            "water_pct": round(water / weight * 100, 2),
            "protein_pct": round(protein / weight * 100, 2),
            "lean_mass_kg": round(water + protein + bone, 2),
            "bone_kg": round(bone, 2),
        }
        for key, expected in derived_map.items():
            read = raw.get(key)
            tolerance = 0.4 if key.endswith("_pct") else 0.4
            if read is None:
                add(key, LABELS[key][0], None, expected, "derived", "由四组分方程算出")
                resolved[key] = expected
            elif abs(read - expected) > tolerance:
                add(key, LABELS[key][0], read, expected, "suspicious",
                    "与四组分方程不符，采纳方程值，请对照原图确认")
                resolved[key] = expected
            else:
                add(key, LABELS[key][0], read, expected, "ok", "方程自洽")
                resolved[key] = round(read, 2)
    else:
        for key in PERCENT_KEYS + ("lean_mass_kg", "bone_kg"):
            value = raw.get(key)
            if value is None:
                add(key, LABELS[key][0], None, None, "missing", "未识别，导入后该指标为空")
            else:
                add(key, LABELS[key][0], value, value, "check",
                    "OCR 读数未经方程验证，请对照原图确认")

    # ---- BMI 与身高互推 ----
    bmi = raw.get("bmi")
    if bmi:
        height = math.sqrt(weight / bmi) * 100
        if 130 <= height <= 220:
            result.inferred_height_cm = round(height, 1)
            add("bmi", "BMI", bmi, round(bmi, 2), "ok",
                f"反推身高约 {height:.0f} cm（App 里登记的身高）")
            resolved["bmi"] = round(bmi, 2)
        else:
            add("bmi", "BMI", bmi, bmi, "suspicious",
                "反推身高超出 130–220 cm，读数可能误识，请核对")
            resolved["bmi"] = bmi
    else:
        add("bmi", "BMI", None, None, "missing", "未识别；可用设置的身高自动计算")

    # ---- 其余指标：只做合理性提示，不自动改写 ----
    plain = {
        "body_score": "身体得分", "skeletal_muscle_kg": "骨骼肌量",
        "visceral_fat": "内脏脂肪等级", "basal_met": "基础代谢率",
        "metabolic_age": "身体年龄",
    }
    for key, label in plain.items():
        value = raw.get(key)
        low, high = PLAUSIBLE[key]
        if value is None:
            add(key, label, None, None, "missing", "OCR 未识别，请对照原图补上")
            resolved[key] = None
        elif not (low <= value <= high):
            add(key, label, value, None, "check", f"超出常见范围 {low}–{high}，请核对原图")
            resolved[key] = None
        else:
            add(key, label, value, value, "ok", "")
            resolved[key] = value

    result.checks = checks
    result.resolved = resolved
    result.masses = dict(masses)
    return result


def parse_report_text(text: str, *, source_kind: str = "") -> ReportResult:
    """解析 OCR 文本 / 键值文本 / 手机系统 OCR 纯文本。"""
    kv = _parse_keyvalue(text)
    lines = parse_ocr_lines(text)
    card, mass = _sections(lines)
    is_kv = len(kv) >= 3
    kind = source_kind or ("keyvalue" if is_kv else "ocr")
    raw: dict[str, float | None] = {}

    for key, labels in LABELS.items():
        if key == "weight_kg":
            continue  # 「去脂体重」也含「体重」，体重必须单独识别
        if key in kv:
            raw[key] = kv[key]
            continue
        # 结构化键值文本里「留空」就是没有这个指标；若再按 OCR 版面去邻近行猜数，
        # 会把上一行的百分比错填到 BMI / 骨骼肌量 / 基础代谢率上（真实踩过）。
        if is_kv:
            continue
        pool = mass if key in MASS_KEYS and mass else card
        raw[key] = _find_value(pool, labels)

    raw["weight_kg"] = kv.get("weight_kg")
    if raw["weight_kg"] is None and not is_kv:
        raw["weight_kg"] = _find_weight(card)
    if raw.get("bone_mass_kg") is None and raw.get("bone_kg") is not None:
        raw["bone_mass_kg"] = raw["bone_kg"]
    if raw.get("bone_kg") is None and kv.get("bone_mass_kg") is not None:
        raw["bone_kg"] = kv["bone_mass_kg"]

    return analyze(raw, _find_timestamp(text) or _find_timestamp_from_kv(text), kind)


def _find_timestamp_from_kv(text: str) -> str | None:
    m = re.search(r"测量时间\s*[=:]\s*(.+)", text)
    return _find_timestamp(m.group(1)) if m else None


def to_measurement(result: ReportResult) -> Measurement:
    r = result.resolved
    timestamp = parse_time(result.timestamp_text) if result.timestamp_text else None
    if timestamp is None:
        raise ValueError("报告里没有可用的测量时间，请在键值文本里补一行「测量时间 = YYYY-MM-DD HH:MM」")
    weight = result.raw.get("weight_kg")
    if not weight:
        raise ValueError("报告里没有可用的体重")
    return Measurement(
        timestamp=timestamp,
        weight_kg=weight,
        fat_pct=r.get("fat_pct"),
        bmi=r.get("bmi"),
        water_pct=r.get("water_pct"),
        muscle_kg=r.get("skeletal_muscle_kg"),
        bone_kg=r.get("bone_kg"),
        visceral_fat=r.get("visceral_fat"),
        basal_met=r.get("basal_met"),
        metabolic_age=r.get("metabolic_age"),
        protein_pct=r.get("protein_pct"),
        body_score=r.get("body_score"),
        height_cm=result.inferred_height_cm,
        source="huawei_report_image",
        raw={"report_checks": [c.__dict__ for c in result.checks]},
    )


def run_ocr(image_path: Path, out_file: Path | None = None,
            script: Path | None = None) -> str:
    """调用 Windows 自带简体中文 OCR；用 EncodedCommand 规避中文路径编码问题。"""
    if shutil.which("powershell.exe") is None:
        raise RuntimeError("未找到 powershell.exe，此功能需要 Windows 10/11")
    script = (script or Path(__file__).resolve().parent.parent / "tools" / "ocr_report.ps1").resolve()
    if not script.exists():
        raise FileNotFoundError(f"OCR 脚本不存在：{script}")
    # WinRT GetFileFromPathAsync 只接受绝对路径；PowerShell 的工作目录也未必是项目目录。
    image_path = Path(image_path).resolve()
    out_file = Path(out_file).resolve() if out_file else Path(str(image_path) + ".ocr.txt")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    command = f'& "{script}" -ImagePath "{image_path}" -OutFile "{out_file}"'
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        capture_output=True, timeout=180,
    )

    def decode(data: bytes) -> str:
        for encoding in ("utf-8", "gbk", "cp936"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    if proc.returncode != 0 or not out_file.exists():
        detail = (decode(proc.stderr) + "\n" + decode(proc.stdout)).strip()
        raise RuntimeError(f"OCR 失败（退出码 {proc.returncode}）：{detail[:800]}")
    return out_file.read_text(encoding="utf-8")


def load_report(path: Path) -> tuple[str, str]:
    """图片走本地 OCR，文本文件直接读。返回 (文本, 来源类型)。"""
    if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
        return run_ocr(path, path.with_suffix(path.suffix + ".ocr.txt")), "image+ocr"
    return path.read_text(encoding="utf-8-sig"), "text"
