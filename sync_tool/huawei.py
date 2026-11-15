"""华为运动健康 / 荣耀体脂秤数据读取。

支持四种输入：
1. 华为隐私中心「获取您的隐私副本」解压后的目录
   （真实路径为 `Health detail data & description/health detail data*.json`）
2. 单个 JSON 文件（华为原始格式，或我们导出的标准 JSON）
3. CSV（华为导出、openScale 导出、huawei-health-to-health-connect 输出等）
4. 命令行手工录入一条记录

真实导出的两个关键事实（来自 imrahil/huawei-to-garmin-weight 的实证）：
  * 每条读数会重复出现两次：record type 8 与 type 10006，同 recordId、同数值；
  * subUser 非空或 extendAttribute != "0" 的读数是访客/家人测的，默认应排除。
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .models import (
    Measurement, from_mapping, parse_time, parse_utc_offset,
)

WEIGHT_BROADCAST_KEY = "WEIGHT_BODYFAT_BROAD"
WEIGHT_BROADCAST_KEYS = ("WEIGHT_BODYFAT_BROAD", "BODYFAT", "WEIGHT")
HUAWEI_DETAIL_DIR = "Health detail data & description"


def _read_text(path: Path) -> str:
    payload = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _walk(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {}
    return value if isinstance(value, (dict, list)) else {}


def _sample_time(sample: dict[str, Any], record: dict[str, Any]) -> Any:
    """时间戳优先级：sample.startTime → sample.endTime → record.startTime → record.endTime。

    sample 里可能完全没有时间字段，此时必须回退到 record，否则会把历史记录
    全部当成「现在」，导进 Garmin 就是一堆同一天的错误数据。
    """
    for value in (sample.get("startTime"), sample.get("endTime"),
                  record.get("startTime"), record.get("endTime")):
        if value not in (None, "", 0):
            return value
    return None


def _is_ext_user(value: Any) -> bool:
    """extendAttribute 非 "0" / 空 表示访客或家庭成员。"""
    return value not in (None, "", "0", 0, "0.0") and str(value).strip() != ""


def _sub_user(value: dict[str, Any], record: dict[str, Any]) -> str:
    explicit = record.get("subUser") or record.get("userId") or value.get("subUser") or ""
    ext = value.get("extendAttribute")
    if _is_ext_user(ext):
        explicit = explicit or str(ext)
    return str(explicit or "")


def _extract_from_huawei_json(blob: Any, *, jin_to_kg: bool,
                              user_filter: str | None = None,
                              include_sub_users: bool = False,
                              stats: dict[str, Any] | None = None) -> list[Measurement]:
    records: list[Measurement] = []
    counter = stats.setdefault("fields", Counter()) if stats is not None else None
    for node in _walk(blob):
        # 标准华为采样结构
        if isinstance(node.get("samplePoints"), list):
            offset = parse_utc_offset(node.get("timeZone"))
            for sample in node["samplePoints"]:
                if not isinstance(sample, dict):
                    continue
                key = str(sample.get("key", "")).upper()
                if key and not any(token in key for token in WEIGHT_BROADCAST_KEYS):
                    continue
                value = _maybe_json(sample.get("value"))
                if not isinstance(value, dict) or not value:
                    continue
                if counter is not None:
                    counter.update(value.keys())
                    stats["raw_records"] += 1
                    stats["by_type"][str(node.get("type"))] += 1
                payload = dict(value)
                payload.setdefault("timestamp", _sample_time(sample, node))
                sub = _sub_user(value, node)
                payload.setdefault("user", sub)
                record = from_mapping(
                    payload, jin_to_kg=jin_to_kg,
                    tz_offset_min=offset, is_sub_user=bool(sub),
                )
                if record:
                    record.source = "huawei_privacy_export"
                    records.append(record)
            continue

        # 已经扁平化的记录（我们自己的标准 JSON / 第三方导出）
        if "samplePoints" not in node and any(
            alias in {str(k).lower() for k in node}
            for alias in ("bodyweight", "body_weight", "weight", "weight_kg", "体重")
        ):
            record = from_mapping(node, jin_to_kg=jin_to_kg)
            if record:
                record.source = node.get("source") or "json"
                records.append(record)

    if user_filter:
        return [r for r in records if r.user == user_filter]
    if not include_sub_users:
        if stats is not None:
            stats["sub_user_skipped"] = sum(1 for r in records if r.is_sub_user)
        return [r for r in records if not r.is_sub_user]
    return records


def parse_json_text(text: str, *, jin_to_kg: bool = False,
                    user_filter: str | None = None,
                    include_sub_users: bool = False,
                    stats: dict[str, Any] | None = None) -> list[Measurement]:
    blob = json.loads(text)
    return _extract_from_huawei_json(
        blob, jin_to_kg=jin_to_kg, user_filter=user_filter,
        include_sub_users=include_sub_users, stats=stats)


def parse_csv_text(text: str, *, jin_to_kg: bool = False) -> list[Measurement]:
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows: list[Measurement] = []
    for row in reader:
        cleaned = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        record = from_mapping(cleaned, jin_to_kg=jin_to_kg)
        if record:
            record.source = "csv"
            rows.append(record)
    return rows


def parse_any_text(text: str, *, jin_to_kg: bool = False,
                   user_filter: str | None = None,
                   include_sub_users: bool = False,
                   stats: dict[str, Any] | None = None) -> list[Measurement]:
    stripped = text.lstrip("\ufeff \r\n\t")
    if stripped.startswith(("{", "[")):
        return parse_json_text(stripped, jin_to_kg=jin_to_kg,
                               user_filter=user_filter,
                               include_sub_users=include_sub_users, stats=stats)
    return parse_csv_text(stripped, jin_to_kg=jin_to_kg)


def iter_source_files(path: Path) -> list[Path]:
    """优先只读体重明细目录，避免把睡眠/心率等其它健康 JSON 也扫进来。"""
    if path.is_file():
        return [path]
    if not path.exists():
        raise FileNotFoundError(f"找不到输入路径：{path}")
    detail = path / HUAWEI_DETAIL_DIR
    if detail.is_dir():
        found = [p for p in detail.iterdir()
                 if p.is_file() and p.suffix.lower() in (".json", ".csv", ".txt")]
        if found:
            return sorted(found)
    files = [p for p in path.rglob("*") if p.is_file()
             and p.suffix.lower() in (".json", ".csv", ".txt")]
    return sorted(files)


def load_path(path: str | Path, *, jin_to_kg: bool = False,
              user_filter: str | None = None,
              include_sub_users: bool = False,
              stats: dict[str, Any] | None = None) -> list[Measurement]:
    """读取文件 / 目录 / 未加密 ZIP，返回归一化测量记录。"""
    target = Path(path).expanduser()
    if target.is_file() and target.suffix.lower() == ".zip":
        # 华为隐私副本 ZIP 通常带密码，请用 7-Zip / WinRAR 解压后指向目录
        with zipfile.ZipFile(target) as archive:
            collected: list[Measurement] = []
            for name in archive.namelist():
                if name.lower().endswith((".json", ".csv", ".txt")):
                    text = archive.read(name).decode("utf-8-sig", errors="replace")
                    collected.extend(parse_any_text(
                        text, jin_to_kg=jin_to_kg, user_filter=user_filter,
                        include_sub_users=include_sub_users, stats=stats))
            return collected

    records: list[Measurement] = []
    for file in iter_source_files(target):
        text = _read_text(file)
        if not text.strip():
            continue
        try:
            if file.suffix.lower() in (".csv", ".txt"):
                records.extend(parse_csv_text(text, jin_to_kg=jin_to_kg))
            else:
                records.extend(parse_json_text(
                    text, jin_to_kg=jin_to_kg, user_filter=user_filter,
                    include_sub_users=include_sub_users, stats=stats))
        except (json.JSONDecodeError, csv.Error) as exc:
            print(f"  [跳过] {file.name}: {exc}")
    return records


def inspect_source(path: str | Path) -> dict[str, Any]:
    """摸底一份真实导出：字段分布、年份、重复、家人记录、时间范围。"""
    stats: dict[str, Any] = {
        "fields": Counter(), "raw_records": 0, "by_type": Counter(),
        "sub_user_skipped": 0,
    }
    all_records = load_path(path, include_sub_users=True, stats=stats)
    main_records = [r for r in all_records if not r.is_sub_user]
    # include_sub_users=True 时解析层不会累计这个数，改为按标记重算
    stats["sub_user_skipped"] = len(all_records) - len(main_records)
    by_year = Counter(r.timestamp.year for r in main_records)
    return {
        "files": len(iter_source_files(Path(path))),
        "raw_records": stats["raw_records"],
        "by_type": dict(stats["by_type"]),
        "duplicates": len(main_records) - len({int(r.timestamp.timestamp()) for r in main_records}),
        "sub_user_skipped": stats["sub_user_skipped"],
        "readings": len(main_records),
        "first": min((r.timestamp for r in main_records), default=None),
        "last": max((r.timestamp for r in main_records), default=None),
        "by_year": dict(sorted(by_year.items())),
        "fields": stats["fields"].most_common(),
        "with_skeletal_muscle": sum(1 for r in main_records if r.muscle_total_kg is not None
                                    and r.muscle_kg != r.muscle_total_kg),
        "with_height": sum(1 for r in main_records if r.height_cm),
    }


def manual_record(date: str, weight: float, *, fat_pct: float | None = None,
                  bmi: float | None = None, water_pct: float | None = None,
                  muscle_kg: float | None = None, bone_kg: float | None = None,
                  visceral_fat: float | None = None, basal_met: float | None = None,
                  metabolic_age: float | None = None,
                  jin_to_kg: bool = False) -> Measurement:
    timestamp = parse_time(date)
    if timestamp is None:
        raise ValueError(f"无法解析日期：{date}（用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM）")
    payload: dict[str, Any] = {"timestamp": timestamp.isoformat(), "weight_kg": weight}
    for key, value in (
        ("fat_pct", fat_pct), ("bmi", bmi), ("water_pct", water_pct),
        ("muscle_kg", muscle_kg), ("bone_kg", bone_kg),
        ("visceral_fat", visceral_fat), ("basal_met", basal_met),
        ("metabolic_age", metabolic_age),
    ):
        if value is not None:
            payload[key] = value
    record = from_mapping(payload, jin_to_kg=jin_to_kg)
    assert record is not None
    record.source = "manual"
    return record
