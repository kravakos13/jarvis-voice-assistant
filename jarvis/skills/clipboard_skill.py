"""Clipboard: read aloud / translate / explain via LLM."""
from __future__ import annotations

import re
from typing import Optional

from .base import Skill, ToolResult

CLIP = r"(буфер\w*|буфер обмена|скопированн\w*|то что я скопировал)"
READ_RE = re.compile(rf"\b(прочитай|прочти|зачитай|что в)\b.*{CLIP}")
TRANSLATE_RE = re.compile(rf"\bпереведи\b.*{CLIP}(?:.*\bна (\w+))?")
EXPLAIN_RE = re.compile(rf"\b(объясни|перескажи|кратко|суммируй|что значит|исправь)\b.*{CLIP}")


def get_clipboard() -> str:
    try:
        import pyperclip
        return pyperclip.paste() or ""
    except Exception:
        return ""


class ClipboardSkill(Skill):
    name = "clipboard"

    def _llm(self, instruction: str, text: str) -> str:
        return self.ctx.llm.complete([
            {"role": "system", "content": "Ты помощник. Отвечай по-русски кратко, без markdown, ответ будет озвучен."},
            {"role": "user", "content": f"{instruction}\n\n{text[:6000]}"}])

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if not re.search(CLIP, t):
            return None
        clip = get_clipboard().strip()
        if not clip and (READ_RE.search(t) or TRANSLATE_RE.search(t) or EXPLAIN_RE.search(t)):
            return ToolResult("Буфер обмена пуст.")
        m = TRANSLATE_RE.search(t)
        if m:
            lang = m.group(2) or ("русский" if not re.search(r"[а-яё]", clip.lower()) else "английский")
            return ToolResult("", then=lambda: self._llm(f"Переведи текст на {lang}. Выведи только перевод.", clip))
        m = EXPLAIN_RE.search(t)
        if m:
            what = {"исправь": "Исправь ошибки в тексте и прочитай исправленный вариант."}.get(
                m.group(1), "Кратко, в двух-трёх предложениях, объясни или перескажи этот текст.")
            return ToolResult("", then=lambda: self._llm(what, clip))
        if READ_RE.search(t):
            return ToolResult(clip[:600] + ("... дальше слишком длинно." if len(clip) > 600 else ""))
        return None
