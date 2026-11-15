"""桌面程序自检。用 desktop/.venv/Scripts/python.exe 运行：

    desktop/.venv/Scripts/python.exe desktop/selftest.py           # 逻辑 + 离线 OCR
    desktop/.venv/Scripts/python.exe desktop/selftest.py --gui     # 外加离屏 GUI 冒烟

验证：键值草稿闭环、RapidOCR 断网可用、导出 FIT 与命令行产物一致、
全自动取报告状态机（假手机，含「只有体重」普通称重自动跳过）、
控件树直读快速清单与小步重叠翻页、详情页直读 14 项全量、离屏 GUI 冒烟。
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent
ROOT = DESKTOP.parent
for p in (str(ROOT), str(DESKTOP)):
    if p not in sys.path:
        sys.path.insert(0, p)

from sync_tool.fit import build_weight_fit, parse_weight_scale_fit
from sync_tool.garmin_csv import build_garmin_csv
from sync_tool.models import Measurement, parse_time
from sync_tool.report import parse_report_text, to_measurement

from main import (Entry, FIELDS, OUTPUT_DIR, PhonePollWorker, build_kv_text,
                  configure_app, generate_files, looks_like_report,
                  values_from_result)
from ocr_engine import OcrEngine, probe, to_layout_text

SAMPLE_TXT = ROOT / "samples" / "huawei_report_20261112.txt"
SAMPLE_IMG = ROOT / "samples" / "huawei_report_20261112.jpg"
EXPECTED = {"水分": 39.94, "蛋白质": 19.49, "脂肪量": 24.62, "骨盐量": 3.25,
            "体重": 87.3, "脂肪率": 28.2, "水分率": 45.8}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print("  [OK] " + message)


def test_roundtrip() -> None:
    print("\n[1] 键值草稿闭环")
    text = SAMPLE_TXT.read_text(encoding="utf-8")
    result = parse_report_text(text)
    check(result.equation_ok, "样例方程自洽")
    values = values_from_result(result)
    again = parse_report_text(build_kv_text(values))
    check(again.equation_ok and not again.requires_review(),
          "桌面表格生成的键值文本重新解析仍通过全部校验")
    check(len(FIELDS) == 16, f"字段表 {len(FIELDS)} 行")


def test_offline_ocr() -> None:
    print("\n[2] RapidOCR 本地离线识别")
    info = probe()
    print("  探测：", info)
    if not (info.get("rapid") and info.get("onnx")):
        print("  [SKIP] 离线 OCR 未安装，跳过（GUI 会降级到 Windows 内置 OCR）")
        return
    real_socket = socket.socket

    class Blocked(socket.socket):
        def __init__(self, *args, **kwargs):
            raise OSError("network disabled for offline OCR verification")

    socket.socket = Blocked
    try:
        engine = OcrEngine("rapid")
        check(engine.kind == "rapid", f"引擎已加载：{engine.detail}")
        outcome = engine.recognize(SAMPLE_IMG)
        check(outcome.ok, f"断网状态下识别成功：{len(outcome.boxes)} 个文本框"
                          + (f" / {outcome.error}" if outcome.error else ""))
        layout = to_layout_text(outcome.boxes, y_factor=outcome.y_factor)
        parsed = parse_report_text(layout, source_kind="ocr")
        print("  OCR 解析出的关键读数：")
        for label in ("weight_kg", "bmi", "fat_pct", "water_pct",
                      "basal_met", "skeletal_muscle_kg"):
            print(f"    {label:20} {parsed.raw.get(label)}")
        print("  四组分采纳值：", parsed.masses)
        check(parsed.raw.get("weight_kg") == 87.3, "体重 87.3 识别正确")
        check(parsed.raw.get("bmi") == 27.2, "BMI 27.2 没有串到同行右侧的骨骼肌量")
        check(parsed.raw.get("water_pct") == 45.8, "水分率 45.8 没有串到骨盐量")
        check(parsed.raw.get("basal_met") == 1780, "基础代谢 1,780 的千分位逗号已处理")
        check(parsed.masses.get("water_mass_kg") == 39.94 and
              parsed.masses.get("protein_mass_kg") == 19.49 and
              parsed.masses.get("fat_mass_kg") == 24.62 and
              parsed.masses.get("bone_mass_kg") == 3.25,
              "四组分全部识别正确")
        check(parsed.equation_ok, "四组分方程闭合、无需人工改数")
        check(not parsed.requires_review(), "无待复核字段，可直接导出")
    finally:
        socket.socket = real_socket
    print("  [OK] 全程无网络请求（socket 被禁用仍能识别）")


def test_export_identical() -> None:
    print("\n[3] 导出与命令行产物一致")
    result = parse_report_text(SAMPLE_TXT.read_text(encoding="utf-8"))
    record = to_measurement(result)
    fit = build_weight_fit([record], height_m=1.792, muscle="skeletal",
                           include_metabolic_age=False)
    reference = ROOT / "out" / "report" / "garmin_weight.fit"
    if reference.exists():
        check(fit == reference.read_bytes(),
              f"FIT 与 CLI 产物逐字节一致（{len(fit)} 字节）")
    check(len(parse_weight_scale_fit(fit)) == 1, "FIT 回读校验通过")
    csv = build_garmin_csv([record], height_m=1.792)
    check("2026-11-12,87.3,27.2,28.2" in csv, "Body CSV 数据行正确")
    paths = generate_files([record], height_m=1.792, muscle="skeletal",
                           garmin_url="https://connect.garmin.cn/modern/import-data")
    check(paths["fit"].read_bytes() == fit, "generate_files 落盘的 FIT 与内存结果一致")
    check("2026-11-12,87.3,27.2,28.2" in
          paths["csv"].read_text(encoding="utf-8-sig"), "落盘 CSV 数据行正确")
    check(paths["readme"].exists() and "connect.garmin.cn" in
          paths["readme"].read_text(encoding="utf-8-sig"), "导入说明已生成")


def test_auto_collect() -> None:
    print("\n[6] 全自动取报告状态机（假手机，零联网）")
    sys.path.insert(0, str(ROOT / "tests"))
    from test_phone import FakeAutoBridge
    from auto_collect import AutoCollector

    fake = FakeAutoBridge()
    events: list[str] = []
    collector = AutoCollector(
        fake, limit=2, max_scrolls=1,
        on_event=lambda kind, text, path: events.append(text),
        sleep=lambda _: None)
    paths = collector.run()
    check(len(paths) == 2 and fake.save_count == 2,
          "逐条打开 → 分享 → 保存 → 拉回，共 2 条")
    check(len({p.read_bytes() for p in paths}) == 2,
          "两条记录内容不同、没有重复")
    check(any("返回" in text for text in events),
          "保存后通过页面「返回」按钮回到历史列表")

    mixed = FakeAutoBridge(weights=("86.2", "86.6", "86.8"),
                           bodyfat_flags=(False, True, True))
    events2: list[str] = []
    second = AutoCollector(
        mixed, limit=5, max_scrolls=1,
        on_event=lambda kind, text, path: events2.append(text),
        sleep=lambda _: None)
    paths2 = second.run()
    check(len(paths2) == 2 and second.saved == 2 and second.skipped == 1,
          "只有体重、没有体脂的普通称重自动跳过且不中断，后面两条继续导出")
    check(any("跳过 1 条" in text for text in events2),
          "完成提示单独统计跳过的普通称重条数")

    sys.path.insert(0, str(DESKTOP))
    from phone import is_weight_only_detail, parse_ui_xml
    fixtures = ROOT / "tests" / "fixtures"
    weight_only = parse_ui_xml((fixtures / "phone_detail_weightonly_real.xml")
                               .read_text(encoding="utf-8"))
    bodyfat = parse_ui_xml((fixtures / "phone_detail_bodyfat_real.xml")
                           .read_text(encoding="utf-8"))
    check(is_weight_only_detail(weight_only) and not is_weight_only_detail(bodyfat),
          "真机详情页快照：无体脂判跳过、有体脂判导出，不误伤")


def test_fast_channel() -> None:
    print("\n[7] 控件树直读 · 小步重叠翻页 · 仅体重兼容")
    sys.path.insert(0, str(ROOT / "tests"))
    from test_history_fast import PagedBridge
    from fast_collect import FastCollector
    from history_export import rows_to_measurements
    from phone import history_rows, parse_ui_xml, scroll_distance_px

    fixtures = ROOT / "tests" / "fixtures"
    rows = history_rows(parse_ui_xml(
        (fixtures / "phone_history_real.xml").read_text(encoding="utf-8")))
    distance = scroll_distance_px(rows, 2772)
    check(distance <= 312 and distance < 2772 * 0.25,
          f"翻页步长 {distance}px（约 1 行），取代原来的半屏大滑")
    real = rows_to_measurements(rows)
    check(len(real) == 6 and sum(1 for r in real if r.fat_pct is None) == 4,
          "真机快照：6 条全部转成记录，其中 4 条只有体重也照常写入")
    csv = build_garmin_csv(real)
    check("2026-09-25,86.2,," in csv, "仅体重记录在 Body CSV 里 BMI/Fat 留空")
    parsed = parse_weight_scale_fit(build_weight_fit(real))
    check(len(parsed) == 6 and parsed[-1]["weight"] == 86.2,
          "FIT 回读 6 条全部有效，无体脂记录只有 weight 字段")
    import tempfile
    from main import generate_files
    with tempfile.TemporaryDirectory(prefix="fast-export-") as tmp:
        paths = generate_files(
            real, height_m=None, muscle="skeletal",
            garmin_url="https://connect.garmin.cn/modern/import-data",
            out_dir=Path(tmp))
        check(len(parse_weight_scale_fit(paths["fit"].read_bytes())) == 6,
              "桌面「生成佳明文件」对混合清单（含仅体重）同样可落盘")

    bridge = PagedBridge(count=24)
    collector = FastCollector(bridge, max_scrolls=200, seek_top=False,
                              on_event=lambda kind, text: None,
                              sleep=lambda _: None)
    paged = collector.run()
    check(len(paged) == 24 and len(collector.rows) == 24,
          "30 行级假手机分多屏读完 24 条，一条不漏（旧逻辑会整屏跳过）")
    check(all(d <= 468 for d in collector.walker.distances),
          "每次滚动都在 1～1.5 行量级，前后两屏必有重叠")
    check(collector.weight_only_count == 12,
          "普通称重被识别并保留，不是丢弃")


def test_detail_channel() -> None:
    print("\n[8] 详情页控件树直读 · 14 项全量")
    from detail_export import detail_to_measurement, parse_number
    from phone import parse_ui_xml

    fixtures = ROOT / "tests" / "fixtures"
    bodyfat = parse_ui_xml((fixtures / "phone_detail_bodyfat_real.xml")
                           .read_text(encoding="utf-8"))
    weight_only = parse_ui_xml((fixtures / "phone_detail_weightonly_real.xml")
                               .read_text(encoding="utf-8"))
    check(parse_number("1,767") == 1767.0,
          "千分位逗号正确识别为基础代谢 1767，而非 1.767")
    record = detail_to_measurement(bodyfat)
    check(record is not None and record.fat_pct == 28.1
          and record.basal_met == 1767.0 and record.visceral_fat == 13.0
          and record.body_score == 70.0,
          "真机详情页快照：14 项全量（含 BMI/内脏脂肪/身体得分）解析正确")
    light = detail_to_measurement(weight_only)
    check(light is not None and light.fat_pct is None and light.weight_kg == 86.2,
          "普通称重详情页：只有体重也照常解析，不报错")
    parsed = parse_weight_scale_fit(build_weight_fit([record, light]))
    check(len(parsed) == 2 and parsed[0]["basal_met"] == 1767.0
          and "percent_fat" not in parsed[1],
          "全量 + 仅体重两条导出 FIT 回读有效，字段各归其位")


def test_gui_offscreen() -> None:
    print("\n[4] 离屏 GUI 冒烟")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    configure_app(app)  # 与真实启动完全一致，否则弹层配色检查测的是裸控件
    window = MainWindow()
    window.show()
    check(window.listing.rowCount() == 0, "主窗口初始化成功")
    window._add_manual()
    check(window.listing.rowCount() == 1, "手工新增一条记录")
    window.fields.item(1, 1).setText("87.3")
    check(window.entries[0].values.get("weight_kg") == "87.3",
          "编辑表格会实时写回数据模型")
    if probe().get("rapid") and SAMPLE_IMG.exists():
        loop = QEventLoop()
        window._start_ocr([SAMPLE_IMG])
        if window._worker:
            window._worker.finished.connect(loop.quit)
            QTimer.singleShot(60000, loop.quit)
            loop.exec()
        check(len(window.entries) >= 2, "拖图流程在 GUI 线程模型下完成识别")
        latest = window.entries[-1]
        check(latest.valid, "GUI 里识别出的记录直接通过校验，无需人工改数")
        check(latest.values.get("weight_kg") not in (None, ""),
              "识别结果已回填到可编辑表格")

    print("\n[5] 手机取图、全自动按钮与下拉配色")
    check(hasattr(window, "chk_auto") and hasattr(window, "btn_capture")
          and hasattr(window, "btn_steps"), "手机取图工具条已接入主窗口")
    check(hasattr(window, "btn_auto") and hasattr(window, "spin_limit"),
          "「全自动取报告」按钮与最多条数设置已接入主窗口")
    check(window.spin_limit.value() >= 1 and "全自动" in window.btn_auto.text(),
          "全自动按钮文案与条数默认值有效")
    check(hasattr(window, "btn_fast") and "快速清单" in window.btn_fast.text()
          and hasattr(window, "chk_weight_only"),
          "「快速清单（控件树直读）」与普通称重兼容开关已接入主窗口")
    check(hasattr(window, "btn_detail") and "详情全量" in window.btn_detail.text(),
          "「详情全量（控件树直读）」按钮已接入主窗口")
    window._phone_status("● 已连接：25053RT47C")
    check("已连接" in window.phone_label.text(), "手机连接状态可更新")

    # 曾经 Fusion 高亮文字是白色，导致选中项白字白底看不见。
    window.region.setCurrentIndex(0)
    window.region.showPopup()
    app.processEvents()
    popup = window.region.view().grab().toImage()
    dark = 0
    for y in range(0, min(24, popup.height())):
        for x in range(popup.width()):
            color = popup.pixelColor(x, y)
            if color.red() < 120 and color.green() < 120 and color.blue() < 120:
                dark += 1
    check(dark >= 60, f"下拉选中项文字清晰可见（深色像素 {dark}）")
    window.region.hidePopup()

    sample = parse_report_text(SAMPLE_TXT.read_text(encoding="utf-8"))
    good = Entry(SAMPLE_IMG, values_from_result(sample))
    good.refresh()
    blank = Entry(SAMPLE_IMG, {key: "" for key, *_ in FIELDS})
    blank.refresh()
    check(looks_like_report(good) and not looks_like_report(blank),
          "自动监听只放行体成分报告，挡掉普通照片")
    before = len(window.entries)
    window._auto_paths.add(SAMPLE_IMG)
    window._on_entry_ready(blank, "", "")
    check(len(window.entries) == before, "非报告图片不会污染待导入清单")
    window._auto_paths.discard(SAMPLE_IMG)

    sys.path.insert(0, str(ROOT / "tests"))
    from test_phone import FakeBridge
    bridge = FakeBridge()
    worker = PhonePollWorker(bridge, interval=0.05)
    pulled: list[Path] = []
    worker.image.connect(lambda p: pulled.append(Path(p)))

    def add_file() -> None:
        bridge.files["/sdcard/DCIM/Screenshots/Screenshot_20261115_163000.jpg"] = 456789

    QTimer.singleShot(160, add_file)
    QTimer.singleShot(500, worker.stop)
    loop2 = QEventLoop()
    worker.finished.connect(loop2.quit)
    QTimer.singleShot(3000, loop2.quit)
    worker.start()
    loop2.exec()
    check(len(pulled) == 1 and pulled[0].exists(),
          "手机保存新图后自动拉取到本机并送入识别队列")

    before = len(window.entries)
    window._add_structured_records([
        Measurement(timestamp=parse_time("2026-09-25 14:04"), weight_kg=86.2)])
    structured = window.entries[-1]
    check(len(window.entries) == before + 1 and structured.valid,
          "控件树直读的「仅体重」记录直接进清单，且无需方程校验")
    check("普通称重" in window.check_label.text(),
          "界面明确标出这条只有体重、没有体脂")
    window.fields.item(1, 1).setText("86.3")
    check(structured.measurement.weight_kg == 86.3,
          "结构化记录在表格里改数字会写回数据模型")
    app.quit()


if __name__ == "__main__":
    test_roundtrip()
    test_offline_ocr()
    test_export_identical()
    test_auto_collect()
    test_fast_channel()
    test_detail_channel()
    if "--gui" in sys.argv:
        test_gui_offscreen()
    print("\n全部自检通过")
