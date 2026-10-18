"""Dictation into the focused window (any app): speech -> text -> paste (Ctrl+V), clipboard restored."""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

from .. import keyinput
from .base import Skill, ToolResult

log = logging.getLogger(__name__)

START_RE = re.compile(r"^(диктовка|начни диктовку|режим диктовки|печатай|пиши|пиши за мной|набирай текст|"
                      r"начинай писать|давай пиши|диктую)$")
STOP_RE = re.compile(r"\b(стоп диктовка|конец диктовки|хватит печатать|хватит писать|закончи диктовку|стоп печать|"
                     r"выключи диктовку|выйди из диктовки)\b")
STOP_ALONE = re.compile(r"^(хватит|закончи|заканчивай|всё|все|стоп|конец|стоп стоп)$")
# «напиши привет, как дела?» -> types «привет, как дела?» once. The verb is cut from the ORIGINAL text so whisper's
# punctuation and capitals survive. «напиши письмо маме» (LLM content) is not ours: only literal text after the verb.
ONCE_RE = re.compile(r"^\s*(?:джарвис[,!]?\s+)?(?:на)?пиш(?:и|ите)(?:\s+(?:мне|текст|туда|тут|здесь))?[\s,:—-]+(.+)$",
                     re.I | re.S)
NOT_LITERAL = re.compile(r"^(письмо|сообщение|стих\w*|сочинение|эссе|код|программ\w*|скрипт\w*|функци\w*|рассказ\w*|"
                         r"пост\w*|статью|отзыв\w*|резюме|поздравлени\w*|историю|песню|текст (?:про|о|об)\b|"
                         r"в телеграм\w*|в телегу|в чат|напоминани\w*|заметк\w*)\b", re.I)
# voice sub-commands while dictating (whole utterance)
SUBCMDS = [
    (re.compile(r"^(новая строка|новую строку|с новой строки|абзац|новый абзац|энтер|enter|ввод)$"), "enter"),
    (re.compile(r"^(отправь|отправить|отправляй|отправка)$"), "enter"),
    (re.compile(r"^(удали слово|сотри слово|удали последнее слово|назад слово)$"), "ctrl+backspace"),
    (re.compile(r"^(удали|сотри)$"), "backspace"),
    (re.compile(r"^(отмени|отмена)$"), "ctrl+z"),
]
OTHER_SKILL = re.compile(r"\b(телеграм\w*|телеге|телегу|телега|тг|скетч\w*|прошивк\w*)\b")  # telegram/code skills
IDLE_EXIT_S = 30.0
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
    """Fallback: put text into the clipboard, press Ctrl+V in the focused window, restore the old clipboard."""
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

    def __init__(self, ctx, clock=time.monotonic, typer=None, presser=None):
        super().__init__(ctx)
        self.active = False
        self.first = True
        self.clock = clock
        self.last = 0.0
        self.type = typer or self._type
        self.press = presser or keyinput.press

    # ---- modal state; wakeflow listens without the wake word while this is True
    @property
    def awaiting(self) -> bool:
        if self.active and self.clock() - self.last > IDLE_EXIT_S:
            self.active = False
            log.info("Диктовка: %d с тишины — выхожу", IDLE_EXIT_S)
            try:
                self.ctx.notify("Джарвис", "Диктовка окончена (тишина).")
            except Exception:                                # noqa: BLE001
                pass
        return self.active

    @property
    def awaiting_timeout(self) -> Optional[float]:
        """How long wakeflow may wait for the next phrase before re-checking (silence auto-exit)."""
        return max(1.0, IDLE_EXIT_S - (self.clock() - self.last)) if self.active else None

    def _start(self) -> str:
        self.active, self.first, self.last = True, True, self.clock()
        return "Пишу в активное окно. Скажи «хватит», чтобы закончить."

    def toggle(self) -> str:
        if self.active:
            self.active = False
            return "Диктовка окончена."
        return self._start()

    # ---- output
    def _wm(self):
        return self.ctx.extra.get("wm")

    def _type(self, text: str) -> bool:
        wm = self._wm()
        if wm is not None and getattr(wm, "b", None) is not None:
            if keyinput.focus_target(wm) is None:
                return False                                 # only Jarvis is focused: never type into ourselves
        return keyinput.type_text(text) or paste_text(text)

    def _out(self, chunk: str) -> Optional[ToolResult]:
        if not self.type(chunk):
            self.active = False
            return ToolResult("Не вижу окна, куда писать. Кликни в нужное поле и скажи ещё раз.")
        return None

    def handle_modal(self, text: str, t: str) -> Optional[ToolResult]:
        self.last = self.clock()
        if STOP_ALONE.match(t):
            self.active = False
            return ToolResult("Диктовка окончена.")
        for rx, combo in SUBCMDS:
            if rx.match(t):
                self.press(combo)
                if combo == "enter":
                    self.first = True
                return ToolResult("")
        m = STOP_RE.search(t)
        if m:
            low = text.lower()
            k = low.find(m.group(1))
            before = text[:k].strip() if k >= 0 else ""
            if before:
                self._out(("" if self.first else " ") + apply_punctuation(before))
            self.active = False
            return ToolResult("Диктовка окончена.")
        chunk = apply_punctuation(text)
        if not chunk:
            return ToolResult("")
        if not self.first and not chunk.startswith("\n"):
            chunk = " " + chunk
        self.first = False
        return self._out(chunk) or ToolResult("")

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if START_RE.match(t):
            return ToolResult(self._start())
        m = ONCE_RE.match(text or "")
        if m and t.startswith(("напиши", "напишите", "пиши ", "джарвис")):
            body = m.group(1).strip()
            if body and not NOT_LITERAL.match(body) and not OTHER_SKILL.search(t):
                return self._out(body) or ToolResult("")
        return None
