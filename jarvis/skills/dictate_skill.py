"""Dictation into the focused window (any app): speech -> text -> paste (Ctrl+V), clipboard restored."""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

from .base import Skill, ToolResult

log = logging.getLogger(__name__)

START_RE = re.compile(r"^(диктовка|начни диктовку|режим диктовки|печатай|пиши за мной|набирай текст)$")
STOP_RE = re.compile(r"\b(стоп диктовка|конец диктовки|хватит печатать|закончи диктовку|стоп печать)\b")
PUNCT = [
    (r"\s*\bновая строка\b\s*", "\n"), (r"\s*\bновый абзац\b\s*", "\n\n"),
    (r"\s*\bвосклицательный знак\b", "!"), (r"\s*\bвопросительный знак\b", "?"),
    (r"\s*\bточка с запятой\b", ";"), (r"\s*\bдвоеточие\b", ":"), (r"\s*\bмноготочие\b", "..."),
    (r"\s*\bточка\b", "."), (r"\s*\bзапятая\b", ","), (r"\s*\bтире\b", " —"),
    (r"\bоткрыть скобку\s*", "("), (r"\s*\bзакрыть скобку\b", ")"), (r"\bкавычки\b", "\""),
]


def apply_punctuation(text: str) -> str:
    out = text.strip()
    for rx, rep in PUNCT:
        out = re.sub(rx, rep, out, flags=re.I)
    out = re.sub(r"([.,!?;:])(?=[^\s\n.,!?;:)])", r"\1 ", out)
    out = re.sub(r"[ \t]+", " ", out)
    # capitalize after sentence end / new line
    out = re.sub(r"(^|[.!?]\s+|\n)([a-zа-яё])", lambda m: m.group(1) + m.group(2).upper(), out)
    return out


def paste_text(text: str) -> bool:
    """Put text into the clipboard, press Ctrl+V in the focused window, restore the old clipboard."""
    try:
        import keyboard
        import pyperclip
    except Exception as e:
        log.error("Диктовка недоступна: %s", e)
        return False
    try:
        old = pyperclip.paste()
    except Exception:
        old = None
    pyperclip.copy(text)
    time.sleep(0.05)
    keyboard.send("ctrl+v")

    def restore():
        time.sleep(0.6)
        if old is not None:
            try:
                pyperclip.copy(old)
            except Exception:
                pass
    threading.Thread(target=restore, daemon=True).start()
    return True


class DictateSkill(Skill):
    name = "dictate"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.active = False
        self.first = True

    @property
    def awaiting(self) -> bool:
        return self.active

    def toggle(self) -> str:
        self.active = not self.active
        self.first = True
        return "Диктую в активное окно. Скажи «стоп диктовка», чтобы закончить." if self.active else "Диктовка окончена."

    def handle_modal(self, text: str, t: str) -> Optional[ToolResult]:
        m = STOP_RE.search(t)
        if m:
            before = text[: text.lower().find(m.group(1))].strip()
            if before:
                paste_text(("" if self.first else " ") + apply_punctuation(before))
            self.active = False
            return ToolResult("Диктовка окончена.")
        chunk = apply_punctuation(text)
        if not self.first and not chunk.startswith("\n"):
            chunk = " " + chunk
        self.first = False
        paste_text(chunk)
        return ToolResult("")

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if START_RE.match(t):
            self.active, self.first = True, True
            return ToolResult("Диктую в активное окно. Скажи «стоп диктовка», чтобы закончить.")
        return None
