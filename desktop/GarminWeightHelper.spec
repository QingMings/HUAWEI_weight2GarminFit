# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir）。

构建：desktop/.venv/Scripts/python.exe -m PyInstaller --noconfirm --clean ^
        --distpath dist --workpath %TEMP%\\garmin_weight_build desktop\\GarminWeightHelper.spec
"""
from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

DESKTOP = Path(SPECPATH).resolve()
ROOT = DESKTOP.parent
ICON = DESKTOP / "assets" / "app.ico"
# 排查启动问题时用 GW_CONSOLE=1 打出控制台窗口，正常发布保持无控制台
CONSOLE = os.environ.get("GW_CONSOLE") == "1"

datas = [
    # USB 取图 / 投屏用的 adb + scrcpy
    (str(DESKTOP / "vendor" / "scrcpy-win64-v2.0"), "vendor/scrcpy-win64-v2.0"),
    # 首次运行时复制到 %LOCALAPPDATA%，保留已录制的手机点按步骤
    (str(DESKTOP / "phone_steps.json"), "."),
    # 程序图标：运行时 setWindowIcon 用，保证任务栏/Alt+Tab 也有图标
    (str(DESKTOP / "assets"), "assets"),
]
# RapidOCR 的 ONNX 模型与 config.yaml 必须随包
datas += collect_data_files("rapidocr")

# PySide6 6.12 把 shiboken6.abi3.dll 放在 shiboken6 子目录，QtCore.pyd 会在
# 包根目录找它；补一份到根目录，否则 exe 一启动就 "DLL load failed"。
binaries = []
try:
    import shiboken6

    _shiboken_dll = Path(shiboken6.__file__).resolve().parent / "shiboken6.abi3.dll"
    if _shiboken_dll.exists():
        binaries.append((str(_shiboken_dll), "."))
except Exception:
    pass

hiddenimports = collect_submodules("rapidocr")

# 只用到 QtWidgets/QtCore/QtGui，其余 Qt 模块全部排除以压体积
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml", "PySide6.QtQuickWidgets",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtDesigner",
    "PySide6.QtTest", "PySide6.QtHelp", "PySide6.QtSql", "PySide6.QtBluetooth",
    "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSerialPort",
    "PySide6.QtSensors", "PySide6.QtNetworkAuth", "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools", "tkinter", "matplotlib", "pandas", "scipy", "IPython",
]

a = Analysis(
    [str(DESKTOP / "main.py")],
    pathex=[str(ROOT), str(DESKTOP)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GarminWeightHelper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
)

# 裁掉用不到的大块二进制：本机 PATH 里的 Anaconda DLL（误当依赖）、
# OpenCV 的视频读写插件、Qt 的软件 OpenGL / Quick / Qml / Pdf（只做 Widgets 界面）。
# shapely 自带 shapely.libs 里的 GEOS，删掉 Anaconda 的重复副本不影响 OCR。
_DROP_SOURCES = ("c:/programdata/anaconda3",)
_DROP_DESTS = {
    "cv2/opencv_videoio_ffmpeg500_64.dll",
    "pyside6/opengl32sw.dll",
    "pyside6/qt6quick.dll",
    "pyside6/qt6qml.dll",
    "pyside6/qt6qmlmodels.dll",
    "pyside6/qt6pdf.dll",
    "pyside6/qt6virtualkeyboard.dll",
}


def _keep(entry):
    dest = entry[0].replace("\\", "/").lower()
    src = str(entry[1]).replace("\\", "/").lower()
    if dest in _DROP_DESTS:
        return False
    return not any(src.startswith(prefix) for prefix in _DROP_SOURCES)


a.binaries = [item for item in a.binaries if _keep(item)]

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="GarminWeightHelper",
)
