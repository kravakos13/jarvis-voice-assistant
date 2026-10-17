"""Dictated notes."""
from __future__ import annotations

import re
from typing import Optional

from ..notes import Notes
from .base import Skill, ToolResult, fn, s

START_RE = re.compile(r"^(?:запиши|создай|сделай|добавь|новая|новую)\s+заметк\w*(?:\s*[:,-]?\s*(.*))?$")
DICTATE_RE = re.compile(r"^(?:диктовка заметки|продиктую заметку|заметка под диктовку)$")
END_RE = re.compile(r"\b(конец заметки|закончи заметку|стоп запись|конец записи|сохрани заметку|конец диктовки)\b")
CANCEL_RE = re.compile(r"\b(отмени заметку|удали заметку|отмена заметки)\b")
READ_RE = re.compile(r"\b(прочитай|прочти|зачитай|покажи)\b.*\bзаметк\w*")


class NotesSkill(Skill):
    name = "notes"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.notes = Notes(ctx.cfg.get("notes", {}))

    @property
    def awaiting(self) -> bool:
        return self.notes.dictating

    def handle_dictation(self, text: str, t: str) -> ToolResult:
        """Called by the router while dictation is active (every phrase goes here)."""
        if CANCEL_RE.search(t):
            self.notes.cancel_dictation()
            return ToolResult("Заметку отменил.")
        m = END_RE.search(t)
        if m:
            before = text[: max(0, text.lower().find(m.group(1)))].strip(" ,.")
            if before:
                self.notes.add_dictation(before)
            path = self.notes.finish_dictation()
            return ToolResult("Заметку сохранил." if path else "Пустую заметку сохранять не стал.")
        self.notes.add_dictation(text)
        return ToolResult("")   # silent, keep listening

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        m = START_RE.match(t)
        if m or DICTATE_RE.match(t):
            body = ""
            if m and m.group(1):
                # keep original casing/punctuation of the dictated part
                low = text.lower()
                idx = low.find("заметк")
                body = text[idx:].split(None, 1)[1] if idx >= 0 and len(text[idx:].split(None, 1)) > 1 else m.group(1)
                body = body.lstrip(" :,-")
            if body:
                self.notes.save(body)
                return ToolResult("Записал.")
            self.notes.start_dictation()
            return ToolResult("Диктуй. Когда закончишь, скажи «конец заметки».", action="dictation")
        if READ_RE.search(t):
            n = 1 if re.search(r"последн\w+ заметк[уа]\b", t) else 3
            return ToolResult(self.notes.read_latest_reply(n))
        return None

    def tools(self):
        return [fn("add_note", "Сохранить текстовую заметку.", {"text": s("текст заметки")}, ["text"]),
                fn("read_notes", "Прочитать последние заметки.", {})]

    def call(self, name, args):
        if name == "add_note":
            self.notes.save(str(args.get("text", "")))
            return ToolResult("Записал.")
        if name == "read_notes":
            return ToolResult(self.notes.read_latest_reply(3))
        return None
