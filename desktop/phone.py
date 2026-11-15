"""手机取图桥接层（纯逻辑，不含 Qt）。

scrcpy-win64-v2.0.zip 里真正有用的是自带的 adb.exe：USB 连上手机后，
它能在本机直接读屏幕、列出/拉取手机里刚保存的图片、模拟点按和启动 App。
全部走 USB，不联网、不上传任何图片；没有手机时所有功能安静降级。

设计要点：
  * 所有 adb 调用都收敛到 PhoneBridge.run()，便于无设备时用替身做单元测试；
  * 新图判定采用「首次扫描建立基线 → 之后只取新出现的文件」，不依赖手机时间，
    也避免把相册里几百张旧照片重复导入；
  * 点按步骤（宏）可录制、可手工编辑，坐标随手机分辨率走归一化比例。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent
ROOT = DESKTOP.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_paths import DATA_DIR, DESKTOP, FROZEN, RESOURCE_DIR
from sync_tool.models import normalize_time_text  # noqa: E402

VENDOR_DIR = RESOURCE_DIR / "vendor" / "scrcpy-win64-v2.0"
PHONE_DIR = DATA_DIR / "out" / "phone"
STEPS_FILE = DATA_DIR / "phone_steps.json" if FROZEN else DESKTOP / "phone_steps.json"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
IMAGE_RE = re.compile(r"\.(jpe?g|png|webp|bmp|tiff?)$", re.I)

# 小米/HyperOS 截图、华为运动健康「保存图片」的常见落点
SCAN_DIRS = (
    "/sdcard/DCIM/Screenshots",
    "/sdcard/Pictures/Screenshots",
    "/sdcard/DCIM/Camera",
    "/sdcard/Pictures",
    "/sdcard/Pictures/Huawei",
    "/sdcard/Pictures/Huawei/Health",
    "/sdcard/DCIM/Huawei",
    "/sdcard/Download",
    "/sdcard/Huawei/Health",
    "/sdcard/Android/data/com.huawei.health/files/Pictures",
)

# 华为运动健康常见包名（按优先级尝试）
HEALTH_PACKAGES = ("com.huawei.health", "com.huawei.hwhealth")

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class PhoneError(Exception):
    """可直接展示给用户的手机相关错误。"""


def ensure_tools() -> Path:
    """确保 scrcpy 包已解包，返回 adb.exe 路径。"""
    adb = VENDOR_DIR / "adb.exe"
    if adb.exists():
        return adb
    zips = sorted(ROOT.glob("scrcpy*.zip")) + sorted(DESKTOP.glob("scrcpy*.zip"))
    if not zips:
        raise PhoneError(
            "没找到 scrcpy 压缩包。请把 scrcpy-win64-v2.0.zip 放在项目根目录，"
            "或者直接在窗口里拖入报告图片（手机功能不是必需的）。")
    target = DESKTOP / "vendor"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zips[0]) as zf:
        zf.extractall(target)
    if not adb.exists():
        raise PhoneError(f"解包后没找到 adb.exe：{adb}")
    return adb


@dataclass
class Device:
    serial: str
    state: str
    model: str = ""
    product: str = ""

    def label(self) -> str:
        return self.model or self.product or self.serial


DEVICE_NAMES = {
    "25053RT47C": "红米 Turbo 5 Max",
    "VOG-AL00": "华为 P30 Pro",
}


def canonical_model(model: str) -> str:
    """adb devices 会把 VOG-AL00 显示成 VOG_AL00，配置键需要统一。"""
    return (model or "").strip().upper().replace("_", "-")


def friendly_model(model: str) -> str:
    raw = (model or "").strip()
    name = DEVICE_NAMES.get(canonical_model(raw), "")
    return f"{name}（{raw}）" if name and raw else name or raw


def device_display_name(device: Device) -> str:
    return friendly_model(device.model or device.product) or device.serial


def device_profile_key(device: Device) -> str:
    return canonical_model(device.model or device.product) or device.serial


@dataclass
class RemoteImage:
    directory: str
    name: str
    size: int

    @property
    def remote(self) -> str:
        return f"{self.directory}/{self.name}"

    @property
    def key(self) -> str:
        """用于去重基线：路径 + 大小。"""
        return f"{self.remote}|{self.size}"


LS_LINE = re.compile(
    r"^[-l]\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\S+\s+\S+\s+(.+)$")


def parse_ls_output(text: str) -> list[RemoteImage]:
    """解析 `ls -lt` 输出（含 @@目录 标记），只保留图片文件。"""
    found: list[RemoteImage] = []
    directory = ""
    for raw in text.replace("\r\n", "\n").splitlines():
        line = raw.strip()
        if line.startswith("@@"):
            directory = line[2:].strip()
            continue
        if not line or line.startswith("total ") or not directory:
            continue
        match = LS_LINE.match(line)
        if not match:
            continue
        size = int(match.group(1))
        name = match.group(2).strip()
        # 去掉软链接的 "xxx -> yyy"
        name = name.split(" -> ")[0].strip()
        if not IMAGE_RE.search(name) or size <= 0:
            continue
        found.append(RemoteImage(directory, name, size))
    return found


def parse_devices(text: str) -> list[Device]:
    devices: list[Device] = []
    for raw in text.replace("\r\n", "\n").splitlines()[1:]:
        line = raw.strip()
        if not line or line.startswith("*") or "List of devices" in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        props = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append(Device(serial, state, props.get("model", ""),
                              props.get("product", "")))
    return devices


def diff_new(known: set[str] | None,
             images: list[RemoteImage]) -> tuple[set[str], list[RemoteImage]]:
    """首次扫描建立基线，之后只返回新出现的图片（纯函数，便于测试）。"""
    if known is None:
        return {item.key for item in images}, []
    fresh = [item for item in images if item.key not in known]
    updated = set(known)
    updated.update(item.key for item in fresh)
    return updated, fresh


def parse_size(text: str) -> tuple[int, int] | None:
    match = re.search(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


# ---------- 手机界面控件树 ----------
# 小米 HyperOS 会拦掉 `input tap/keyevent`（INJECT_EVENTS），但 `monkey -f` 的
# 脚本注入走的是另一条通道，已在红米 Turbo 5 Max / Android 16 上实测可用。
MONKEY_REMOTE = "/sdcard/.garmin_weight_monkey.mrs"
UI_DUMP_REMOTE = "/sdcard/.garmin_weight_ui.xml"
INJECT_MARKERS = ("INJECT_EVENTS", "Injecting input events", "SecurityException")
BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
DATE_RE = re.compile(r"20\d{2}[/-]\d{1,2}[/-]\d{1,2}")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")

# ---------- 「只有体重、没有体脂」的普通称重记录 ----------
# 真机控件树实测（华为运动健康 / 红米 Turbo 5 Max，2026-11-15）：
#   历史列表行有体脂 → 多出 child_item_fat(28.1%)、child_item_skeletal_muscle(33.6 公斤)
#   历史列表行无体脂 → 只有 child_item_weight(86.2)、child_item_weight_unit(公斤)、
#                        child_item_time(14:04)，行高完全相同，不能靠高度区分
#   详情页无体脂     → health_healthdata_inputweight_top_tip 横幅「未测量到脂肪率？」，
#                        顶栏只有「更多选项」没有「分享」，指标区只有 BMI，
#                        因此没有可分享的「人体成分分析报告」，必须跳过
BODYFAT_ROW_RIDS = (
    "child_item_fat", "child_item_fat_image",
    "child_item_skeletal_muscle", "child_item_skeletal_muscle_image",
    "child_item_bone", "child_item_water",
)
NO_FAT_TIP_RID = "health_healthdata_inputweight_top_tip"
NO_FAT_TIPS = (
    "未测量到脂肪率", "未测到脂肪率", "未能测量到脂肪率",
    "未测量到体脂", "未测到体脂", "无脂肪率数据", "无体脂数据",
)
BODYFAT_DETAIL_LABELS = (
    "脂肪率", "骨骼肌量", "肌肉量", "水分率", "骨盐量", "蛋白质",
    "内脏脂肪等级", "去脂体重", "基础代谢率", "身体年龄", "身体得分",
)


@dataclass(frozen=True)
class UiNode:
    text: str
    desc: str
    rid: str
    bounds: tuple[int, int, int, int]
    clickable: bool
    cls: str = ""

    @property
    def label(self) -> str:
        return self.text or self.desc

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def height(self) -> int:
        return self.bounds[3] - self.bounds[1]


def parse_bounds(value: str) -> tuple[int, int, int, int]:
    match = BOUNDS_RE.match(value or "")
    return tuple(int(g) for g in match.groups()) if match else (0, 0, 0, 0)  # type: ignore[return-value]


def parse_ui_xml(xml: str) -> list[UiNode]:
    """把 uiautomator dump 的 XML 拍平成节点列表（纯函数，便于单测）。"""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    nodes: list[UiNode] = []
    for item in root.iter("node"):
        nodes.append(UiNode(
            (item.get("text") or "").strip(),
            (item.get("content-desc") or "").strip(),
            item.get("resource-id") or "",
            parse_bounds(item.get("bounds") or ""),
            item.get("clickable") == "true",
            item.get("class") or "",
        ))
    return nodes


def find_node(nodes: list[UiNode], *, text: str | None = None,
              desc: str | None = None, rid: str | None = None,
              contains: str | None = None, clickable: bool | None = None) -> UiNode | None:
    """按文字 / 描述 / resource-id 后缀找控件；找不到返回 None。"""
    for node in nodes:
        if clickable is not None and node.clickable != clickable:
            continue
        if text is not None and node.text != text:
            continue
        if desc is not None and node.desc != desc:
            continue
        if rid is not None and not node.rid.endswith(rid):
            continue
        if contains is not None and contains not in node.label:
            continue
        if node.bounds == (0, 0, 0, 0):
            continue
        return node
    return None


def detect_page(nodes: list[UiNode]) -> str:
    """识别华为运动健康体重模块的三个页面。"""
    rids = " ".join(node.rid for node in nodes)
    labels = " ".join(node.label for node in nodes)
    if "share_save_to_local_layout" in rids:
        return "share"
    # 历史列表有专属控件；先判它，避免首页上零散的「分享」文字造成误判
    if "health_data_weight_father_ll" in rids:
        return "history"
    # 只用体重模块专属控件判定详情页；主首页同样有「分享」「更多选项」，不能拿它们当特征
    if "level_value_text" in rids or "测量记录" in labels \
            or "ta_weight_bodyindex_mid_time" in rids:
        return "detail"
    return "unknown"


def node_inside(node: UiNode, bounds: tuple[int, int, int, int],
               margin: int = 8) -> bool:
    """控件中心是否落在给定行框内（列表行的子控件都在行框里）。"""
    x1, y1, x2, y2 = bounds
    cx, cy = node.center
    return (x1 - margin <= cx <= x2 + margin
            and y1 - margin <= cy <= y2 + margin)


def row_bodyfat_state(nodes: list[UiNode],
                      bounds: tuple[int, int, int, int]) -> bool | None:
    """判断一条历史记录有没有体成分。

    True  = 行内有脂肪率/骨骼肌量，能出报告；
    False = 只有体重 + 时间，是普通称重，必须跳过；
    None  = 证据不足（例如半屏行、控件还没加载），不猜测，打开详情页再判。
    """
    inside = [node for node in nodes
              if node.bounds != (0, 0, 0, 0) and node_inside(node, bounds)]
    if any(any(node.rid.endswith(rid) for rid in BODYFAT_ROW_RIDS)
           for node in inside) or any("%" in node.text for node in inside):
        return True
    has_weight = any(node.rid.endswith("child_item_weight") for node in inside)
    has_time = any(node.rid.endswith("child_item_time") for node in inside)
    if has_weight and has_time:
        return False
    return None


def is_weight_only_detail(nodes: list[UiNode]) -> bool:
    """详情页兜底：这条记录只有体重，没有体成分报告可分享。

    先认官方横幅（最强证据），再用「有体重 + 有 BMI + 没有分享入口 +
    指标区没有脂肪率等」做结构判断，兼容不同版本/文案。
    """
    if detect_page(nodes) != "detail":
        return False
    labels = " ".join(node.label for node in nodes)
    rids = " ".join(node.rid for node in nodes)
    if NO_FAT_TIP_RID in rids or any(tip in labels for tip in NO_FAT_TIPS):
        return True
    has_share = bool(find_node(nodes, desc="分享")
                     or find_node(nodes, rid="hwappbarpattern_menu_icon"))
    titles = " ".join(node.text for node in nodes
                      if node.rid.endswith("body_index_recycle_title"))
    if titles:
        has_bodyfat = any(label in titles for label in BODYFAT_DETAIL_LABELS)
    else:
        plain = labels
        for tip in NO_FAT_TIPS:
            plain = plain.replace(tip, "")
        has_bodyfat = any(label in plain for label in BODYFAT_DETAIL_LABELS)
    has_bmi = bool(find_node(nodes, text="BMI") or "BMI" in labels)
    has_weight = bool(find_node(nodes, rid="level_value_text"))
    return bool(has_weight and has_bmi and not has_share and not has_bodyfat)


def _numeric(text: str) -> float | None:
    match = NUMBER_RE.search((text or "").replace(",", ""))
    return float(match.group()) if match else None


def make_stamp(date: str, time_text: str) -> str:
    """日期分组头 + 行内时刻 → 'YYYY-MM-DD HH:MM'。"""
    clock = CLOCK_RE.match(normalize_time_text(time_text))
    if not date or not clock:
        return ""
    year, month, day = (int(part) for part in re.split(r"[/-]", date))
    return (f"{year:04d}-{month:02d}-{day:02d} "
            f"{int(clock.group(1)):02d}:{int(clock.group(2)):02d}")


def _row_values(nodes: list[UiNode], bounds: tuple[int, int, int, int],
                date: str) -> dict:
    """从一行的子控件里直接读出结构化数值（不经过 OCR、不经过网络接口）。

    真机实测（红米 Turbo 5 Max / 华为运动健康，2026-11-15）：
      child_item_weight            "86.6"      体重 kg
      child_item_weight_unit       "公斤"      单位（斤要除以 2）
      child_item_time              "08:27"     测量时刻
      child_item_fat               "28.1%"     脂肪率
      child_item_skeletal_muscle   "33.6"      骨骼肌量 kg
    这些是 App 自己渲染出来的原始数字，精度等同云端接口返回值，却只需本机 USB。
    """
    inside = [node for node in nodes
              if node.bounds != (0, 0, 0, 0) and node_inside(node, bounds)]

    def pick(suffix: str) -> UiNode | None:
        return next((node for node in inside
                     if node.rid.endswith(suffix) and node.text), None)

    weight_node = pick("child_item_weight")
    unit_node = pick("child_item_weight_unit")
    fat_node = pick("child_item_fat")
    muscle_node = pick("child_item_skeletal_muscle")
    time_node = pick("child_item_time")
    weight = _numeric(weight_node.text) if weight_node else None
    if weight is not None and unit_node and "斤" in unit_node.text \
            and "公斤" not in unit_node.text:
        weight = round(weight / 2, 3)  # 部分版本把单位切成「斤」
    time_text = time_node.text.strip() if time_node else ""
    return {
        "date": date,
        "time": time_text,
        "stamp": make_stamp(date, time_text),
        "weight_kg": weight,
        "fat_pct": _numeric(fat_node.text) if fat_node else None,
        "muscle_kg": _numeric(muscle_node.text) if muscle_node else None,
    }


def history_rows(nodes: list[UiNode], *,
                 screen_height: int | None = None) -> list[dict]:
    """返回历史列表里可见的「测量记录行」，带稳定 key，滚动后也不会认错。

    行框用 child_item_linear_layout；key 由所属日期分组头 + 行内文字拼接而成，
    不依赖坐标，因此翻页后仍能判断是否已处理过。key_core 不含日期，供上层
    在分组头滚出屏幕时把日期跨屏继承回来。

    屏幕边缘被截断的「半行」子控件不全，complete=False；这类行绝不参与
    bodyfat 判定（否则有体脂记录会因脂肪率那半截没画出来被误判成普通称重），
    也不写入 Garmin —— 小步重叠翻页保证每一行迟早会完整地出现一次。
    """
    headers = sorted(
        (node for node in nodes
         if node.rid.endswith("health_data_weight_father_ll") and DATE_RE.search(node.label)),
        key=lambda node: node.bounds[1])
    row_nodes = [node for node in nodes
                 if node.rid.endswith("child_item_linear_layout") and node.height > 60]
    if not row_nodes:  # 兼容某些版本把整行标成可点但没有 resource-id
        row_nodes = [node for node in nodes
                     if node.clickable and not node.rid and node.height > 100
                     and node.bounds[0] == 0]
    rows: list[dict] = []
    for row in sorted(row_nodes, key=lambda node: node.bounds[1]):
        x1, y1, x2, y2 = row.bounds
        inside = [
            node.text for node in nodes
            if node.text and node.bounds != (0, 0, 0, 0)
            and x1 - 8 <= node.center[0] <= x2 + 8
            and y1 - 8 <= node.center[1] <= y2 + 8
        ]
        date = ""
        for header in headers:
            if header.bounds[1] <= row.bounds[1]:
                match = DATE_RE.search(header.label)
                if match:
                    date = match.group(0)
        texts = " ".join(sorted(set(inside)))
        key_core = f"{texts}|{row.height}"
        values = _row_values(nodes, row.bounds, date)
        clipped = (screen_height is not None
                   and (y1 <= 0 or y2 >= screen_height - 8))
        complete = bool(values["weight_kg"] and values["stamp"] and not clipped)
        state = row_bodyfat_state(nodes, row.bounds)
        rows.append({"center": row.center, "key": f"{date}|{key_core}",
                     "key_core": key_core, "complete": complete,
                     "clipped": clipped, "bodyfat_raw": state,
                     "label": (date + " " + texts).strip()[:48] or "测量记录",
                     "bounds": row.bounds,
                     # 半行不猜：交给详情页兜底，避免把有体脂的记录误跳过
                     "bodyfat": state if complete else None,
                     **values})
    return rows


def apply_carried_date(row: dict, date: str) -> dict:
    """分组头滚出屏幕时，用上一屏记下的日期把这条行补全。"""
    if not date or row.get("stamp"):
        return row
    updated = dict(row)
    updated["date"] = date
    updated["stamp"] = make_stamp(date, row.get("time", ""))
    updated["key"] = f"{date}|{row['key_core']}"
    updated["label"] = (date + " " + row["label"]).strip()[:48]
    updated["complete"] = bool(updated["stamp"] and updated["weight_kg"]
                               and not updated.get("clipped"))
    updated["bodyfat"] = (row.get("bodyfat_raw") if updated["complete"] else None)
    return updated


def visible_header_dates(nodes: list[UiNode]) -> list[str]:
    """当前屏上可见的日期分组头（按 y 从小到大）。"""
    dates = []
    for node in sorted((n for n in nodes
                        if n.rid.endswith("health_data_weight_father_ll")),
                       key=lambda n: n.bounds[1]):
        match = DATE_RE.search(node.label)
        if match:
            dates.append(match.group(0))
    return dates


def row_in_stable_zone(row: dict, screen_height: int) -> bool:
    """行是否稳稳落在屏幕中部（上下边缘容易被动画裁掉子控件，不可信）。"""
    y1, y2 = row["bounds"][1], row["bounds"][3]
    return (y1 >= screen_height * 0.12
            and y2 <= screen_height * 0.86)


def collect_readable_rows(nodes: list[UiNode], *,
                          screen_height: int | None = None,
                          state: dict | None = None) -> list[dict]:
    """把一屏控件树收敛成「数据完整、可直接写库」的行。

    state 跨屏复用，里面存：
      date_by_core  行内容指纹 → 日期分组（分组头滚出屏幕后仍能把日期补回来）
      carry_date    本屏最靠下的可见分组头日期

    半行（控件被屏幕裁掉）和日期缺失的行都不会返回；小步重叠翻页保证
    每条记录至少有一屏是完整可见的，因此不会因此丢数据。
    """
    memory = state if state is not None else {}
    headers = visible_header_dates(nodes)
    if headers:
        memory["carry_date"] = headers[-1]
    date_by_core: dict[str, str] = memory.setdefault("date_by_core", {})
    ready: list[dict] = []
    for row in history_rows(nodes, screen_height=screen_height):
        if row["date"] and row["weight_kg"]:
            date_by_core[row["key_core"]] = row["date"]
        if not row["stamp"]:
            row = apply_carried_date(
                row, date_by_core.get(row["key_core"])
                or memory.get("carry_date", ""))
        if row["complete"] and row["stamp"] and row["weight_kg"]:
            ready.append(row)
    return ready


def scroll_distance_px(rows: list[dict], screen_height: int) -> int:
    """一次只滚 1～1.5 行，保证前后两屏至少重叠一行，绝不整屏翻页。

    原来从 78% 滑到 28%（半屏 1300+ px），加上 monkey 快速甩动带来的惯性，
    真实位移远超一屏，中间几行从来没出现在控件树里，于是永久丢数据。
    """
    heights = sorted(max(1, row["bounds"][3] - row["bounds"][1])
                     for row in rows)
    # 取中位数并设下限：屏幕边缘被裁的半行高度只有几十 px，若用 min()
    # 会把步长压到 120px，导致翻页次数暴涨；中位数仍是完整行高。
    row_height = max(180, heights[len(heights) // 2] if heights else 208)
    return max(180, min(int(row_height * 1.5), int(screen_height * 0.22)))


def page_signature(nodes: list[UiNode]) -> str:
    """用于判断翻页后列表是否真的变了（防止无限滚动）。"""
    return "|".join(row["key"] for row in history_rows(nodes))


def monkey_tap_lines(x: int, y: int, hold_ms: int = 150) -> list[str]:
    return [
        f"DispatchPointer(0,0,0,{int(x)},{int(y)},1,1,0,1,1,0,0)",
        f"UserWait({int(hold_ms)})",
        f"DispatchPointer(0,0,1,{int(x)},{int(y)},1,1,0,1,1,0,0)",
    ]


def monkey_swipe_lines(x1: int, y1: int, x2: int, y2: int,
                       steps: int = 8, step_wait_ms: int = 16) -> list[str]:
    """steps 越多、每步等待越久，手势越「慢」，Android 就不会附加惯性甩动。"""
    steps = max(2, int(steps))
    wait = max(8, int(step_wait_ms))
    lines = [f"DispatchPointer(0,0,0,{int(x1)},{int(y1)},1,1,0,1,1,0,0)",
             f"UserWait({wait})"]
    for index in range(1, steps + 1):
        x = int(x1 + (x2 - x1) * index / steps)
        y = int(y1 + (y2 - y1) * index / steps)
        lines.append(f"DispatchPointer(0,0,2,{x},{y},1,1,0,1,1,0,0)")
        lines.append(f"UserWait({wait})")
    lines.append(f"DispatchPointer(0,0,1,{int(x2)},{int(y2)},1,1,0,1,1,0,0)")
    return lines


def monkey_key_lines(code: int) -> list[str]:
    return [f"DispatchKey(0,0,0,{int(code)},0,0,0,0)",
            "UserWait(80)",
            f"DispatchKey(0,0,1,{int(code)},0,0,0,0)"]


def build_monkey_script(lines: list[str]) -> str:
    # count 必须覆盖长滑动脚本里的全部事件，否则可能被 monkey 提前截断
    return ("type= raw events\n"
            f"count= {max(20, len(lines))}\nspeed= 1.0\nstart data >>\n"
            + "\n".join(lines) + "\n")


class PhoneBridge:
    """对一台 USB 连接的 Android 设备执行本地操作。"""

    def __init__(self, adb: Path | None = None, serial: str | None = None) -> None:
        self.adb = Path(adb) if adb else ensure_tools()
        self.serial = serial
        self.scrcpy = VENDOR_DIR / "scrcpy.exe"
        self._mirror: subprocess.Popen | None = None
        # "auto" 先试原生 input，遇到小米 INJECT_EVENTS 拦截就永久切到 monkey 脚本注入
        self.inject_backend = "auto"

    # ---------- 底层 ----------
    def run(self, args: list[str], *, timeout: float = 15) -> bytes:
        """唯一的外部进程出口；测试时替换本方法即可模拟整台手机。"""
        cmd = [str(self.adb)]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                  creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError as exc:
            raise PhoneError(f"找不到 adb.exe：{self.adb}") from exc
        except subprocess.TimeoutExpired as exc:
            raise PhoneError(f"adb 命令超时：{' '.join(args[:2])}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
            raise PhoneError(detail or f"adb 命令失败（{proc.returncode}）")
        return proc.stdout

    def text(self, args: list[str], *, timeout: float = 15) -> str:
        return self.run(args, timeout=timeout).decode("utf-8", "replace")

    def shell(self, command: str, *, timeout: float = 15) -> str:
        return self.text(["shell", command], timeout=timeout)

    # ---------- 连接 ----------
    def devices(self) -> list[Device]:
        return parse_devices(self.text(["devices", "-l"], timeout=12))

    def attach(self) -> Device:
        devices = self.devices()
        ready = [d for d in devices if d.state == "device"]
        if not ready:
            if any(d.state in ("unauthorized", "authorizing") for d in devices):
                raise PhoneError("手机已连接但未授权：请在手机上点「允许 USB 调试」，"
                                 "并勾选「一律允许使用这台计算机进行调试」。")
            if any(d.state == "offline" for d in devices):
                raise PhoneError("手机处于 offline：重新插拔数据线，或把 USB 模式切成「传输文件」。")
            raise PhoneError("未检测到手机：用数据线连接电脑，并在开发者选项里打开「USB 调试」。")
        self.serial = ready[0].serial
        return ready[0]

    def describe(self) -> str:
        device = self.attach()
        model = ""
        try:
            model = self.shell("getprop ro.product.model").strip()
        except PhoneError:
            pass
        size = ""
        try:
            parsed = parse_size(self.shell("wm size"))
            if parsed:
                size = f" · {parsed[0]}×{parsed[1]}"
        except PhoneError:
            pass
        return f"{friendly_model(model or device.label())}{size}"

    def screen_size(self) -> tuple[int, int] | None:
        return parse_size(self.shell("wm size"))

    # ---------- 取图 ----------
    def screencap(self, dest: Path) -> Path:
        """把手机当前屏幕存成 PNG；exec-out 失败时自动回退到手机临时文件 + pull。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            raw = self.run(["exec-out", "screencap", "-p"], timeout=30)
            if raw.startswith(PNG_SIGNATURE):
                dest.write_bytes(raw)
                return dest
            # 只补正被 PTY 改写过的 PNG 头，绝不能全局替换二进制里的 CRLF
            if raw.startswith(b"\x89PNG\n\x1a\n"):
                dest.write_bytes(b"\x89PNG\r\n\x1a\n" + raw[7:])
                return dest
        except PhoneError:
            pass
        remote = "/sdcard/.garmin_weight_cap.png"
        self.shell(f"screencap -p {remote}", timeout=30)
        self.pull(remote, dest)
        try:
            self.shell(f"rm -f {remote}")
        except PhoneError:
            pass
        return dest

    def scan_images(self, limit: int = 25) -> list[RemoteImage]:
        """扫描常见图库目录里最近的一批图片（每台手机最多两次 shell 调用）。

        第二遍做一次受限的递归查找，覆盖厂商把图片存到未知子目录的情况。
        """
        parts = []
        for directory in SCAN_DIRS:
            parts.append(
                f'if [ -d "{directory}" ]; then echo "@@{directory}"; '
                f'ls -lt "{directory}" 2>/dev/null | head -{limit}; fi')
        found = parse_ls_output(self.shell(" ; ".join(parts), timeout=30))
        recursive = (
            "find /sdcard/DCIM /sdcard/Pictures /sdcard/Download /sdcard/Huawei "
            "-type f 2>/dev/null | grep -Ei '\\.(jpg|jpeg|png|webp|bmp)$' | "
            "head -120 | while read f; do stat -c '%s %n' \"$f\" 2>/dev/null; done")
        try:
            out = self.shell(recursive, timeout=30)
        except PhoneError:
            out = ""
        for line in out.replace("\r\n", "\n").splitlines():
            parts2 = line.strip().split(" ", 1)
            if len(parts2) != 2 or not parts2[0].isdigit():
                continue
            path = parts2[1].strip()
            if not IMAGE_RE.search(path) or "/" not in path:
                continue
            directory, name = path.rsplit("/", 1)
            found.append(RemoteImage(directory, name, int(parts2[0])))
        seen: set[str] = set()
        unique: list[RemoteImage] = []
        for item in found:
            if item.key not in seen:
                seen.add(item.key)
                unique.append(item)
        return unique

    def pull(self, remote: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.run(["pull", remote, str(dest)], timeout=90)
        if not dest.exists() or dest.stat().st_size == 0:
            raise PhoneError(f"拉取失败：{remote}")
        return dest

    def unique_dest(self, remote: str) -> Path:
        PHONE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = re.sub(r"[^\w.\-]+", "_", Path(remote).name)
        dest = PHONE_DIR / f"{stamp}_{name}"
        index = 1
        while dest.exists():
            dest = PHONE_DIR / f"{stamp}_{index}_{name}"
            index += 1
        return dest

    # ---------- 输入注入（自动绕过 HyperOS 的 INJECT_EVENTS 拦截） ----------
    def _try_input(self, command: str) -> bool:
        """原生 input 能用就用；被拦则把本次会话切到 monkey 脚本注入。"""
        if self.inject_backend == "monkey":
            return False
        try:
            output = self.shell(command)
        except PhoneError as exc:
            if not any(marker in str(exc) for marker in INJECT_MARKERS):
                raise
            self.inject_backend = "monkey"
            return False
        if any(marker in output for marker in INJECT_MARKERS):
            self.inject_backend = "monkey"
            return False
        self.inject_backend = "input"
        return True

    def _run_monkey(self, lines: list[str]) -> None:
        """把事件脚本推到手机 /sdcard 隐藏文件，再用 monkey -f 重放。"""
        local = Path(tempfile.gettempdir()) / "garmin_weight_monkey.mrs"
        with open(local, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(build_monkey_script(lines))
        self.run(["push", str(local), MONKEY_REMOTE], timeout=20)
        # 实测：count 给大值会让 monkey 反复重放脚本并卡死 adb（100000 → 40s 超时）。
        # 单脚本模式下脚本只跑一遍，末尾固定给 1；覆盖全部事件靠脚本头里的 count。
        output = self.text(["shell", f"monkey -f {MONKEY_REMOTE} 1"],
                           timeout=30)
        if "Events injected" not in output:
            raise PhoneError("monkey 脚本注入失败：" + output.strip()[-220:])

    def tap(self, x: int, y: int) -> None:
        x, y = int(x), int(y)
        if self._try_input(f"input tap {x} {y}"):
            return
        self._run_monkey(monkey_tap_lines(x, y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 400) -> None:
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        if self._try_input(f"input swipe {x1} {y1} {x2} {y2} {int(ms)}"):
            return
        # monkey 原本固定 8 步 ×16ms ≈ 128ms，等于一次高速甩动 → 惯性滚动过猛。
        # 长按 ms 折算成「多步 + 慢移」，让系统把它识别成拖动而不是 fling。
        steps = max(8, min(48, int(ms) // 25))
        self._run_monkey(monkey_swipe_lines(
            x1, y1, x2, y2, steps=steps,
            step_wait_ms=max(12, int(ms) // steps)))

    def key(self, code: int) -> None:
        if self._try_input(f"input keyevent {int(code)}"):
            return
        self._run_monkey(monkey_key_lines(int(code)))

    def wake(self) -> None:
        self.key(224)  # KEYCODE_WAKEUP

    def back(self) -> None:
        self.key(4)  # KEYCODE_BACK

    def scroll_by(self, pixels: int, *, up: bool = False, ms: int = 700) -> None:
        """短距离慢拖动。向下翻页 = 手指从下往上滑；up=True 则反向补读。"""
        width, height = self.screen_size() or (1080, 2400)
        x = int(width * 0.5)
        y_low, y_high = int(height * 0.76), int(height * 0.30)
        distance = min(max(80, int(pixels)), y_low - y_high - 40)
        y1, y2 = ((y_high, y_high + distance) if up
                  else (y_low, y_low - distance))
        self.swipe(x, y1, x, y2, ms)

    def scroll_down(self, pixels: int | None = None) -> None:
        """默认只滚 1/8 屏（约一行），彻底取代原来的半屏大滑。"""
        if pixels is None:
            _, height = self.screen_size() or (1080, 2400)
            pixels = int(height * 0.12)
        self.scroll_by(pixels)

    def scroll_up(self, pixels: int | None = None) -> None:
        if pixels is None:
            _, height = self.screen_size() or (1080, 2400)
            pixels = int(height * 0.12)
        self.scroll_by(pixels, up=True)

    def scroll_toward_top(self, ms: int = 900) -> None:
        """快速回到列表顶部：手指从屏幕中部往下拖，起点避开顶部下拉刷新区。"""
        width, height = self.screen_size() or (1080, 2400)
        x = int(width * 0.5)
        self.swipe(x, int(height * 0.42), x, int(height * 0.80), ms)

    # ---------- 界面读取 / 安全护栏 ----------
    def dump_ui_xml(self) -> str:
        last = ""
        for _ in range(3):
            try:
                self.shell(f"uiautomator dump {UI_DUMP_REMOTE}", timeout=30)
                xml = self.run(["exec-out", "cat", UI_DUMP_REMOTE],
                               timeout=30).decode("utf-8", "replace")
                if "<hierarchy" in xml:
                    return xml
                last = xml.strip()[:160]
            except PhoneError as exc:
                last = str(exc)
            time.sleep(0.5)
        raise PhoneError(f"读取手机界面失败（控件树）：{last}")

    def ui_nodes(self) -> list[UiNode]:
        return parse_ui_xml(self.dump_ui_xml())

    def current_package(self) -> str:
        output = self.shell("dumpsys window", timeout=25)
        match = re.search(r"mCurrentFocus=Window\{[^}]*?\s+u0\s+([\w.]+)/", output)
        if not match:
            match = re.search(r"mFocusedApp=ActivityRecord\{[^}]*?\s+u0\s+([\w.]+)/",
                              output)
        return match.group(1) if match else ""

    def ensure_foreground(self, packages=HEALTH_PACKAGES) -> None:
        """任何点击之前都要确认前台是华为运动健康，绝不在别的 App 上乱点。"""
        package = self.current_package()
        if package and package not in packages:
            raise PhoneError(
                f"手机当前不在华为运动健康（前台是 {package}）。\n"
                "请切回华为运动健康的体重页面，再点「全自动取报告」；"
                "为避免误点其它应用，已自动停止。")

    def injection_status(self) -> str:
        return {"auto": "待检测", "input": "原生 input",
                "monkey": "monkey 脚本（免安全设置）"}.get(self.inject_backend,
                                                    self.inject_backend)

    # ---------- 投屏 ----------

    def launch(self, package: str) -> None:
        self.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1",
                   timeout=20)

    def open_mirror(self) -> None:
        """打开 scrcpy 投屏窗口，方便在电脑上直接操作手机。"""
        if not self.scrcpy.exists():
            raise PhoneError(f"找不到 scrcpy.exe：{self.scrcpy}")
        self.attach()
        args = [str(self.scrcpy), "-s", self.serial or "",
                "--window-title", "华为运动健康 · 手机投屏",
                "--max-size", "1024", "--no-audio"]
        self._mirror = subprocess.Popen(
            args, cwd=str(VENDOR_DIR), creationflags=CREATE_NO_WINDOW)

    # ---------- 点按步骤（宏） ----------
    def run_steps(self, steps: list[dict], log=lambda msg: None) -> list[Path]:
        """依次执行录制/编辑好的步骤，返回其中 capture 步骤生成的本地图片。"""
        captured: list[Path] = []
        for index, step in enumerate(steps, 1):
            do = str(step.get("do", "")).strip().lower()
            note = step.get("note") or do
            log(f"{index}/{len(steps)} {note}")
            if do == "app":
                self.launch(str(step.get("package") or HEALTH_PACKAGES[0]))
                time.sleep(float(step.get("wait", 2200)) / 1000)
            elif do == "tap":
                self.tap(int(step.get("x", 0)), int(step.get("y", 0)))
                time.sleep(float(step.get("wait", 1200)) / 1000)
            elif do == "swipe":
                self.swipe(int(step.get("x1", 0)), int(step.get("y1", 0)),
                           int(step.get("x2", 0)), int(step.get("y2", 0)),
                           int(step.get("ms", 400)))
                time.sleep(float(step.get("wait", 800)) / 1000)
            elif do == "key":
                self.key(int(step.get("code", 0)))
                time.sleep(float(step.get("wait", 800)) / 1000)
            elif do == "sleep":
                time.sleep(float(step.get("ms", 1000)) / 1000)
            elif do == "capture":
                path = self.screencap(self.unique_dest("phone_screen.png"))
                captured.append(path)
            elif do == "wake":
                self.wake()
                time.sleep(0.6)
            else:
                raise PhoneError(f"未知步骤类型：{do}")
        return captured


def default_steps() -> dict:
    return {
        "_说明": "每款手机分别保存点按步骤，切换手机不会混用坐标；坐标是该手机的真实像素，"
                 "wait 是点完等待的毫秒数。录制完成后对应机型的 enabled 会变成 true。",
        "profiles": {},
    }


def migrate_steps(data: dict) -> dict:
    """旧版只有一套红米坐标，迁移为分机型配置，原步骤原样保留。"""
    if not isinstance(data, dict):
        return default_steps()
    if isinstance(data.get("profiles"), dict):
        return data
    migrated = default_steps()
    legacy = data.get("steps") or []
    if legacy:
        migrated["profiles"]["25053RT47C"] = {
            "label": DEVICE_NAMES["25053RT47C"],
            "enabled": bool(data.get("enabled")),
            "steps": legacy,
        }
    return migrated


def steps_profile(data: dict, device: Device) -> dict:
    profile = data.get("profiles", {}).get(device_profile_key(device), {})
    return profile if isinstance(profile, dict) else {}


def set_steps_profile(data: dict, device: Device, steps: list[dict]) -> None:
    data.setdefault("profiles", {})[device_profile_key(device)] = {
        "label": device_display_name(device),
        "enabled": True,
        "steps": steps,
    }


def load_steps() -> dict:
    if not STEPS_FILE.exists():
        # 打包版第一次运行时，把随包的默认配置（含已录制的红米步骤）复制过来
        bundled = RESOURCE_DIR / "phone_steps.json"
        if FROZEN and bundled.exists():
            STEPS_FILE.parent.mkdir(parents=True, exist_ok=True)
            STEPS_FILE.write_bytes(bundled.read_bytes())
        if not STEPS_FILE.exists():
            data = default_steps()
            save_steps(data)
            return data
    try:
        data = json.loads(STEPS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default_steps()
    return migrate_steps(data)


def save_steps(data: dict) -> None:
    STEPS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")


USB_GUIDE = (
    "已适配红米 Turbo 5 Max 与华为 P30 Pro。第一次开启 USB 调试：\n\n"
    "【红米 Turbo 5 Max（HyperOS）】\n"
    "1. 设置 → 我的设备 → 全部参数与信息 → 连点「OS 版本」7 次\n"
    "2. 设置 → 更多设置 → 开发者选项 → 打开「USB 调试」\n\n"
    "【华为 P30 Pro（EMUI 10）】\n"
    "1. 设置 → 关于手机 → 连点「版本号」7 次\n"
    "2. 设置 → 系统和更新 → 开发人员选项 → 打开「USB 调试」\n\n"
    "【两款手机通用】\n"
    "1. 用数据线连接电脑，USB 用途选「传输文件 / MTP」\n"
    "2. 手机弹出「允许 USB 调试吗」→ 勾选「一律允许」→ 点确定\n"
    "3. 回到本软件点「刷新手机」\n\n"
    "小米的「USB 调试（安全设置）」只影响原生点击；本软件会自动改用\n"
    "monkey 脚本注入，不打开也能自动点按。华为 P30 Pro 已验证可直接\n"
    "使用原生 input。只有想在 scrcpy 投屏窗口直接点击小米手机时，才需\n"
    "额外打开该安全设置（通常要求小米账号 + SIM 卡，并重启手机）。\n\n"
    "只需设置一次；之后插上线就能用。全部通过本机 adb/USB，不联网、不上传图片。"
)

INJECT_GUIDE = (
    "这是小米 HyperOS 的安全策略：它把 `adb shell input tap/keyevent` 拦掉了，\n"
    "报 INJECT_EVENTS 权限错误，但截屏、读控件树、拉图片都不受影响。\n\n"
    "本软件已自动改用 `monkey -f` 事件脚本注入，在红米 Turbo 5 Max /\n"
    "Android 16 上实测可用，无需改手机设置。「录制点按步骤」也一并修好了。\n\n"
    "如果你更想用原生 input（或 scrcpy 投屏里点手机）：\n"
    "设置 → 更多设置 → 开发者选项 → 打开「USB 调试（安全设置）」\n"
    "（需登录小米账号、插入 SIM 卡，部分机型还要切到移动数据验证，然后重启手机）\n"
    "开启后软件会自动优先使用原生 input。"
)


def guide_for(error: str) -> str:
    if "未检测到手机" in error or "未授权" in error or "offline" in error:
        return USB_GUIDE
    if any(marker in error for marker in INJECT_MARKERS):
        return INJECT_GUIDE
    return ""
