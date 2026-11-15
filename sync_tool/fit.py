"""生成 Garmin Connect 可导入的 weight-scale FIT 文件（纯标准库实现）。

FIT 全局消息号：file_id=0, device_info=23, weight_scale=30, file_creator=49
文件类型：weight=9。字段定义与缩放系数对齐 Garmin FIT SDK / python-garminconnect。
"""
from __future__ import annotations

import struct
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from .models import half_up

FIT_EPOCH = 631065600  # 1989-12-31 00:00:00 UTC

# 2026-11-03 起 Garmin Connect 把 weight_scale.metabolic_age（uint8, 单位「年」）
# 当成时长/时间戳渲染：34 岁会显示成 1072935484000 年（= 34 × 31556926000 ms）。
# 这是 Garmin 服务端/网页的显示回归，FIT 文件本身符合官方 Profile（field 10,
# scale 1, offset 0, units years）。默认不写该字段，Garmin 修复后可用开关恢复。
INCLUDE_METABOLIC_AGE_DEFAULT = False

_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)

# (base type byte, size, invalid value, struct format)
_BT = {
    "enum": (0x00, 1, 0xFF, "B"),
    "uint8": (0x02, 1, 0xFF, "B"),
    "uint16": (0x84, 2, 0xFFFF, "H"),
    "uint32": (0x86, 4, 0xFFFFFFFF, "I"),
    "uint32z": (0x8C, 4, 0x00000000, "I"),
}

_MSG = {"file_id": 0, "device_info": 23, "weight_scale": 30, "file_creator": 49}
_FILE_TYPE_WEIGHT = 9


def _crc_byte(crc: int, byte: int) -> int:
    crc = ((crc >> 4) & 0x0FFF) ^ _CRC_TABLE[crc & 0xF] ^ _CRC_TABLE[byte & 0xF]
    crc = ((crc >> 4) & 0x0FFF) ^ _CRC_TABLE[crc & 0xF] ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc & 0xFFFF


def _crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = _crc_byte(crc, byte)
    return crc


def fit_timestamp(value: datetime | float) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.timestamp()
    return int(value - FIT_EPOCH)


class WeightFitEncoder:
    """一个 FIT 文件可写入多条 weight_scale 记录。"""

    def __init__(self, *, include_metabolic_age: bool = INCLUDE_METABOLIC_AGE_DEFAULT) -> None:
        self._buf = BytesIO()
        self._write_header(0)
        self._weight_defined = False
        self._include_metabolic_age = include_metabolic_age

    # ---------- 底层 ----------
    def _write_header(self, data_size: int) -> None:
        self._buf.seek(0)
        self._buf.write(struct.pack("<BBHI4s", 12, 0x10, 108, data_size, b".FIT"))
        self._buf.seek(0, 2)

    @staticmethod
    def _field_def(number: int, base_type: str) -> bytes:
        type_byte, size, _, _ = _BT[base_type]
        return struct.pack("<BBB", number, size, type_byte)

    @staticmethod
    def _pack_content(content: list[tuple[int, str, Any, float | None]]) -> tuple[bytes, bytes]:
        definitions, values = [], []
        for number, base_type, value, scale in content:
            _, _, invalid, fmt = _BT[base_type]
            definitions.append(WeightFitEncoder._field_def(number, base_type))
            if value is None:
                raw = invalid
            else:
                scaled = value * scale if scale is not None else value
                raw = half_up(scaled)
            values.append(struct.pack("<" + fmt, raw))
        return b"".join(definitions), b"".join(values)

    def _write_definition(self, local_type: int, global_msg: int, fields: bytes) -> None:
        self._buf.write(bytes([0x40 | local_type]))
        self._buf.write(struct.pack("<BBHB", 0, 0, global_msg, len(fields) // 3))
        self._buf.write(fields)

    def _write_data(self, local_type: int, values: bytes) -> None:
        self._buf.write(bytes([local_type]))
        self._buf.write(values)

    # ---------- 消息 ----------
    def write_file_info(self, *, timestamp: datetime | None = None,
                        serial_number: int = 0x5A17C0DE,
                        manufacturer: int = 255, product: int = 0) -> None:
        moment = fit_timestamp(timestamp or datetime.now(timezone.utc))
        content: list[tuple[int, str, Any, float | None]] = [
            (3, "uint32z", serial_number, None),
            (4, "uint32", moment, None),
            (1, "uint16", manufacturer, None),
            (2, "uint16", product, None),
            (5, "uint16", None, None),
            (0, "enum", _FILE_TYPE_WEIGHT, None),
        ]
        fields, values = self._pack_content(content)
        self._write_definition(0, _MSG["file_id"], fields)
        self._write_data(0, values)

    def write_file_creator(self, *, software_version: int = 100) -> None:
        content: list[tuple[int, str, Any, float | None]] = [
            (0, "uint16", software_version, None),
            (1, "uint8", None, None),
        ]
        fields, values = self._pack_content(content)
        self._write_definition(1, _MSG["file_creator"], fields)
        self._write_data(1, values)

    def write_device_info(self, timestamp: datetime, *,
                          serial_number: int = 0x5A17C0DE,
                          manufacturer: int = 255) -> None:
        content: list[tuple[int, str, Any, float | None]] = [
            (253, "uint32", fit_timestamp(timestamp), None),
            (3, "uint32z", serial_number, None),
            (7, "uint32", None, None),
            (8, "uint32", None, None),
            (2, "uint16", manufacturer, None),
            (4, "uint16", None, None),
            (5, "uint16", None, None),
            (10, "uint16", None, None),
            (0, "uint8", 0, None),
            (1, "uint8", None, None),
            (6, "uint8", None, None),
            (11, "uint8", None, None),
        ]
        fields, values = self._pack_content(content)
        self._write_definition(2, _MSG["device_info"], fields)
        self._write_data(2, values)

    def write_weight_scale(self, *, timestamp: datetime, weight_kg: float,
                           percent_fat: float | None = None,
                           percent_hydration: float | None = None,
                           visceral_fat_mass: float | None = None,
                           bone_mass: float | None = None,
                           muscle_mass: float | None = None,
                           basal_met: float | None = None,
                           active_met: float | None = None,
                           physique_rating: float | None = None,
                           metabolic_age: float | None = None,
                           visceral_fat_rating: float | None = None,
                           bmi: float | None = None) -> None:
        content: list[tuple[int, str, Any, float | None]] = [
            (253, "uint32", fit_timestamp(timestamp), None),
            (0, "uint16", weight_kg, 100),
            (1, "uint16", percent_fat, 100),
            (2, "uint16", percent_hydration, 100),
            (3, "uint16", visceral_fat_mass, 100),
            (4, "uint16", bone_mass, 100),
            (5, "uint16", muscle_mass, 100),
            (7, "uint16", basal_met, 4),
            (9, "uint16", active_met, 4),
            (8, "uint8", physique_rating, 1),
        ]
        if self._include_metabolic_age:
            content.append((10, "uint8", metabolic_age, 1))
        content += [
            (11, "uint8", visceral_fat_rating, 1),
            (13, "uint16", bmi, 10),
        ]
        fields, values = self._pack_content(content)
        if not self._weight_defined:
            self._write_definition(3, _MSG["weight_scale"], fields)
            self._weight_defined = True
        self._write_data(3, values)

    def finish(self) -> bytes:
        size = len(self._buf.getvalue())
        data_size = size - 12
        self._write_header(data_size)
        body = self._buf.getvalue()
        return body + struct.pack("<H", _crc(body))


def build_weight_fit(records: list[Any], *, height_m: float | None = None,
                     muscle: str = "skeletal",
                     include_metabolic_age: bool = INCLUDE_METABOLIC_AGE_DEFAULT) -> bytes:
    """把 Measurement 列表编码为一个合并的体重 FIT 文件。

    muscle=skeletal（默认）写骨骼肌，与 Garmin Index 秤口径一致；
    muscle=total 写华为的 muscleMass（总肌肉量），仅当用户明确要求时使用。
    include_metabolic_age 默认 False：Garmin Connect 当前会把该字段错误显示为
    「年龄 × 一年的毫秒数」，详见 INCLUDE_METABOLIC_AGE_DEFAULT 注释。
    """
    if not records:
        raise ValueError("没有可写入的记录")
    encoder = WeightFitEncoder(include_metabolic_age=include_metabolic_age)
    encoder.write_file_info(timestamp=records[0].timestamp)
    encoder.write_file_creator()
    for record in records:
        encoder.write_device_info(record.timestamp)
        encoder.write_weight_scale(
            timestamp=record.timestamp,
            weight_kg=record.weight_kg,
            percent_fat=record.fat_pct,
            percent_hydration=record.water_pct,
            bone_mass=record.bone_kg,
            muscle_mass=record.fit_muscle(muscle),
            basal_met=record.basal_met,
            metabolic_age=record.metabolic_age,
            visceral_fat_rating=record.visceral_fat,
            bmi=record.computed_bmi(height_m),
        )
    return encoder.finish()


_WS_FIELDS = {
    253: ("timestamp", 1, "uint32"), 0: ("weight", 100, "uint16"),
    1: ("percent_fat", 100, "uint16"), 2: ("percent_hydration", 100, "uint16"),
    3: ("visceral_fat_mass", 100, "uint16"), 4: ("bone_mass", 100, "uint16"),
    5: ("muscle_mass", 100, "uint16"), 7: ("basal_met", 4, "uint16"),
    9: ("active_met", 4, "uint16"), 8: ("physique_rating", 1, "uint8"),
    10: ("metabolic_age", 1, "uint8"), 11: ("visceral_fat_rating", 1, "uint8"),
    13: ("bmi", 10, "uint16"),
}
_INVALID = {"uint16": 0xFFFF, "uint32": 0xFFFFFFFF, "uint8": 0xFF}


def parse_weight_scale_fit(data: bytes) -> list[dict[str, Any]]:
    """轻量 FIT 回读器，仅用于自检生成结果是否正确。"""
    if data[8:12] != b".FIT":
        raise ValueError("不是有效的 FIT 文件")
    data_size = struct.unpack_from("<I", data, 4)[0]
    pos, end = 12, 12 + data_size
    definitions: dict[int, tuple[int, list[tuple[int, int]]]] = {}
    results: list[dict[str, Any]] = []
    while pos < end:
        header = data[pos]
        pos += 1
        local_type = header & 0x0F
        if header & 0x40:
            global_msg = struct.unpack_from("<H", data, pos + 2)[0]
            count = data[pos + 4]
            pos += 5
            fields = []
            for _ in range(count):
                number, size, _type = struct.unpack_from("<BBB", data, pos)
                pos += 3
                fields.append((number, size))
            definitions[local_type] = (global_msg, fields)
            continue
        global_msg, fields = definitions[local_type]
        if global_msg != _MSG["weight_scale"]:
            for number, size in fields:
                pos += size
            continue
        item: dict[str, Any] = {}
        for number, size in fields:
            raw = int.from_bytes(data[pos:pos + size], "little")
            pos += size
            spec = _WS_FIELDS.get(number)
            if not spec:
                continue
            name, scale, base_type = spec
            if raw == _INVALID[base_type]:
                continue
            if name == "timestamp":
                item[name] = datetime.fromtimestamp(raw + FIT_EPOCH, tz=timezone.utc)
            else:
                item[name] = round(raw / scale, 3)
        results.append(item)
    file_crc = struct.unpack_from("<H", data, len(data) - 2)[0]
    if file_crc != _crc(data[:-2]):
        raise ValueError("FIT CRC 校验失败")
    return results
