"""本地离线 OCR 引擎封装。

优先用 RapidOCR（PaddleOCR 的 PP-OCRv6 模型转 ONNX，模型随 pip 包分发，
装完即可断网运行）；没装时降级到 Windows 自带简体中文 OCR；两者都没有时
返回空结果，软件仍可手工填表 —— 任何情况下都不会把图片传到网络上。

对外统一输出带坐标的文本行，可直接转成 sync_tool.report 认识的
「YYYY|XXXX|文字」版面文本，复用已有的四组分方程校验逻辑。
"""
from __future__ import annotations

import importlib
import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

# 报告截图的基准宽度（样例 484px）。rapid 输出的文本框比 Windows OCR 细得多，
# 「标签行」与「下一行数值」只差约 22px，会被通用解析器误判成「同一行右侧」。
# 纵向放大后把这段间距推到 20~90px 的「标签在上、数值在下」区间。
BASE_WIDTH = 484.0


@dataclass
class OcrBox:
    y: float
    x: float
    text: str
    score: float = 1.0


@dataclass
class OcrOutcome:
    boxes: list[OcrBox]
    engine: str
    detail: str = ""
    error: str = ""
    y_factor: float = 1.0  # 生成版面文本时纵向坐标的缩放系数

    @property
    def ok(self) -> bool:
        return bool(self.boxes) and not self.error


def probe() -> dict[str, str]:
    """只查包是否存在，不真正加载模型（界面启动时不该卡住）。"""
    info: dict[str, str] = {}
    for name, key in (("rapidocr", "rapid"), ("onnxruntime", "onnx")):
        try:
            spec = importlib.util.find_spec(name)
            if spec is None:
                info[key] = ""
                continue
            if key == "rapid":
                # 版本从包元数据读，避免 import 触发 onnxruntime 加载
                from importlib.metadata import version
                try:
                    info[key] = version("rapidocr")
                except Exception:
                    info[key] = "已安装"
            else:
                info[key] = "已安装"
        except Exception:
            info[key] = ""
    info["windows"] = "可用" if _windows_ocr_available() else ""
    return info


def _windows_ocr_available() -> bool:
    import shutil
    import sys
    return sys.platform == "win32" and shutil.which("powershell.exe") is not None


def _rapid_result_to_boxes(result) -> list[OcrBox]:
    """兼容 RapidOCR 3.x 的 RapidOCROutput 与 1.x 的 (list, elapse) 两种返回。"""
    boxes: list[OcrBox] = []
    items = None
    if hasattr(result, "boxes") and hasattr(result, "txts"):
        points = result.boxes
        texts = result.txts
        scores = getattr(result, "scores", None) or [1.0] * len(texts)
        items = zip(points, texts, scores)
    elif isinstance(result, tuple) and result:
        items = result[0]
    elif isinstance(result, list):
        items = result
    if items is None:
        return boxes
    for item in items or []:
        try:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                points, text = item[0], item[1]
                score = float(item[2]) if len(item) >= 3 and item[2] is not None else 1.0
            else:
                continue
            if not text or not str(text).strip():
                continue
            if points is None or len(points) == 0:
                continue
            ys = [float(p[1]) for p in points]
            xs = [float(p[0]) for p in points]
            boxes.append(OcrBox(min(ys), min(xs), str(text).strip(), score))
        except (TypeError, ValueError, IndexError):
            continue
    return boxes


class OcrEngine:
    """按可用性自动选择后端。engine='auto' | 'rapid' | 'windows' | 'manual'。"""

    def __init__(self, engine: str = "auto") -> None:
        self._impl = None
        self._kind = "manual"
        self._detail = ""
        self._error = ""
        if engine == "manual":
            return
        if engine in ("auto", "rapid"):
            if self._init_rapid():
                return
        if engine in ("auto", "windows") and _windows_ocr_available():
            self._kind = "windows"
            self._detail = "Windows 内置简体中文 OCR（零安装，但数字易误识）"
            return
        if not self._error:
            self._error = "未找到本地 OCR 引擎，可点「安装离线 OCR」或直接在表格里手填。"

    def _init_rapid(self) -> bool:
        try:
            from rapidocr import RapidOCR
            self._impl = RapidOCR()
            from importlib.metadata import version
            try:
                tag = version("rapidocr")
            except Exception:
                tag = getattr(importlib.import_module("rapidocr"), "__version__", "")
            self._kind = "rapid"
            self._detail = (f"RapidOCR {tag} "
                            "· PP-OCRv6 ONNX · 纯本地离线 · 模型随包")
            return True
        except Exception as exc:  # noqa: BLE001
            self._error = f"RapidOCR 初始化失败：{type(exc).__name__}: {exc}"
            return False

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def label(self) -> str:
        return {"rapid": "离线 OCR（RapidOCR）",
                "windows": "Windows 内置 OCR",
                "manual": "手工填写"}.get(self._kind, "手工填写")

    @property
    def detail(self) -> str:
        return self._detail or self._error

    @property
    def error(self) -> str:
        return self._error

    def recognize(self, image_path: Path) -> OcrOutcome:
        image_path = Path(image_path)
        if self._kind == "rapid":
            return self._recognize_rapid(image_path)
        if self._kind == "windows":
            return self._recognize_windows(image_path)
        return OcrOutcome([], self.label, error="当前为手工填写模式")

    def _recognize_rapid(self, image_path: Path) -> OcrOutcome:
        try:
            # 传 ndarray 可避免中文路径在部分 ONNX/OpenCV 版本下的解码问题。
            import numpy as np
            with Image.open(image_path) as im:
                image = np.asarray(im.convert("RGB"))
            factor = max(1.0, min(8.0, 3.0 * BASE_WIDTH / image.shape[1]))
            result = self._impl(image)
            boxes = _rapid_result_to_boxes(result)
            if not boxes:
                return OcrOutcome([], self.label, self._detail,
                                  "未识别到文字，请换一张更清晰的截图或手工填写",
                                  y_factor=factor)
            # 低置信度的数字行放大后重识别一次，专门救小数点。
            boxes = self._rescan_low_confidence(image, boxes)
            return OcrOutcome(boxes, self.label, self._detail, y_factor=factor)
        except Exception as exc:  # noqa: BLE001
            return OcrOutcome([], self.label, self._detail,
                              f"识别出错：{type(exc).__name__}: {exc}")

    def _rescan_low_confidence(self, image, boxes: list[OcrBox]) -> list[OcrBox]:
        """对含数字且置信度偏低的行，裁剪放大后再识别一次，取更可信的结果。"""
        try:
            import cv2
            import numpy as np
        except Exception:
            return boxes
        improved: list[OcrBox] = []
        for box in boxes:
            has_digit = any(ch.isdigit() for ch in box.text)
            if not has_digit or box.score >= 0.92:
                improved.append(box)
                continue
            y, x = int(box.y), int(box.x)
            # 按该行文字长度估计裁剪宽度；长图里数字通常在同行右侧。
            h = max(24, min(90, image.shape[0] // 20))
            w = max(120, min(image.shape[1], len(box.text) * 22 + 120))
            crop = image[max(0, y - 8):min(image.shape[0], y + h),
                         max(0, x - 8):min(image.shape[1], x + w)]
            if crop.size == 0:
                improved.append(box)
                continue
            big = cv2.resize(crop, None, fx=3, fy=3,
                             interpolation=cv2.INTER_CUBIC)
            try:
                second = _rapid_result_to_boxes(self._impl(big))
                if second:
                    text = " ".join(b.text for b in second)
                    best = max(b.score for b in second)
                    if best > box.score and any(ch.isdigit() for ch in text):
                        improved.append(OcrBox(box.y, box.x, text, best))
                        continue
            except Exception:
                pass
            improved.append(box)
        return improved

    def _recognize_windows(self, image_path: Path) -> OcrOutcome:
        try:
            from sync_tool.report import parse_ocr_lines, run_ocr
            text = run_ocr(image_path)
            boxes = [OcrBox(line.y, line.x, line.text, 0.6)
                     for line in parse_ocr_lines(text)]
            return OcrOutcome(boxes, self.label, self._detail)
        except Exception as exc:  # noqa: BLE001
            return OcrOutcome([], self.label, self._detail,
                              f"Windows OCR 出错：{type(exc).__name__}: {exc}")


def to_layout_text(boxes: list[OcrBox], *, y_factor: float = 1.0) -> str:
    """转成 sync_tool.report.parse_ocr_lines 接受的 YYYY|XXXX|文字。

    - y_factor：把「标签行→数值行」的间距推到通用解析器的正确区间。
    - 顺手清掉千分位逗号，否则 `1,780千卡/日` 会被读成基础代谢 1 kcal。
    """
    lines = []
    for box in sorted(boxes, key=lambda b: (b.y, b.x)):
        clean = str(box.text).replace("|", " ")
        clean = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", clean)
        lines.append(f"{int(box.y * y_factor):04d}|{int(box.x):04d}|{clean}")
    return "\n".join(lines)
