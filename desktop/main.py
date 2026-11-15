"""佳明体重助手 —— 本地桌面程序。

把华为运动健康「人体成分分析报告」截图拖进窗口，自动离线 OCR + 四组分方程校验，
确认后一键生成 Garmin Connect 可导入的 CSV / FIT。图片始终留在本机，不上传任何服务器。

用法：双击 desktop/启动软件.bat，或 desktop/.venv/Scripts/pythonw.exe desktop/main.py
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent
ROOT = DESKTOP.parent
for path in (str(ROOT), str(DESKTOP)):
    if path not in sys.path:
        sys.path.insert(0, path)

from app_paths import DATA_DIR, LOG_DIR  # noqa: E402

# pythonw.exe 无控制台时 sys.stdout / stderr 是 None，任何 print 都会抛异常。
# 日志目录写不进去也不能拦住程序启动（个别电脑会禁用 LocalAppData 写入）。
if sys.stdout is None or sys.stderr is None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _stream = open(LOG_DIR / "app.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        _stream = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or _stream
    sys.stderr = sys.stderr or _stream

from PySide6.QtCore import Qt, QThread, Signal, QSettings, QUrl, QTimer
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QIcon,
                           QImage, QPainter, QPalette, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                              QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout,
                              QHeaderView, QLabel, QLineEdit, QMainWindow,
                              QMessageBox, QProgressBar, QPushButton, QScrollArea,
                              QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                              QVBoxLayout, QWidget)

from auto_collect import AutoCollector
from detail_collect import DetailCollector
from fast_collect import FastCollector
from history_export import (merge_rich, rows_to_measurements,
                            values_for_fields)
from ocr_engine import OcrBox, OcrEngine, probe, to_layout_text
from phone import (PHONE_DIR, STEPS_FILE, USB_GUIDE, PhoneBridge, PhoneError,
                   default_steps, device_display_name, diff_new, guide_for,
                   load_steps, save_steps, set_steps_profile, steps_profile)
from sync_tool.fit import build_weight_fit, parse_weight_scale_fit
from sync_tool.garmin_csv import (build_garmin_csv, build_standard_csv,
                                  build_standard_json)
from sync_tool.models import Measurement, parse_time
from sync_tool.report import _parse_keyvalue, parse_report_text, to_measurement

APP_NAME = "佳明体重助手"
VERSION = "1.5.1"
OUTPUT_DIR = DATA_DIR / "out" / "desktop"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}

# key, 表格显示名, 键值文本标签, 分组
FIELDS = [
    ("timestamp", "测量时间", "测量时间", "基本信息"),
    ("weight_kg", "体重 (kg)", "体重", "基本信息"),
    ("bmi", "BMI", "BMI", "基本信息"),
    ("water_mass_kg", "水分 (kg)", "水分", "四组分 · 验算基准"),
    ("protein_mass_kg", "蛋白质 (kg)", "蛋白质", "四组分 · 验算基准"),
    ("fat_mass_kg", "脂肪量 (kg)", "脂肪量", "四组分 · 验算基准"),
    ("bone_kg", "骨盐量 (kg)", "骨盐量", "四组分 · 验算基准"),
    ("fat_pct", "脂肪率 (%)", "脂肪率", "报告读数"),
    ("water_pct", "水分率 (%)", "水分率", "报告读数"),
    ("protein_pct", "蛋白质率 (%)", "蛋白质率", "报告读数"),
    ("lean_mass_kg", "去脂体重 (kg)", "去脂体重", "报告读数"),
    ("skeletal_muscle_kg", "骨骼肌量 (kg)", "骨骼肌量", "报告读数"),
    ("visceral_fat", "内脏脂肪等级", "内脏脂肪等级", "报告读数"),
    ("basal_met", "基础代谢率", "基础代谢率", "报告读数"),
    ("metabolic_age", "身体年龄", "身体年龄", "报告读数"),
    ("body_score", "身体得分", "身体得分", "报告读数"),
]
MASS_KEYS = {"water_mass_kg", "protein_mass_kg", "fat_mass_kg", "bone_mass_kg"}

STYLE = """
QWidget { font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif; font-size: 13px; }
QMainWindow, QDialog { background: #f5f7fa; }
QLabel#title { font-size: 19px; font-weight: 700; color: #16324f; }
QLabel#sub { color: #6b7c93; font-size: 12px; }
QFrame#card { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; }
QPushButton { background: #ffffff; border: 1px solid #cfd9e6; border-radius: 7px;
              padding: 7px 14px; color: #243b53; }
QPushButton:hover { background: #eef4ff; border-color: #9db8dc; }
QPushButton:disabled { color: #9aa8b8; background: #f2f4f7; }
QPushButton#primary { background: #1668c1; border: 1px solid #1668c1; color: #ffffff;
                      font-size: 15px; font-weight: 700; padding: 11px 22px; border-radius: 8px; }
QPushButton#primary:hover { background: #1157a6; }
QPushButton#auto { background: #1668c1; border: 1px solid #1668c1; color: #ffffff;
                   font-weight: 700; padding: 7px 14px; }
QPushButton#auto:hover { background: #1157a6; }
QPushButton#auto:disabled { background: #eef2f7; color: #7b8ca1; border-color: #cfd9e6; }
QTableWidget { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px;
               gridline-color: #eef2f7; }
QTableWidget::item { padding: 5px 6px; }
QHeaderView::section { background: #eef3f9; color: #3d5573; font-weight: 600;
                       border: 0; border-bottom: 1px solid #dbe4ef; padding: 7px 6px; }
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox { background: #ffffff; border: 1px solid #cfd9e6;
              border-radius: 6px; padding: 5px 8px; color: #243b53; }
/* Fusion 的 HighlightedText 是白字；若不显式指定选中项配色，
   下拉弹层里当前选中项会变成「白字白底」看不见（Windows 上实测必现）。 */
QComboBox QAbstractItemView { background: #ffffff; color: #243b53;
              border: 1px solid #cfd9e6; border-radius: 4px; outline: 0;
              selection-background-color: #dbeafe; selection-color: #16324f; }
QComboBox QAbstractItemView::item { min-height: 26px; padding: 4px 8px;
              background: #ffffff; color: #243b53; }
QComboBox QAbstractItemView::item:selected { background: #dbeafe; color: #16324f; }
QComboBox QAbstractItemView::item:hover { background: #eef4ff; color: #16324f; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right;
              width: 22px; border-left: 1px solid #e2e8f0; }
QComboBox:disabled { color: #9aa8b8; background: #f2f4f7; }
QLabel#ok { color: #137a4b; font-weight: 700; }
QLabel#warn { color: #b54708; font-weight: 700; }
QProgressBar { border: 1px solid #dbe4ef; border-radius: 6px; background: #ffffff;
               height: 12px; text-align: center; }
QProgressBar::chunk { background: #1668c1; border-radius: 5px; }
"""


def fmt(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def build_kv_text(values: dict) -> str:
    lines = ["# 由佳明体重助手生成；数值可直接在左侧表格里修改"]
    for key, _label, kv_label, _group in FIELDS:
        lines.append(f"{kv_label} = {fmt(values.get(key))}")
    return "\n".join(lines) + "\n"


def values_from_result(result) -> dict:
    """用 CLI 同一份草稿文本做初值，保证网页/命令行/桌面三条路完全一致。"""
    kv = _parse_keyvalue(result.as_keyvalue_text())
    values = {"timestamp": result.timestamp_text or ""}
    for key, *_ in FIELDS:
        if key == "timestamp":
            continue
        if key == "bone_kg":
            values[key] = kv.get("bone_mass_kg")
        elif key in kv:
            values[key] = kv[key]
        else:
            values[key] = result.resolved.get(key)
    return values


def sync_structured_values(entry: "Entry") -> None:
    """结构化记录在表格里被改过数字后，把改动写回 Measurement。"""
    record = entry.measurement
    if record is None:
        return

    def number(key: str) -> float | None:
        text = str(entry.values.get(key, "")).strip()
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    stamp = parse_time(entry.values.get("timestamp"))
    if stamp:
        record.timestamp = stamp
    weight = number("weight_kg")
    if weight and weight > 0:
        record.weight_kg = round(weight, 3)
    record.fat_pct = number("fat_pct")
    record.muscle_kg = number("skeletal_muscle_kg")
    record.bmi = number("bmi")


def generate_files(records, *, height_m: float | None, muscle: str,
                   garmin_url: str, out_dir: Path = OUTPUT_DIR) -> dict[str, Path]:
    """纯文件生成逻辑（无弹窗，便于自检与脚本复用）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_text = build_garmin_csv(records, height_m=height_m)
    fit_bytes = build_weight_fit(records, height_m=height_m, muscle=muscle,
                                 include_metabolic_age=False)
    if len(parse_weight_scale_fit(fit_bytes)) != len(records):
        raise RuntimeError("FIT 回读自检未通过")
    paths = {
        "csv": out_dir / "garmin_import.csv",
        "fit": out_dir / "garmin_weight.fit",
        "csv_archive": out_dir / "measurements.csv",
        "json_archive": out_dir / "measurements.json",
        "readme": out_dir / "导入说明.txt",
    }
    paths["csv"].write_bytes(csv_text.encode("utf-8-sig"))
    paths["fit"].write_bytes(fit_bytes)
    paths["csv_archive"].write_bytes(build_standard_csv(records).encode("utf-8-sig"))
    paths["json_archive"].write_bytes(build_standard_json(records).encode("utf-8"))
    paths["readme"].write_text(
        "1. 打开 " + garmin_url + "\n"
        "2. 点右上角云朵图标 →「导入数据」→ 选择本目录的 garmin_import.csv 或 garmin_weight.fit\n"
        "3. 若该日期已有记录，先在 Garmin 体重页面删掉旧记录再导入（重传不会覆盖）\n"
        "4. FIT 默认不写「新陈代谢年龄」：Garmin Connect 2026-11 起会把 34 岁显示成 1072935484000 年\n"
        f"生成时间：{datetime.now():%Y-%m-%d %H:%M:%S} · 记录数：{len(records)}\n",
        encoding="utf-8-sig")
    return paths


@dataclass
class Entry:
    path: Path | None
    values: dict = field(default_factory=dict)
    result: object | None = None
    boxes: list[OcrBox] = field(default_factory=list)
    engine: str = ""
    error: str = ""
    checked: bool = True
    # 控件树直读的结构化记录：数值来自 App 自己渲染的文本，不是 OCR 猜测，
    # 因此不需要（也无法）走四组分方程校验，直接可写 Garmin。
    measurement: Measurement | None = None
    source: str = ""

    def refresh(self) -> None:
        if self.measurement is not None:
            return  # 结构化记录不进报告解析器，避免「键值文本」被 OCR 版面猜测污染
        self.result = parse_report_text(build_kv_text(self.values),
                                        source_kind="keyvalue")

    @property
    def valid(self) -> bool:
        if self.measurement is not None:
            return self.measurement.weight_kg > 0
        return bool(self.result and self.result.equation_ok
                    and not self.result.requires_review()
                    and self.values.get("timestamp") and self.values.get("weight_kg"))

    @property
    def time_text(self) -> str:
        return fmt(self.values.get("timestamp")) or "(未填测量时间)"

    @classmethod
    def from_measurement(cls, record: Measurement) -> "Entry":
        values = {key: "" for key, *_ in FIELDS}
        values.update(values_for_fields(record))
        return cls(None, values, measurement=record,
                   source="控件树直读" if record.fat_pct is not None
                   else "普通称重（仅体重）")


# ---------- 清单去重（可独立单测） ----------
# 采集器按「当前位置/从头」多次运行时，同一条历史可能被读到两次。这里按
# 「测量时间 + 体重」判重（与 history_export.merge_rich 同口径：同一分钟可能
# 有不同体重的真实记录，故必须带上体重），保留字段更全、勾选状态更宽松的那条。
def dedup_key(entry: Entry) -> tuple[str, str] | None:
    """返回去重键；时间或体重缺失时返回 None（这种记录不参与去重）。"""
    stamp = fmt(entry.values.get("timestamp"))
    weight = fmt(entry.values.get("weight_kg"))
    if not stamp or not weight:
        return None
    return (stamp, weight)


def entry_richness(entry: Entry) -> int:
    """字段越全越该保留：结构化记录优先看 measurement，再看非空值数量。"""
    if entry.measurement is not None:
        m = entry.measurement
        filled = sum(1 for name in (
            "fat_pct", "bmi", "water_pct", "muscle_kg", "bone_kg",
            "visceral_fat", "basal_met", "metabolic_age", "protein_pct",
            "body_score") if getattr(m, name, None) is not None)
        return 1000 + filled
    return sum(1 for v in entry.values.values() if fmt(v))


def dedup_entries(entries: list[Entry]) -> tuple[list[Entry], int]:
    """按「时间+体重」去重，返回（保留列表, 删除条数）；稳定保序。

    组内保留字段最全的一条；勾选状态取并集（任一条被勾选则保留勾选，
    避免去重把用户想导出的记录弄丢）。无键记录（时间/体重缺失）原样保留。
    """
    best: dict[tuple[str, str], Entry] = {}
    order: list[tuple[str, str] | None] = []
    kept_nc: list[Entry] = []  # 无键记录：原样保留，不参与去重
    for entry in entries:
        key = dedup_key(entry)
        if key is None:
            kept_nc.append(entry)
            continue
        if key not in best:
            best[key] = entry
            order.append(key)
            continue
        current = best[key]
        if entry_richness(entry) > entry_richness(current):
            entry.checked = entry.checked or current.checked
            best[key] = entry
        else:
            current.checked = current.checked or entry.checked
    kept = [best[key] for key in order] + kept_nc
    return kept, len(entries) - len(kept)


class OcrWorker(QThread):
    """后台批量识别，避免加载模型时界面卡死。"""
    progress = Signal(int, int, str)
    ready = Signal(object, str, str)  # Entry, engine label, error

    def __init__(self, paths: list[Path], engine_kind: str) -> None:
        super().__init__()
        self.paths = paths
        self.engine_kind = engine_kind

    def run(self) -> None:
        engine = OcrEngine(self.engine_kind)
        total = len(self.paths)
        for index, path in enumerate(self.paths, 1):
            self.progress.emit(index, total, path.name)
            outcome = engine.recognize(path)
            text = (to_layout_text(outcome.boxes, y_factor=outcome.y_factor)
                    if outcome.ok else "")
            result = parse_report_text(text, source_kind="ocr")
            entry = Entry(path, values_from_result(result), result,
                          outcome.boxes, outcome.engine, outcome.error)
            entry.refresh()
            self.ready.emit(entry, outcome.engine, outcome.error)
        self.progress.emit(total, total, "完成")


def looks_like_report(entry: Entry) -> bool:
    """自动监听时用来挡掉手机里的普通照片/截图，避免污染待导入清单。"""
    if entry.error:
        return False
    values = entry.values
    masses = [key for key in ("water_mass_kg", "protein_mass_kg",
                              "fat_mass_kg", "bone_kg")
              if str(values.get(key, "")).strip()]
    weight = str(values.get("weight_kg", "")).strip()
    return bool(weight) or len(masses) >= 2


class PhoneActionWorker(QThread):
    """后台执行一次 adb 动作，避免界面卡死。"""
    log = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn) -> None:
        super().__init__()
        self.fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self.fn(self.log.emit))
        except Exception as exc:  # noqa: BLE001 - 统一转成界面提示
            self.failed.emit(str(exc))


class PhonePollWorker(QThread):
    """轮询手机图库：手机上每保存/截图一张新图，就自动拉到电脑。"""
    status = Signal(str)
    image = Signal(object)  # Path

    def __init__(self, bridge: PhoneBridge, interval: float = 2.5) -> None:
        super().__init__()
        self.bridge = bridge
        self.interval = interval
        self._stop = False
        self._known: set[str] | None = None
        self._last = ""

    def stop(self) -> None:
        self._stop = True

    def _say(self, text: str) -> None:
        if text != self._last:
            self._last = text
            self.status.emit(text)

    def run(self) -> None:
        while not self._stop:
            try:
                ready = [d for d in self.bridge.devices() if d.state == "device"]
                if not ready:
                    self.bridge.serial = None
                    self._known = None
                    self._say("○ 等待手机连接（数据线 + USB 调试）")
                else:
                    self.bridge.serial = ready[0].serial
                    images = self.bridge.scan_images()
                    first_scan = self._known is None
                    self._known, fresh = diff_new(self._known, images)
                    if first_scan:
                        self._say("● 正在监听手机新图 · 手机上保存报告图或截屏即可自动导入")
                    for item in fresh:
                        if self._stop:
                            break
                        dest = self.bridge.unique_dest(item.remote)
                        self.bridge.pull(item.remote, dest)
                        self.image.emit(dest)
            except Exception as exc:  # noqa: BLE001
                self._say(f"○ 手机连接异常：{exc}")
            for _ in range(max(1, int(self.interval * 10))):
                if self._stop:
                    break
                time.sleep(0.1)


class AutoCollectWorker(QThread):
    """后台跑全自动取报告状态机；每张新图实时送进 OCR 流水线。"""
    log = Signal(str)
    image = Signal(object)  # Path
    records = Signal(object)  # list[Measurement]：只有体重的普通称重
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, bridge: PhoneBridge, limit: int,
                 collect_weight_only: bool = True) -> None:
        super().__init__()
        self.bridge = bridge
        self.limit = limit
        self.collect_weight_only = collect_weight_only
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _sleep(self, seconds: float) -> None:
        for _ in range(max(1, int(seconds * 10))):
            if self._stop:
                return
            time.sleep(0.1)

    def _event(self, kind: str, text: str, path) -> None:
        self.log.emit(text)
        if kind == "image" and path is not None:
            self.image.emit(Path(path))

    def run(self) -> None:
        collector = AutoCollector(
            self.bridge, limit=self.limit, on_event=self._event,
            should_stop=lambda: self._stop, sleep=self._sleep,
            collect_weight_only=self.collect_weight_only)
        try:
            collector.run()
            self.records.emit(rows_to_measurements(collector.weight_only_rows))
            skipped = (f" · 另有 {collector.skipped} 条只有体重的普通称重"
                       f"{'已按「仅体重」收进清单' if self.collect_weight_only else '（跳过）'}"
                       if collector.skipped else "")
            self.done.emit(f"点击方式：{self.bridge.injection_status()} · "
                           f"共保存 {collector.saved} 条报告{skipped}")
        except Exception as exc:  # noqa: BLE001 - 统一转成界面提示
            self.failed.emit(str(exc))


class FastCollectWorker(QThread):
    """控件树直读：小步翻页，把整份历史列表一次读回来，不点分享、不做 OCR。"""
    log = Signal(str)
    row = Signal(object)      # 单条 Measurement，读一条发一条 → 表格实时显示
    records = Signal(object)  # list[Measurement]，最终合并结果
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, bridge: PhoneBridge, *, seek_top: bool = True) -> None:
        super().__init__()
        self.bridge = bridge
        self.seek_top = seek_top
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _sleep(self, seconds: float) -> None:
        for _ in range(max(1, int(seconds * 10))):
            if self._stop:
                return
            time.sleep(0.1)

    def run(self) -> None:
        collector = FastCollector(
            self.bridge, on_event=lambda kind, text: self.log.emit(text),
            should_stop=lambda: self._stop, sleep=self._sleep,
            seek_top=self.seek_top,
            on_record=lambda record: self.row.emit(record))
        try:
            records = collector.run()
            self.records.emit(records)
            self.done.emit(f"控件树直读完成：{len(records)} 条，"
                           f"其中 {collector.weight_only_count} 条只有体重")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class DetailCollectWorker(QThread):
    """详情页控件树直读：逐条点开读 14 项全量，不分享/不出图/不 OCR。"""
    log = Signal(str)
    row = Signal(object)      # 单条 Measurement，读一条发一条 → 表格实时显示
    records = Signal(object)  # list[Measurement]，兼容批量接收
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, bridge: PhoneBridge, *, seek_top: bool = False) -> None:
        super().__init__()
        self.bridge = bridge
        self.seek_top = seek_top
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _sleep(self, seconds: float) -> None:
        for _ in range(max(1, int(seconds * 10))):
            if self._stop:
                return
            time.sleep(0.1)

    def run(self) -> None:
        collector = DetailCollector(
            self.bridge, on_event=lambda kind, text: self.log.emit(text),
            on_record=lambda rec: self.row.emit(rec),
            should_stop=lambda: self._stop, sleep=self._sleep,
            seek_top=self.seek_top)
        try:
            records = collector.run()
            self.done.emit(f"详情页直读完成：{len(records)} 条 · "
                           f"{collector.detail_count} 条 14 项全量 / "
                           f"{collector.weight_only_count} 条只有体重")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class ClickableImage(QLabel):
    """显示手机截图，把点击位置换算成手机真实像素坐标。"""
    clicked = Signal(int, int)

    def __init__(self) -> None:
        super().__init__()
        self._orig: QPixmap | None = None
        self._shown: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setText("正在抓取手机画面…")

    def set_source(self, path: Path, width: int = 360) -> None:
        self._orig = QPixmap(str(path))
        if self._orig.isNull():
            self.setText("手机画面读取失败")
            return
        self._shown = self._orig.scaledToWidth(
            width, Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(self._shown)
        self.setFixedSize(self._shown.size())

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._orig and self._shown:
            pos = event.position()
            self.clicked.emit(
                int(pos.x() * self._orig.width() / max(1, self._shown.width())),
                int(pos.y() * self._orig.height() / max(1, self._shown.height())))
        super().mousePressEvent(event)


RECORD_PROMPTS = (
    "点：体重 / 人体成分入口",
    "点：分享（或右上角「更多」）",
    "点：保存图片 / 保存到相册",
    "继续点下一步（可多次）",
)


class StepRecorder(QDialog):
    """边看手机实时画面边点，录成可重复执行的点按步骤。"""

    def __init__(self, parent, bridge: PhoneBridge) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.device = None
        self.steps: list[dict] = []
        self.tmp = PHONE_DIR / "_录制预览.png"
        self._ready = False
        self.setWindowTitle("录制手机点按步骤")
        self.resize(430, 720)

        layout = QVBoxLayout(self)
        info = QLabel("在下面这张手机实时画面上点一下要点哪里，软件会真的点手机、"
                      "等界面切换后自动刷新，直到录完「保存图片」。以后点「一键取报告」"
                      "就会自动重放这些步骤。")
        info.setWordWrap(True)
        info.setObjectName("sub")
        layout.addWidget(info)

        self.chk_launch = QCheckBox("每次先唤醒并打开「华为运动健康」")
        self.chk_launch.setChecked(True)
        layout.addWidget(self.chk_launch)

        self.hint = QLabel("准备连接手机…")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.image = ClickableImage()
        scroll = QScrollArea()
        scroll.setWidgetResizable(False)
        scroll.setWidget(self.image)
        layout.addWidget(scroll, 1)
        self.image.clicked.connect(self._on_click)

        bar = QHBoxLayout()
        btn_refresh = QPushButton("刷新画面")
        btn_refresh.clicked.connect(lambda: self._guarded(self._grab))
        bar.addWidget(btn_refresh)
        btn_undo = QPushButton("撤销上一步")
        btn_undo.clicked.connect(self._undo)
        bar.addWidget(btn_undo)
        btn_edit = QPushButton("编辑步骤文件")
        btn_edit.clicked.connect(self._edit_file)
        bar.addWidget(btn_edit)
        bar.addStretch(1)
        btn_done = QPushButton("完成并保存")
        btn_done.clicked.connect(self._finish)
        bar.addWidget(btn_done)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        bar.addWidget(btn_cancel)
        layout.addLayout(bar)
        QTimer.singleShot(30, self._prepare)

    def _guarded(self, fn) -> None:
        try:
            fn()
        except PhoneError as exc:
            QMessageBox.warning(self, APP_NAME, f"手机操作失败：{exc}")

    def _prepare(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.device = self.bridge.attach()
            self.setWindowTitle(f"录制手机点按步骤 · {device_display_name(self.device)}")
            if self.chk_launch.isChecked():
                self.bridge.wake()
                self.bridge.launch("com.huawei.health")
                time.sleep(2.2)
            self._grab()
            self._ready = True
            self._hint_text()
        except PhoneError as exc:
            QMessageBox.warning(self, APP_NAME, f"无法连接手机：{exc}\n\n{guide_for(str(exc))}")
            self.reject()
        finally:
            QApplication.restoreOverrideCursor()

    def _grab(self) -> None:
        PHONE_DIR.mkdir(parents=True, exist_ok=True)
        self.bridge.screencap(self.tmp)
        self.image.set_source(self.tmp)

    def _hint_text(self) -> None:
        if not self.steps:
            self.hint.setText("第 1 步：" + RECORD_PROMPTS[0] + "（点画面任意位置）")
        else:
            prompt = RECORD_PROMPTS[min(len(self.steps), len(RECORD_PROMPTS) - 1)]
            self.hint.setText(f"已录 {len(self.steps)} 步。下一步：{prompt}；"
                              "录完「保存图片」后点「完成并保存」。")

    def _on_click(self, x: int, y: int) -> None:
        if not self._ready:
            return
        prompt = RECORD_PROMPTS[min(len(self.steps), len(RECORD_PROMPTS) - 1)]
        self.steps.append({"do": "tap", "x": x, "y": y, "wait": 1500,
                           "note": f"第{len(self.steps) + 1}步 {prompt}"})
        try:
            self.bridge.tap(x, y)
            time.sleep(1.5)
            self._grab()
        except PhoneError as exc:
            QMessageBox.warning(self, APP_NAME, f"点按后刷新失败：{exc}")
        self._hint_text()

    def _undo(self) -> None:
        if not self.steps:
            return
        self.steps.pop()
        QMessageBox.information(
            self, APP_NAME,
            "已删掉最后一步。注意手机界面不会回退，请用「刷新画面」确认当前画面，"
            "必要时取消后重新录制。")
        self._hint_text()

    def _edit_file(self) -> None:
        if not STEPS_FILE.exists():
            save_steps(default_steps())
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(STEPS_FILE)))

    def _finish(self) -> None:
        if not self.steps:
            QMessageBox.information(self, APP_NAME, "至少录制一步再保存。")
            return
        prep: list[dict] = []
        if self.chk_launch.isChecked():
            prep = [{"do": "wake", "note": "唤醒屏幕"},
                    {"do": "app", "package": "com.huawei.health", "wait": 2200,
                     "note": "打开华为运动健康"}]
        steps = prep + self.steps + [
            {"do": "sleep", "ms": 1800, "note": "等待图片保存完成"}]
        data = load_steps()
        set_steps_profile(data, self.device, steps)
        save_steps(data)
        QMessageBox.information(
            self, APP_NAME,
            f"已为{device_display_name(self.device)}保存 {len(steps)} 步，其它手机的步骤保持不变。\n\n"
            "以后点「重放录制步骤」会自动选择当前手机的配置：唤醒 → 打开 App → "
            "按坐标点按 → 等待保存 → 把新图片拉到电脑并识别。")
        self.accept()


class ImagePreview(QScrollArea):
    """原图预览：按窗口宽度铺开、可上下滚动和缩放。

    报告是 484×1920 的长图，若强行「整图适配窗口」数字会小到看不清，
    所以这里保持横向铺满、纵向滚动；点击右侧字段会自动滚到对应位置并画红框。
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label = QLabel("把报告截图拖进窗口\n或点「选择图片」")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWidget(self._label)
        self.setStyleSheet("QScrollArea { background:#ffffff; border:1px solid #e2e8f0;"
                           " border-radius:8px; }")
        self._src: QPixmap | None = None
        self._highlight: OcrBox | None = None
        self._zoom = 1.0
        self._ratio = 1.0
        self.setMinimumWidth(240)

    def load(self, path: Path | None) -> None:
        self._highlight = None
        self._src = QPixmap(str(path)) if path and Path(path).exists() else None
        self._render()

    def zoom(self, factor: float) -> None:
        self._zoom = max(0.6, min(4.0, self._zoom * factor))
        self._render()

    def _render(self) -> None:
        if self._src is None:
            self._label.setPixmap(QPixmap())
            self._label.setText("把报告截图拖进窗口\n或点「选择图片」")
            self._label.resize(max(200, self.viewport().width() - 16),
                               max(160, self.viewport().height() - 16))
            return
        width = max(180, int((self.viewport().width() - 16) * self._zoom))
        pixmap = self._src.scaledToWidth(
            width, Qt.TransformationMode.SmoothTransformation)
        self._ratio = width / self._src.width()
        if self._highlight:
            box = self._highlight
            painter = QPainter(pixmap)
            painter.setPen(QPen(QColor("#e03131"), 3))
            painter.drawRect(int(box.x * self._ratio) - 6,
                             int(box.y * self._ratio) - 8,
                             max(40, int(len(box.text) * 14 * self._ratio)),
                             max(26, int(34 * self._ratio)))
            painter.end()
        self._label.setPixmap(pixmap)
        self._label.resize(pixmap.size())

    def highlight(self, box: OcrBox | None) -> None:
        self._highlight = box
        self._render()
        if box:
            self.ensureVisible(int(box.x * self._ratio) + 40,
                               int(box.y * self._ratio) + 20, 60, 60)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._render()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("GarminWeightHelper", "App")
        self.engine_pref = str(self.settings.value("engine", "auto"))
        self.entries: list[Entry] = []
        self.current: int = -1
        self._loading = False
        self._worker: OcrWorker | None = None
        self._replace_row: int | None = None
        self._pending: list[Path] = []
        self._seen_phone_paths: set[Path] = set()
        self._auto_paths: set[Path] = set()
        self._phone_action: PhoneActionWorker | None = None
        self._phone_poll: PhonePollWorker | None = None
        self._auto_worker: AutoCollectWorker | None = None
        self._fast_worker: FastCollectWorker | None = None
        self._detail_worker: DetailCollectWorker | None = None
        # 快速清单采集期间「时间+体重 → 表格行号」的索引：同一条记录被更全的
        # 版本读到时原地更新，而不是在表格里再加一行
        self._fast_index: dict[tuple[str, str], int] = {}
        self.setWindowTitle(f"{APP_NAME}  v{VERSION}")
        self.resize(1180, 820)
        self.setAcceptDrops(True)
        self._build_ui()
        self._refresh_engine_label()
        self._refresh_list()
        self.statusBar().showMessage("准备就绪 · 拖入报告截图，或打开「自动接收手机新图」")
        if bool(int(self.settings.value("auto_phone", 0))):
            self.chk_auto.setChecked(True)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        sub = QLabel("华为体成分报告截图 → Garmin Connect · 图片只在本机，OCR 全程离线")
        sub.setObjectName("sub")
        titles.addWidget(title)
        titles.addWidget(sub)
        header.addLayout(titles)
        header.addStretch(1)
        self.engine_label = QLabel()
        self.engine_label.setObjectName("sub")
        header.addWidget(self.engine_label)
        self.btn_engine = QPushButton("OCR 设置")
        self.btn_engine.clicked.connect(self._choose_engine)
        header.addWidget(self.btn_engine)
        root.addLayout(header)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("佳明账号："))
        self.region = QComboBox()
        self.region.addItems(["中国区 connect.garmin.cn", "国际区 connect.garmin.com"])
        self.region.setCurrentIndex(int(self.settings.value("global", 0)))
        self.region.currentIndexChanged.connect(
            lambda i: self.settings.setValue("global", i))
        opts.addWidget(self.region)
        opts.addSpacing(14)
        opts.addWidget(QLabel("身高 (cm)："))
        self.height = QDoubleSpinBox()
        self.height.setRange(0, 250)
        self.height.setDecimals(1)
        self.height.setValue(float(self.settings.value("height", 179.2)))
        self.height.setSpecialValueText("用报告反推")
        self.height.valueChanged.connect(
            lambda v: self.settings.setValue("height", v))
        opts.addWidget(self.height)
        opts.addSpacing(14)
        opts.addWidget(QLabel("肌肉量口径："))
        self.muscle = QComboBox()
        self.muscle.addItems(["骨骼肌（推荐，与 Garmin Index 一致）", "总肌肉量"])
        self.muscle.setCurrentIndex(int(self.settings.value("muscle", 0)))
        self.muscle.currentIndexChanged.connect(
            lambda i: self.settings.setValue("muscle", i))
        opts.addWidget(self.muscle)
        opts.addStretch(1)
        root.addLayout(opts)

        # ---------- 手机取图 ----------
        phone_bar = QHBoxLayout()
        phone_bar.addWidget(QLabel("手机取图："))
        self.btn_auto = QPushButton("★ 全自动取报告")
        self.btn_auto.setToolTip(
            "手机上打开华为运动健康的体重页面，点这里后自动逐条「分享 → 保存」并拉回电脑识别；"
            "小米无需开启「USB 调试（安全设置）」")
        self.btn_auto.setObjectName("auto")
        self.btn_auto.clicked.connect(self._auto_toggle)
        phone_bar.addWidget(self.btn_auto)
        self.btn_fast = QPushButton("⚡ 快速清单（控件树直读）")
        self.btn_fast.setToolTip(
            "不点分享、不生成长图、不做 OCR：直接读「所有数据」列表里的"
            "日期/体重/脂肪率/骨骼肌量，一次拿全历史；只有体重的普通称重也会一起收进清单")
        self.btn_fast.setObjectName("auto")
        self.btn_fast.clicked.connect(self._fast_toggle)
        phone_bar.addWidget(self.btn_fast)
        self.btn_detail = QPushButton("🔍 详情全量（控件树直读）")
        self.btn_detail.setToolTip(
            "逐条点开详情页直接读控件树里的 14 项人体成分（脂肪率/骨骼肌量/"
            "水分率/骨盐量/内脏脂肪/基础代谢/蛋白质/身体年龄/身体得分…），"
            "不分享、不生成长图、不做 OCR，也比列表多 10 项；只有体重的普通称重同样收进来")
        self.btn_detail.setObjectName("auto")
        self.btn_detail.clicked.connect(self._detail_toggle)
        phone_bar.addWidget(self.btn_detail)
        phone_bar.addWidget(QLabel("最多"))
        self.spin_limit = QSpinBox()
        self.spin_limit.setRange(1, 500)
        self.spin_limit.setValue(int(self.settings.value("auto_limit", 10)))
        self.spin_limit.setFixedWidth(64)
        self.spin_limit.setToolTip("想一次导出全部历史就填大一点，例如 200")
        self.spin_limit.valueChanged.connect(
            lambda v: self.settings.setValue("auto_limit", v))
        phone_bar.addWidget(self.spin_limit)
        phone_bar.addWidget(QLabel("条"))
        self.chk_auto = QCheckBox("自动接收手机新图")
        self.chk_auto.setToolTip("开启后，手机上每次「保存图片」或系统截屏，都会自动拉到电脑并识别")
        self.chk_auto.toggled.connect(self._toggle_auto)
        phone_bar.addWidget(self.chk_auto)
        self.btn_capture = QPushButton("抓取手机屏幕")
        self.btn_capture.setToolTip("把手机当前画面拉到电脑并立即识别；一屏放不下时用保存的长图")
        self.btn_capture.clicked.connect(self._phone_capture)
        phone_bar.addWidget(self.btn_capture)
        self.btn_phone = QPushButton("刷新手机")
        self.btn_phone.setToolTip("重新检测 USB 连接的手机")
        self.btn_phone.clicked.connect(self._phone_refresh)
        phone_bar.addWidget(self.btn_phone)
        phone_bar.addStretch(1)
        root.addLayout(phone_bar)

        phone_aux = QHBoxLayout()
        phone_aux.addWidget(QLabel("辅助："))
        self.chk_weight_only = QCheckBox("普通称重也收进清单（仅体重）")
        self.chk_weight_only.setChecked(
            bool(int(self.settings.value("collect_weight_only", 1))))
        self.chk_weight_only.setToolTip(
            "全自动取报告时，只有体重、没有体脂的数据无法生成报告；"
            "勾上就改用控件树里的时间+体重，按「仅体重」写入 Garmin，不丢记录")
        self.chk_weight_only.toggled.connect(
            lambda v: self.settings.setValue("collect_weight_only", int(v)))
        phone_aux.addWidget(self.chk_weight_only)
        self.btn_mirror = QPushButton("手机投屏")
        self.btn_mirror.setToolTip("打开 scrcpy 窗口，在电脑上直接操作手机（可选）")
        self.btn_mirror.clicked.connect(self._phone_mirror)
        phone_aux.addWidget(self.btn_mirror)
        self.btn_record = QPushButton("录制点按步骤")
        self.btn_record.setToolTip("为当前手机单独录制坐标；红米与华为 P30 Pro 的配置互不影响")
        self.btn_record.clicked.connect(self._phone_record)
        phone_aux.addWidget(self.btn_record)
        self.btn_steps = QPushButton("重放录制步骤")
        self.btn_steps.setToolTip("自动选择当前手机的录制步骤，重放并拉取新保存的图片")
        self.btn_steps.clicked.connect(self._phone_run_steps)
        phone_aux.addWidget(self.btn_steps)
        btn_inject = QPushButton("注入自检")
        btn_inject.setToolTip("检查手机是否允许电脑模拟点击；小米会自动启用免安全设置的注入方式")
        btn_inject.clicked.connect(self._phone_inject_check)
        phone_aux.addWidget(btn_inject)
        btn_guide = QPushButton("连接指引")
        btn_guide.clicked.connect(lambda: QMessageBox.information(
            self, "USB 调试开启指引", USB_GUIDE))
        phone_aux.addWidget(btn_guide)
        phone_aux.addStretch(1)
        root.addLayout(phone_aux)

        status_row = QHBoxLayout()
        self.phone_label = QLabel("手机：未连接（可选）")
        self.phone_label.setObjectName("sub")
        self.phone_label.setMinimumWidth(340)
        status_row.addWidget(self.phone_label)
        status_row.addStretch(1)
        phone_hint = QLabel("已适配红米 Turbo 5 Max / 华为 P30 Pro · 全走本机 USB · "
                            "不联网、不上传 · 没插手机时原功能照常可用")
        phone_hint.setObjectName("sub")
        status_row.addWidget(phone_hint)
        root.addLayout(status_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QFrame()
        left.setObjectName("card")
        lv = QVBoxLayout(left)
        lv.setContentsMargins(10, 10, 10, 10)
        lv.addWidget(QLabel("原图（点击右侧数值行可定位到图中位置）"))
        self.preview = ImagePreview()
        lv.addWidget(self.preview, 1)
        nav = QHBoxLayout()
        self.btn_pick = QPushButton("选择图片（可多选）")
        self.btn_pick.clicked.connect(self._pick_files)
        nav.addWidget(self.btn_pick)
        self.btn_rescan = QPushButton("重新识别当前图")
        self.btn_rescan.clicked.connect(self._rescan_current)
        nav.addWidget(self.btn_rescan)
        nav.addStretch(1)
        btn_zoom_out = QPushButton("－")
        btn_zoom_out.setFixedWidth(34)
        btn_zoom_out.setToolTip("缩小原图")
        btn_zoom_out.clicked.connect(lambda: self.preview.zoom(1 / 1.3))
        nav.addWidget(btn_zoom_out)
        btn_zoom_in = QPushButton("＋")
        btn_zoom_in.setFixedWidth(34)
        btn_zoom_in.setToolTip("放大原图，方便核对小数点")
        btn_zoom_in.clicked.connect(lambda: self.preview.zoom(1.3))
        nav.addWidget(btn_zoom_in)
        lv.addLayout(nav)
        splitter.addWidget(left)

        right = QFrame()
        right.setObjectName("card")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(10, 10, 10, 10)
        rv.addWidget(QLabel("识别结果 · 直接改错的数字，状态会实时重算"))
        self.fields = QTableWidget(len(FIELDS), 4)
        self.fields.setHorizontalHeaderLabels(["字段", "数值（可编辑）", "校验", "说明"])
        self.fields.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.fields.setColumnWidth(0, 150)
        self.fields.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.fields.setColumnWidth(1, 130)
        self.fields.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.fields.setColumnWidth(2, 88)
        self.fields.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.fields.verticalHeader().setVisible(False)
        for row, (key, label, _kv, group) in enumerate(FIELDS):
            self.fields.setItem(row, 0, QTableWidgetItem(label))
            self.fields.setItem(row, 1, QTableWidgetItem(""))
            self.fields.setItem(row, 2, QTableWidgetItem(""))
            self.fields.setItem(row, 3, QTableWidgetItem(group))
            for col in (0, 2, 3):
                item = self.fields.item(row, col)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if key in MASS_KEYS or key == "bone_kg" or key in ("weight_kg", "timestamp"):
                self.fields.item(row, 0).setForeground(QColor("#1668c1"))
        self.fields.itemChanged.connect(self._on_field_edited)
        self.fields.cellClicked.connect(self._on_field_clicked)
        rv.addWidget(self.fields, 1)
        self.check_label = QLabel("等待图片…")
        self.check_label.setObjectName("warn")
        rv.addWidget(self.check_label)
        splitter.addWidget(right)
        splitter.setSizes([400, 700])
        root.addWidget(splitter, 1)

        list_bar = QHBoxLayout()
        list_bar.addWidget(QLabel("待导入清单"))
        self.list_hint = QLabel("（勾选要导出的记录）")
        self.list_hint.setObjectName("sub")
        list_bar.addWidget(self.list_hint)
        list_bar.addStretch(1)
        btn_manual = QPushButton("手工新增一条")
        btn_manual.clicked.connect(self._add_manual)
        list_bar.addWidget(btn_manual)
        btn_remove = QPushButton("移除选中")
        btn_remove.clicked.connect(self._remove_selected)
        list_bar.addWidget(btn_remove)
        btn_dedup = QPushButton("🔀 去重")
        btn_dedup.setToolTip("按「测量时间+体重」去除重复采集，保留字段最全的一条")
        btn_dedup.clicked.connect(self._deduplicate)
        list_bar.addWidget(btn_dedup)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self._clear_all)
        list_bar.addWidget(btn_clear)
        root.addLayout(list_bar)

        self.listing = QTableWidget(0, 6)
        self.listing.setHorizontalHeaderLabels(
            ["", "测量时间", "体重 kg", "体脂率 %", "校验", "来源"])
        self.listing.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.listing.setColumnWidth(0, 34)
        for col in (2, 3, 4):
            self.listing.setColumnWidth(col, 100)
        self.listing.setColumnWidth(5, 190)
        self.listing.verticalHeader().setVisible(False)
        self.listing.setMaximumHeight(170)
        self.listing.cellClicked.connect(self._on_list_clicked)
        self.listing.itemChanged.connect(self._on_list_check_changed)
        root.addWidget(self.listing)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        bottom = QHBoxLayout()
        self.btn_export = QPushButton("生成佳明文件")
        self.btn_export.setObjectName("primary")
        self.btn_export.clicked.connect(self._export)
        bottom.addWidget(self.btn_export)
        btn_folder = QPushButton("打开输出文件夹")
        btn_folder.clicked.connect(self._open_folder)
        bottom.addWidget(btn_folder)
        btn_garmin = QPushButton("打开佳明导入页")
        btn_garmin.clicked.connect(self._open_garmin)
        bottom.addWidget(btn_garmin)
        bottom.addStretch(1)
        self.foot = QLabel("首次使用需联网安装离线 OCR 组件；之后识别全程断网可用")
        self.foot.setObjectName("sub")
        bottom.addWidget(self.foot)
        root.addLayout(bottom)

        self.setCentralWidget(central)

    # ---------- 引擎 ----------
    def _refresh_engine_label(self) -> None:
        info = probe()
        self.foot.setText(
            "图片永不上传 · 识别全程离线，可断网使用"
            if info.get("rapid") and info.get("onnx") else
            "首次使用需联网安装离线 OCR 组件；之后识别全程断网可用")
        if self.engine_pref == "manual":
            self.engine_label.setText("● 手工填写模式")
            self.engine_label.setStyleSheet("color:#6b7c93;font-weight:700;")
        elif self.engine_pref == "windows" or not (info.get("rapid") and info.get("onnx")):
            self.engine_label.setText("● Windows 内置 OCR（建议安装离线 OCR 提升准确率）")
            self.engine_label.setStyleSheet("color:#b54708;font-weight:700;")
        else:
            self.engine_label.setText(f"● 离线 OCR 就绪  RapidOCR {info['rapid']}")
            self.engine_label.setStyleSheet("color:#137a4b;font-weight:700;")

    def _choose_engine(self) -> None:
        info = probe()
        box = QMessageBox(self)
        box.setWindowTitle("OCR 设置")
        box.setText("识别引擎（全部在本机运行，不上传图片）")
        box.setInformativeText(
            f"RapidOCR：{info.get('rapid') or '未安装'}\n"
            f"ONNX Runtime：{info.get('onnx') or '未安装'}\n"
            f"Windows 内置 OCR：{info.get('windows') or '不可用'}\n\n"
            "RapidOCR 把 PaddleOCR 的中文模型转成 ONNX 随包分发，\n"
            "装完之后断网也能识别，数字准确率明显高于系统自带 OCR。\n"
            "没装时会自动降级到 Windows 内置 OCR，仍可手工改数。")
        btn_auto = box.addButton("优先离线 OCR（推荐）", QMessageBox.ButtonRole.AcceptRole)
        btn_win = box.addButton("只用 Windows 内置", QMessageBox.ButtonRole.ActionRole)
        btn_manual = box.addButton("纯手工填写", QMessageBox.ButtonRole.ActionRole)
        box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_auto:
            self.engine_pref = "auto"
        elif clicked is btn_win:
            self.engine_pref = "windows"
        elif clicked is btn_manual:
            self.engine_pref = "manual"
        self.settings.setValue("engine", self.engine_pref)
        self._refresh_engine_label()

    # ---------- 手机取图 ----------
    def _phone_status(self, text: str) -> None:
        self.phone_label.setText(text)
        color = ("#137a4b" if text.startswith("●") else
                 "#b54708" if text.startswith("○") else "#6b7c93")
        self.phone_label.setStyleSheet(f"color:{color};font-weight:700;")

    def _phone_error(self, message: str) -> None:
        self._phone_status(f"○ {message}")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(APP_NAME)
        box.setText(message)
        guide = guide_for(message)
        if guide:
            box.setInformativeText(guide)
        box.exec()

    def _run_phone_action(self, fn, on_done) -> None:
        if self._phone_action and self._phone_action.isRunning():
            QMessageBox.information(self, APP_NAME, "还有手机操作正在执行，请稍等。")
            return
        worker = PhoneActionWorker(fn)
        worker.log.connect(lambda msg: self.statusBar().showMessage(msg, 5000))
        worker.failed.connect(self._phone_error)
        worker.done.connect(on_done)
        self._phone_action = worker
        worker.start()

    def _phone_refresh(self) -> None:
        def task(log):
            log("正在检测 USB 连接的手机…")
            return PhoneBridge().describe()
        self._run_phone_action(
            task, lambda text: self._phone_status(f"● 已连接：{text}"))

    def _phone_capture(self) -> None:
        def task(log):
            bridge = PhoneBridge()
            bridge.attach()
            log("正在抓取手机当前屏幕…")
            return [bridge.screencap(bridge.unique_dest("phone_screen.png"))]
        self._run_phone_action(task, self._enqueue_ocr)

    def _toggle_auto(self, checked: bool) -> None:
        self.settings.setValue("auto_phone", 1 if checked else 0)
        if not checked:
            if self._phone_poll and self._phone_poll.isRunning():
                self._phone_poll.stop()
                self._phone_poll.wait(3000)
            self._phone_poll = None
            self._phone_status("手机：自动接收已关闭")
            return
        try:
            bridge = PhoneBridge()
        except PhoneError as exc:
            self.chk_auto.blockSignals(True)
            self.chk_auto.setChecked(False)
            self.chk_auto.blockSignals(False)
            self._phone_error(str(exc))
            return
        worker = PhonePollWorker(bridge)
        worker.status.connect(self._phone_status)
        worker.image.connect(self._on_auto_image)
        self._phone_poll = worker
        worker.start()
        self.statusBar().showMessage(
            "已开启自动接收：手机上保存报告图或系统截屏，会自动拉到电脑并识别", 8000)

    def _on_auto_image(self, path) -> None:
        path = Path(path)
        self._auto_paths.add(path)
        self.statusBar().showMessage(f"手机新图 {path.name} → 正在离线识别…", 6000)
        self._enqueue_ocr([path])

    def _phone_record(self) -> None:
        try:
            bridge = PhoneBridge()
            bridge.attach()
        except PhoneError as exc:
            self._phone_error(str(exc))
            return
        StepRecorder(self, bridge).exec()

    def _phone_run_steps(self) -> None:
        try:
            bridge = PhoneBridge()
            device = bridge.attach()
        except PhoneError as exc:
            self._phone_error(str(exc))
            return
        profile = steps_profile(load_steps(), device)
        if not profile.get("enabled") or not profile.get("steps"):
            answer = QMessageBox.question(
                self, APP_NAME,
                f"当前手机{device_display_name(device)}还没有自己的点按步骤。\n"
                "为避免混用另一款手机的屏幕坐标，需要单独录制一次。\n"
                "现在看着手机实时画面录制吗？（红米已录制的步骤会保留）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self._phone_record()
            return
        polling = bool(self._phone_poll and self._phone_poll.isRunning())

        def task(log):
            before = set() if polling else {i.key for i in bridge.scan_images()}
            paths = bridge.run_steps(profile["steps"], log)
            if polling:
                log("步骤完成，等待自动接收手机保存的图片…")
                return paths
            log("正在查找手机里刚保存的图片…")
            for item in bridge.scan_images():
                if item.key in before:
                    continue
                paths.append(bridge.pull(item.remote,
                                         bridge.unique_dest(item.remote)))
            return paths
        self._run_phone_action(task, self._enqueue_ocr)

    def _phone_mirror(self) -> None:
        def task(log):
            log("正在打开 scrcpy 投屏窗口…")
            PhoneBridge().open_mirror()
            return []
        self._run_phone_action(
            task, lambda _: self.statusBar().showMessage(
                "已打开投屏窗口，可在电脑上直接操作手机", 6000))

    def _phone_inject_check(self) -> None:
        def task(log):
            bridge = PhoneBridge()
            bridge.attach()
            log("正在检测电脑能否模拟点击手机…")
            try:
                bridge._try_input("input keyevent 0")
            except PhoneError:
                pass
            if bridge.inject_backend != "input":
                # 空事件脚本：只验证 monkey 通道本身可用，不产生任何点击
                bridge._run_monkey(["UserWait(1)"])
            return bridge.injection_status()
        self._run_phone_action(
            task, lambda text: (
                self._phone_status(f"● 注入自检：{text}"),
                QMessageBox.information(
                    self, "注入自检",
                    f"当前可用的点击方式：{text}\n\n"
                    "红米 Turbo 5 Max 被 HyperOS 拦截时会自动改用 monkey 脚本，\n"
                    "不需要打开「USB 调试（安全设置）」；华为 P30 Pro 已验证可用原生 input。\n"
                    "两种通道都支持全自动取报告与录制点按步骤。")))

    # ---------- 全自动取报告 ----------
    def _auto_toggle(self) -> None:
        if self._auto_worker and self._auto_worker.isRunning():
            self._auto_worker.stop()
            self.btn_auto.setText("正在停止…")
            self.btn_auto.setEnabled(False)
            self.statusBar().showMessage("正在停止，完成当前一步后立即结束", 6000)
            return
        limit = int(self.spin_limit.value())
        answer = QMessageBox.question(
            self, APP_NAME,
            f"接下来会自动在手机上点击「分享 → 保存」，最多 {limit} 条。\n\n"
            "请先确认：\n"
            "① 打开华为运动健康 → 点「体重」卡片 → 点「所有数据」进历史列表\n"
            "   （也可以直接停在任意一条「测量记录」详情页）\n"
            "② 屏幕已解锁、保持亮屏，中途不要切到别的 App\n\n"
            "只有体重、没有体脂的普通称重不会报错中断；勾了「普通称重也收进清单」时，\n"
            "它们会以「仅体重」记录进清单，否则只是跳过。「最多 N 条」只统计真正出报告的记录。\n\n"
            "翻页已改成每次约 1 行并强制前后重叠，不会再滑过一屏丢记录。\n\n"
            "全程走本机 USB，不联网、不上传；点「是」开始，随时可再点按钮停止。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            bridge = PhoneBridge()
        except PhoneError as exc:
            self._phone_error(str(exc))
            return
        worker = AutoCollectWorker(
            bridge, limit,
            collect_weight_only=self.chk_weight_only.isChecked())
        worker.log.connect(self._auto_log)
        worker.image.connect(self._auto_image)
        worker.records.connect(self._add_structured_records)
        worker.done.connect(self._auto_done)
        worker.failed.connect(self._auto_failed)
        self._auto_worker = worker
        self.btn_auto.setText("■ 停止自动取图")
        self.spin_limit.setEnabled(False)
        self._phone_status("● 全自动取报告中…")
        worker.start()

    def _auto_log(self, text: str) -> None:
        self.statusBar().showMessage(text, 8000)
        self._phone_status(f"● {text}")

    def _auto_image(self, path) -> None:
        path = Path(path)
        self._auto_paths.add(path)
        self._enqueue_ocr([path])

    def _auto_finish_ui(self) -> None:
        self.btn_auto.setText("★ 全自动取报告")
        self.btn_auto.setEnabled(True)
        self.spin_limit.setEnabled(True)

    def _auto_done(self, detail: str) -> None:
        self._auto_finish_ui()
        self._phone_status(f"● 全自动取图完成 · {detail}")
        QMessageBox.information(
            self, APP_NAME,
            f"全自动取报告完成。\n\n{detail}\n\n"
            "拉回来的图片已经在清单里（离线 OCR 会自动核对），"
            "确认无误后点「生成佳明文件」即可。")

    def _auto_failed(self, message: str) -> None:
        self._auto_finish_ui()
        self._phone_error(message)

    # ---------- 快速清单：控件树直读 ----------
    def _add_structured_records(self, records) -> None:
        """批量把结构化记录（含只有体重的普通称重）加进待导入清单。"""
        added = 0
        for record in records or []:
            if not isinstance(record, Measurement):
                continue
            self.entries.append(Entry.from_measurement(record))
            added += 1
        if added:
            self._refresh_list()
            self._select(len(self.entries) - 1)
            self.statusBar().showMessage(
                f"已从手机控件树读取 {added} 条记录（无需 OCR）", 9000)

    def _add_one_structured_record(self, record) -> None:
        """详情页每读一条就入表一条 —— 采集中表格实时显示，不再等全部读完。"""
        if not isinstance(record, Measurement):
            return
        self.entries.append(Entry.from_measurement(record))
        self._refresh_list()
        self.statusBar().showMessage(
            f"已读取 {len(self.entries)} 条记录，正在继续…", 4000)

    def _upsert_streamed(self, record: Measurement) -> bool:
        """快速清单单条入表：同一条记录被更全的版本读到时原地更新。

        滚动时同一条历史可能出现「只有体重」和「带脂肪率」两个版本，按
        「时间+体重」定位到同一行，保留字段更全的那份，避免表格里出现重复行。
        """
        entry = Entry.from_measurement(record)
        key = dedup_key(entry)
        index = self._fast_index.get(key) if key else None
        if index is not None and index < len(self.entries):
            current = self.entries[index]
            if entry_richness(entry) > entry_richness(current):
                entry.checked = entry.checked or current.checked
                self.entries[index] = entry
            return False
        if key is not None:
            self._fast_index[key] = len(self.entries)
        self.entries.append(entry)
        return True

    def _add_streamed_record(self, record) -> None:
        """快速清单每读到一条就刷新表格，不用等整份历史读完。"""
        if not isinstance(record, Measurement):
            return
        added = self._upsert_streamed(record)
        if added:
            self._refresh_list()
        self.statusBar().showMessage(
            f"已读取 {len(self._fast_index)} 条记录，正在继续…", 4000)

    def _sort_entries_by_time(self) -> None:
        """按测量时间倒序整理清单（最新在上），没有时间的记录留在末尾。

        华为「所有数据」列表本身并不保证按测量时间排序（实测顶部是 2024/4/7，
        中部却出现 2025/4/10，按同步顺序穿插），照抄列表顺序会让人误以为
        年份读错了；这里统一按真实测量时间整理一遍。
        """
        def stamp(entry: Entry) -> str:
            return fmt(entry.values.get("timestamp"))

        timed = [entry for entry in self.entries if stamp(entry)]
        untimed = [entry for entry in self.entries if not stamp(entry)]
        timed.sort(key=stamp, reverse=True)
        self.entries[:] = timed + untimed

    def _finish_streamed_records(self, records) -> None:
        """收尾：用最终合并结果补齐/更新表格，按时间整理后展示。"""
        for record in records or []:
            if isinstance(record, Measurement):
                self._upsert_streamed(record)
        if self._fast_index:
            self._sort_entries_by_time()
            self._refresh_list()
            self._select(0)
            self.statusBar().showMessage(
                f"已从手机控件树读取 {len(self._fast_index)} 条记录（无需 OCR），"
                "已按测量时间从新到旧整理", 9000)

    def _fast_toggle(self) -> None:
        if self._fast_worker and self._fast_worker.isRunning():
            self._fast_worker.stop()
            self.btn_fast.setText("正在停止…")
            self.btn_fast.setEnabled(False)
            self.statusBar().showMessage("正在停止，完成当前一屏后立即结束", 6000)
            return
        if self._auto_worker and self._auto_worker.isRunning():
            QMessageBox.information(self, APP_NAME,
                                    "「全自动取报告」正在运行，请先停止它。")
            return
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            "「快速清单」直接从「所有数据」列表读结构化数字：\n\n"
            "· 不逐条点分享、不生成长图、不做 OCR，一次翻完整个历史\n"
            "· 每条拿到：测量时间 / 体重 / 脂肪率 / 骨骼肌量\n"
            "· 只有体重、没有体脂的普通称重也会一起收进来\n"
            "· 翻页每次约 1 行并强制前后重叠，不会漏记录\n\n"
            "请选择从哪里开始：\n"
            "· 【从当前位置】接着你手机上现在停留的地方往下读（推荐）\n"
            "· 【从头开始】先滚回顶部，把全部历史读一遍\n\n"
            "需要 14 项完整体成分时，仍请用「★ 全自动取报告」。\n"
            "请先打开：华为运动健康 → 体重 →「所有数据」列表。\n"
            "全程本机 USB，不联网、不上传。")
        btn_here = box.addButton("从当前位置", QMessageBox.ButtonRole.YesRole)
        btn_top = box.addButton("从头开始", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (btn_here, btn_top):
            return
        seek_top = clicked is btn_top
        try:
            bridge = PhoneBridge()
        except PhoneError as exc:
            self._phone_error(str(exc))
            return
        self._fast_index = {}
        worker = FastCollectWorker(bridge, seek_top=seek_top)
        worker.log.connect(self._auto_log)
        worker.row.connect(self._add_streamed_record)
        worker.records.connect(self._finish_streamed_records)
        worker.done.connect(self._fast_done)
        worker.failed.connect(self._fast_failed)
        self._fast_worker = worker
        self.btn_fast.setText("■ 停止快速清单")
        self.btn_auto.setEnabled(False)
        self._phone_status("● 控件树直读中…")
        worker.start()

    def _fast_finish_ui(self) -> None:
        self.btn_fast.setText("⚡ 快速清单（控件树直读）")
        self.btn_fast.setEnabled(True)
        self.btn_auto.setEnabled(True)

    def _fast_done(self, detail: str) -> None:
        self._fast_finish_ui()
        self._phone_status(f"● 快速清单完成 · {detail}")
        if QMessageBox.question(
                self, APP_NAME,
                f"{detail}\n\n记录已加入待导入清单，无需逐条核对。\n"
                "现在就生成佳明文件吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            self._export()

    def _fast_failed(self, message: str) -> None:
        self._fast_finish_ui()
        self._phone_error(message)

    # ---------- 详情全量：控件树直读 14 项 ----------
    def _detail_toggle(self) -> None:
        if self._detail_worker and self._detail_worker.isRunning():
            self._detail_worker.stop()
            self.btn_detail.setText("正在停止…")
            self.btn_detail.setEnabled(False)
            self.statusBar().showMessage("正在停止，读完当前一条后立即结束", 6000)
            return
        for worker, name in ((self._auto_worker, "全自动取报告"),
                             (self._fast_worker, "快速清单")):
            if worker and worker.isRunning():
                QMessageBox.information(self, APP_NAME,
                                        f"「{name}」正在运行，请先停止它。")
                return
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            "「详情全量」逐条点开测量记录，直接读详情页控件树里的完整数字：\n\n"
            "· 每条拿到 14 项：体重/脂肪率/BMI/骨骼肌量/水分率/骨盐量/\n"
            "  内脏脂肪/基础代谢/蛋白质/身体年龄/身体得分…\n"
            "· 不分享、不生成长图、不做 OCR，不受识别误差影响\n"
            "· 已优化为「一屏直读」：手机屏幕够大，指标一屏就全，\n"
            "  通常每条约 9 秒（主要在等手机读屏，属正常）\n"
            "· 只有体重的普通称重也按「仅体重」收进来\n"
            "· 读一条就入表一条，表格实时显示，不用等全部读完\n\n"
            "请选择从哪里开始：\n"
            "· 【从当前位置】接着你手机上现在停留的地方往下读（推荐）\n"
            "· 【从头开始】先滚回顶部，把全部历史读一遍\n\n"
            "请先打开：华为运动健康 → 体重 →「所有数据」列表。\n"
            "全程本机 USB，不联网、不上传。")
        btn_here = box.addButton("从当前位置", QMessageBox.ButtonRole.YesRole)
        btn_top = box.addButton("从头开始", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (btn_here, btn_top):
            return
        seek_top = clicked is btn_top
        try:
            bridge = PhoneBridge()
        except PhoneError as exc:
            self._phone_error(str(exc))
            return
        worker = DetailCollectWorker(bridge, seek_top=seek_top)
        worker.log.connect(self._auto_log)
        worker.row.connect(self._add_one_structured_record)
        worker.done.connect(self._detail_done)
        worker.failed.connect(self._detail_failed)
        self._detail_worker = worker
        self.btn_detail.setText("■ 停止详情全量")
        self.btn_auto.setEnabled(False)
        self.btn_fast.setEnabled(False)
        self._phone_status("● 详情页控件树直读中…")
        worker.start()

    def _detail_finish_ui(self) -> None:
        self.btn_detail.setText("🔍 详情全量（控件树直读）")
        self.btn_detail.setEnabled(True)
        self.btn_auto.setEnabled(True)
        self.btn_fast.setEnabled(True)

    def _detail_done(self, detail: str) -> None:
        self._detail_finish_ui()
        self._phone_status(f"● 详情全量完成 · {detail}")
        if QMessageBox.question(
                self, APP_NAME,
                f"{detail}\n\n记录已加入待导入清单，无需逐条核对。\n"
                "现在就生成佳明文件吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            self._export()

    def _detail_failed(self, message: str) -> None:
        self._detail_finish_ui()
        self._phone_error(message)

    def _enqueue_ocr(self, paths) -> None:
        """把手机取到的图片排队送进现有 OCR 流水线（识别忙时不丢图）。"""
        fresh = [Path(p) for p in (paths or [])
                 if p and Path(p).exists() and Path(p) not in self._seen_phone_paths]
        if not fresh:
            return
        self._seen_phone_paths.update(fresh)
        if self._worker and self._worker.isRunning():
            self._pending.extend(fresh)
            self.statusBar().showMessage(
                f"已排队 {len(fresh)} 张，识别完当前批次后继续", 6000)
        else:
            self._start_ocr(fresh)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._phone_poll and self._phone_poll.isRunning():
            self._phone_poll.stop()
            self._phone_poll.wait(3000)
        if self._auto_worker and self._auto_worker.isRunning():
            self._auto_worker.stop()
            self._auto_worker.wait(3000)
        if self._fast_worker and self._fast_worker.isRunning():
            self._fast_worker.stop()
            self._fast_worker.wait(3000)
        if self._detail_worker and self._detail_worker.isRunning():
            self._detail_worker.stop()
            self._detail_worker.wait(3000)
        if self._phone_action and self._phone_action.isRunning():
            self._phone_action.wait(3000)
        super().closeEvent(event)

    # ---------- 输入 ----------
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls()]
        images = [p for p in paths if p.suffix.lower() in IMAGE_SUFFIXES and p.exists()]
        if images:
            self._start_ocr(images)
        else:
            self.statusBar().showMessage("拖进来的不是图片文件", 4000)

    def _pick_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(
            self, "选择体成分报告截图", str(Path.home()),
            "图片 (*.jpg *.jpeg *.png *.bmp *.webp *.tif *.tiff)")
        if names:
            self._start_ocr([Path(n) for n in names])

    def _start_ocr(self, paths: list[Path], replace_row: int | None = None) -> None:
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, APP_NAME, "还有图片正在识别，请稍等。")
            return
        self._replace_row = replace_row
        self.progress.setVisible(True)
        self.progress.setRange(0, len(paths))
        self.progress.setValue(0)
        self.btn_pick.setEnabled(False)
        self._worker = OcrWorker(paths, self.engine_pref)
        self._worker.progress.connect(self._on_progress)
        self._worker.ready.connect(self._on_entry_ready)
        self._worker.finished.connect(self._on_ocr_done)
        self._worker.start()

    def _on_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setValue(done)
        self.statusBar().showMessage(f"正在识别 {done}/{total}：{name}")

    def _on_entry_ready(self, entry: Entry, engine: str, error: str) -> None:
        if entry.path in self._auto_paths:
            self._auto_paths.discard(entry.path)
            if not looks_like_report(entry):
                self.statusBar().showMessage(
                    f"手机新图 {entry.path.name} 不像体成分报告，已忽略", 7000)
                return
        if self._replace_row is not None and 0 <= self._replace_row < len(self.entries):
            row = self._replace_row
            self.entries[row] = entry
            self._replace_row = None
        else:
            self.entries.append(entry)
            row = len(self.entries) - 1
        self._refresh_list()
        self._select(row)
        if error:
            self.statusBar().showMessage(f"{engine}：{error}", 6000)

    def _on_ocr_done(self) -> None:
        self.progress.setVisible(False)
        self.btn_pick.setEnabled(True)
        bad = sum(1 for e in self.entries if not e.valid)
        if self.entries:
            self.statusBar().showMessage(
                f"识别完成：共 {len(self.entries)} 条，{bad} 条需要核对（点清单里的行改数字）", 8000)
        self._refresh_list()
        if self._pending:
            QTimer.singleShot(0, self._drain_pending)

    def _drain_pending(self) -> None:
        if not self._pending or (self._worker and self._worker.isRunning()):
            return
        paths, self._pending = self._pending, []
        self._start_ocr(paths)

    def _rescan_current(self) -> None:
        if self.current < 0 or not self.entries[self.current].path:
            QMessageBox.information(
                self, APP_NAME,
                "这条是手工录入或控件树直读的记录，没有原图；"
                "直接在右侧表格里改数字即可。")
            return
        self._start_ocr([self.entries[self.current].path], replace_row=self.current)

    def _add_manual(self) -> None:
        entry = Entry(None, {key: "" for key, *_ in FIELDS},
                      None, [], "手工录入", "")
        entry.values["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry.refresh()
        self.entries.append(entry)
        self._refresh_list()
        self._select(len(self.entries) - 1)
        self.statusBar().showMessage(
            "已新增空白记录：在右侧填体重和四组分，状态会实时重算", 8000)

    # ---------- 清单 / 字段联动 ----------
    def _refresh_list(self) -> None:
        self._loading = True
        self.listing.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Checked if entry.checked
                                else Qt.CheckState.Unchecked)
            self.listing.setItem(row, 0, check)
            source = (entry.path.name if entry.path else
                      entry.source or "手工录入")
            values = [entry.time_text,
                      fmt(entry.values.get("weight_kg")),
                      fmt(entry.values.get("fat_pct")),
                      "通过" if entry.valid else "需核对",
                      source]
            for col, value in enumerate(values, 1):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col == 4:
                    item.setForeground(QColor("#137a4b" if entry.valid else "#b54708"))
                self.listing.setItem(row, col, item)
        self._loading = False

    def _on_list_clicked(self, row: int, _col: int) -> None:
        self._select(row)

    def _on_list_check_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != 0:
            return
        row = item.row()
        if 0 <= row < len(self.entries):
            self.entries[row].checked = item.checkState() == Qt.CheckState.Checked

    def _select(self, row: int) -> None:
        if not (0 <= row < len(self.entries)):
            return
        self.current = row
        entry = self.entries[row]
        self.preview.load(entry.path)
        self._loading = True
        self.fields.blockSignals(True)
        for i, (key, *_rest) in enumerate(FIELDS):
            self.fields.item(i, 1).setText(fmt(entry.values.get(key)))
        self.fields.blockSignals(False)
        self._loading = False
        self._refresh_checks()

    def _on_field_edited(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != 1 or self.current < 0:
            return
        entry = self.entries[self.current]
        key = FIELDS[item.row()][0]
        entry.values[key] = item.text().strip()
        if entry.measurement is not None:
            sync_structured_values(entry)
        else:
            entry.refresh()
        self._refresh_checks()
        self._refresh_list()

    def _on_field_clicked(self, row: int, _col: int) -> None:
        if self.current < 0:
            return
        entry = self.entries[self.current]
        value = self.fields.item(row, 1).text().strip()
        if not value or not entry.boxes:
            self.preview.highlight(None)
            return
        for box in entry.boxes:
            if value in box.text or value.replace(".", "") in box.text.replace(".", ""):
                self.preview.highlight(box)
                return
        self.preview.highlight(None)

    def _refresh_checks(self) -> None:
        if self.current < 0:
            return
        entry = self.entries[self.current]
        if entry.measurement is not None:
            # 结构化记录来自 App 自己渲染的数字，不是 OCR 猜测，不走四组分方程
            known = {key for key in ("timestamp", "weight_kg", "fat_pct",
                                     "skeletal_muscle_kg", "bmi")
                     if str(entry.values.get(key, "")).strip()}
            self._loading = True
            self.fields.blockSignals(True)
            for row, (key, *_rest) in enumerate(FIELDS):
                hit = key in known
                self.fields.item(row, 2).setText("OK" if hit else "—")
                self.fields.item(row, 2).setForeground(
                    QColor("#137a4b" if hit else "#9aa8b8"))
                self.fields.item(row, 3).setText(
                    "控件树直读 · 可直接导出" if hit else "该指标列表未展示，导入后为空")
            self.fields.blockSignals(False)
            self._loading = False
            self.check_label.setText(
                "✓ 控件树直读：数值来自华为 App 界面文本，无需 OCR 与方程校验"
                + ("（普通称重：只有体重）" if entry.measurement.fat_pct is None else ""))
            self.check_label.setObjectName("ok")
            self.check_label.setStyleSheet("color:#137a4b;font-weight:700;")
            return
        result = entry.result
        by_key = {c.key: c for c in result.checks} if result else {}
        self._loading = True
        self.fields.blockSignals(True)
        for row, (key, *_rest) in enumerate(FIELDS):
            check = by_key.get(key)
            status, note = "", ""
            if check:
                status = {"ok": "OK", "derived": "推导", "suspicious": "可疑",
                          "missing": "缺失", "check": "请核对"}.get(check.status, "")
                note = check.note
            if key in ("timestamp", "weight_kg"):
                status = "OK" if fmt(self.entries[self.current].values.get(key)) else "必填"
                note = "" if status == "OK" else "缺这个不能导出"
            cell = self.fields.item(row, 2)
            cell.setText(status)
            color = {"OK": "#137a4b", "推导": "#1668c1"}.get(status, "#b54708")
            cell.setForeground(QColor(color))
            self.fields.item(row, 3).setText(note or FIELDS[row][3])
        self.fields.blockSignals(False)
        self._loading = False
        if result and result.equation_ok and not result.requires_review():
            self.check_label.setText("✓ 方程校验通过：四组分之和 = 体重，百分比自洽")
            self.check_label.setObjectName("ok")
        else:
            reason = (result.warnings[0] if result and result.warnings
                      else "有字段待核对")
            self.check_label.setText("⚠ " + reason + " · 请对照左图改红字")
            self.check_label.setObjectName("warn")
        self.check_label.setStyleSheet(
            "color:#137a4b;font-weight:700;" if result and result.equation_ok and
            not result.requires_review() else "color:#b54708;font-weight:700;")

    def _remove_selected(self) -> None:
        row = self.listing.currentRow()
        if 0 <= row < len(self.entries):
            self.entries.pop(row)
            self.current = -1 if not self.entries else min(row, len(self.entries) - 1)
            self._refresh_list()
            if self.current >= 0:
                self._select(self.current)

    def _deduplicate(self) -> None:
        """按「测量时间+体重」去重，去掉重复采集，保留字段最全的一条。"""
        if len(self.entries) < 2:
            QMessageBox.information(self, APP_NAME, "清单不足两条，无需去重。")
            return
        kept, removed = dedup_entries(self.entries)
        if removed <= 0:
            QMessageBox.information(self, APP_NAME, "没有发现重复记录。")
            return
        answer = QMessageBox.question(
            self, APP_NAME,
            f"发现 {removed} 条重复采集（测量时间+体重相同）。\n\n"
            f"将保留字段最全的一条并合并勾选状态，删除其余 {removed} 条，"
            f"清单从 {len(self.entries)} 条变为 {len(kept)} 条。\n\n"
            "确定去重？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        old_current_time = (self.entries[self.current].values.get("timestamp")
                            if 0 <= self.current < len(self.entries) else None)
        self.entries = kept
        # 尽量把选中行落到原记录上（按时间匹配），否则回到第一条
        self.current = next((i for i, e in enumerate(self.entries)
                             if e.values.get("timestamp") == old_current_time), -1)
        if self.current < 0 and self.entries:
            self.current = 0
        self._refresh_list()
        if self.current >= 0:
            self._select(self.current)
        self.statusBar().showMessage(
            f"已去除 {removed} 条重复采集，剩余 {len(self.entries)} 条", 8000)

    def _clear_all(self) -> None:
        if self.entries and QMessageBox.question(
                self, APP_NAME, "清空全部记录？") == QMessageBox.StandardButton.Yes:
            self.entries.clear()
            self.current = -1
            self._refresh_list()
            self.preview.load(None)
            self.check_label.setText("等待图片…")

    # ---------- 导出 ----------
    def _height_m(self) -> float | None:
        value = float(self.height.value())
        return value / 100 if value > 3 else None

    def _export(self) -> None:
        selected = [e for e in self.entries if e.checked]
        if not selected:
            QMessageBox.information(self, APP_NAME, "清单里没有勾选任何记录。")
            return
        bad = [e for e in selected if not e.valid]
        if bad and QMessageBox.question(
                self, APP_NAME,
                f"有 {len(bad)} 条未通过方程校验，识别数值可能有误。\n"
                "建议先点清单里的行对照原图改数字。\n\n仍要强制导出吗？"
        ) != QMessageBox.StandardButton.Yes:
            return
        records = []
        try:
            for entry in selected:
                if entry.measurement is not None:
                    records.append(entry.measurement)  # 控件树直读 / 仅体重
                elif entry.result is not None:
                    records.append(to_measurement(entry.result))
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, f"有记录缺少测量时间或体重：{exc}")
            return
        # 同一时刻若既有控件树记录又有 OCR 全量报告，优先保留字段更全的那条
        records = merge_rich(records)
        try:
            paths = generate_files(
                records, height_m=self._height_m(),
                muscle="total" if self.muscle.currentIndex() == 1 else "skeletal",
                garmin_url=self._garmin_url())
        except (RuntimeError, OSError) as exc:
            QMessageBox.warning(self, APP_NAME, f"导出失败：{exc}")
            return
        self.statusBar().showMessage(
            f"已生成 {len(records)} 条 → {OUTPUT_DIR}", 12000)
        QMessageBox.information(
            self, APP_NAME,
            f"已生成 {len(records)} 条记录：\n\n"
            f"{paths['csv']}\n{paths['fit']}\n\n"
            "接下来会自动打开输出文件夹。把 FIT 或 CSV 拖到佳明网页的导入区即可。")
        self._open_folder()

    def _garmin_url(self) -> str:
        base = "connect.garmin.com" if self.region.currentIndex() == 1 else "connect.garmin.cn"
        return f"https://{base}/modern/import-data"

    def _open_folder(self) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(OUTPUT_DIR)))

    def _open_garmin(self) -> None:
        QDesktopServices.openUrl(QUrl(self._garmin_url()))


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    try:
        log = LOG_DIR
        log.mkdir(parents=True, exist_ok=True)
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        (log / "error.log").write_text(text, encoding="utf-8")
    except OSError:
        log = LOG_DIR
    try:
        QMessageBox.critical(None, APP_NAME,
                             f"程序出错，详情已写入 {log / 'error.log'}\n\n{exc_value}")
    except Exception:
        pass


def _asset_path(name: str) -> Path:
    """图标等只读资源：打包后在 _internal/assets，源码运行时在 desktop/assets。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "assets" / name
    return Path(__file__).resolve().parent / "assets" / name


def configure_app(app: QApplication) -> None:
    """统一样式入口；GUI 自检也走这里，避免测出一个没上样式的假界面。"""
    app.setApplicationName(APP_NAME)
    # 显式设置窗口/任务栏图标：只依赖 exe 内嵌图标时，任务栏偶尔会退回默认图标
    icon = _asset_path("app.ico")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    app.setStyle("Fusion")
    # 兜底：Fusion 默认「蓝底白字」在浅色自定义样式里会造成选中项白字白底，
    # 下拉弹层已单独指定配色，这里再统一高亮色，表格/列表选中也不会踩坑。
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#dbeafe"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#16324f"))
    app.setPalette(palette)
    app.setStyleSheet(STYLE)


def main() -> int:
    sys.excepthook = _excepthook
    app = QApplication(sys.argv)
    configure_app(app)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
