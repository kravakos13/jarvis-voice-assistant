"""Screen capture + local vision model (Ollama) + optional Tesseract OCR."""
from __future__ import annotations

import base64
import io
import logging
import re
import shutil
from typing import Optional

from . import platform_utils as pu

log = logging.getLogger(__name__)


def foreground_window() -> tuple[str, str, Optional[tuple[int, int, int, int]]]:
    """-> (process name, window title, rect) of the active window (Windows), else ('', '', None)."""
    if not pu.IS_WIN:
        return "", "", None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ""
        try:
            import psutil
            name = psutil.Process(pid.value).name()
        except Exception:
            pass
        return name, buf.value, (rect.left, rect.top, rect.right, rect.bottom)
    except Exception as e:
        log.debug("foreground: %s", e)
        return "", "", None


def capture(active_window: bool = False, max_side: int = 1280):
    """Returns a PIL image of the primary monitor (or the active window)."""
    from PIL import Image
    import mss
    with mss.mss() as sct:
        region = sct.monitors[1]
        if active_window:
            _, _, rect = foreground_window()
            if rect and rect[2] - rect[0] > 50 and rect[3] - rect[1] > 50:
                region = {"left": rect[0], "top": rect[1], "width": rect[2] - rect[0], "height": rect[3] - rect[1]}
        shot = sct.grab(region)
        img = Image.frombytes("RGB", shot.size, shot.rgb)
    img.thumbnail((max_side, max_side))
    return img


def to_b64(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def ocr(img, tesseract_path: str = "", langs: str = "rus+eng") -> Optional[str]:
    try:
        import pytesseract
    except ImportError:
        return None
    exe = tesseract_path or shutil.which("tesseract") or (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe" if pu.IS_WIN else "")
    if not exe:
        return None
    pytesseract.pytesseract.tesseract_cmd = exe
    try:
        return pytesseract.image_to_string(img, lang=langs).strip()
    except Exception as e:
        log.warning("OCR: %s", e)
        return None


def mostly_russian(text: str) -> bool:
    cyr = len(re.findall(r"[а-яё]", text.lower()))
    lat = len(re.findall(r"[a-z]", text.lower()))
    return cyr >= lat


class ScreenReader:
    def __init__(self, cfg: dict, llm):
        self.cfg = cfg or {}
        self.llm = llm
        self.models = [self.cfg.get("vision_model", "qwen2.5vl:3b")] + list(self.cfg.get("fallback_models", ["moondream"]))

    def _model(self) -> Optional[str]:
        for m in self.models:
            if self.llm.has_model(m):
                return m
        return None

    def ask(self, question_ru: str, question_en: str, active_window: bool = False, context: str = "") -> str:
        model = self._model()
        if not model:
            return f"Нет модели для зрения. Скачай её: ollama pull {self.models[0]}"
        try:
            img = capture(active_window)
        except Exception as e:
            log.error("Захват экрана: %s", e)
            return "Не получилось сделать снимок экрана."
        prompt = question_en if model.startswith("moondream") else question_ru
        if context:
            prompt = f"{context}\n{prompt}"
        out = self.llm.complete_with_images(prompt, [to_b64(img)], model)
        if out and not mostly_russian(out):
            out = self.llm.complete([{"role": "user", "content":
                "Перескажи по-русски коротко и разговорно, 2-3 предложения, без markdown:\n\n" + out}])
        return out or "Ничего не разобрал."

    def read_text(self) -> str:
        try:
            img = capture(False, 2400)
        except Exception as e:
            return f"Не получилось сделать снимок экрана: {e}"
        text = ocr(img, self.cfg.get("tesseract_path", ""))
        if text:
            return text[:800]
        return self.ask("Перепиши дословно основной текст на этом скриншоте.",
                        "Transcribe the main text visible in this screenshot.")
