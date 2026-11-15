"""源码运行与打包成 exe 后共用的路径规则。

开发时行为完全不变：数据就写在项目目录里。
PyInstaller 打包后：
  * 只读资源（adb/scrcpy、OCR 模型）来自 exe 自带的 _internal 目录；
  * 可写数据（导出的佳明文件、录制步骤、日志、拉回的图片）依次尝试
    %LOCALAPPDATA% → %APPDATA% → %TEMP% → exe 同目录，
    取第一个真正能写的，避免个别电脑上 LocalAppData 无权限导致程序无法启动。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# PyInstaller 启动的 exe 会设置 sys.frozen
FROZEN = bool(getattr(sys, "frozen", False))
DESKTOP = Path(__file__).resolve().parent
ROOT = DESKTOP.parent
# onedir 打包时随包文件在 _internal（即 _MEIPASS），开发时就是源码目录
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", DESKTOP))


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _data_dir() -> Path:
    if not FROZEN:
        return ROOT
    candidates = []
    for env in ("LOCALAPPDATA", "APPDATA", "TEMP"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "GarminWeightHelper")
    candidates.append(Path(sys.executable).resolve().parent / "data")
    for candidate in candidates:
        if _is_writable(candidate):
            return candidate
    return Path(tempfile.gettempdir()) / "GarminWeightHelper"


DATA_DIR = _data_dir()
LOG_DIR = DATA_DIR / "logs" if FROZEN else DESKTOP / "logs"
