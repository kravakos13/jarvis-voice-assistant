"""Long-term memory voice commands."""
from __future__ import annotations

import re
from typing import Optional

from ..memory import Memory
from .base import Confirm, Skill, ToolResult, fn, s

REMEMBER_RE = re.compile(r"^(?:запомни|запиши в память|учти|имей в виду)[,:]?\s+(?:что\s+|то что\s+)?(.+)$")
RECALL_RE = re.compile(r"\b(что ты (обо мне|про меня) (знаешь|помнишь)|что ты помнишь|что ты знаешь обо мне|покажи память)\b")
FORGET_ALL_RE = re.compile(r"\bзабудь (все|всё) обо мне\b|\bочисти память\b|\bсотри память\b")
FORGET_RE = re.compile(r"^забудь[,]?\s+(?:что\s+|про\s+|о том что\s+|факт\s+)(.+)$")


class MemorySkill(Skill):
    name = "memory"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.memory = Memory(ctx.cfg.get("memory", {}), ctx.llm)
        ctx.llm.context_provider = self.memory.context_for

    def _first_person(self, fact: str) -> str:
        # "я люблю пиццу" -> "Пользователь любит пиццу" is hard; just store as said, with a prefix
        return fact[0].upper() + fact[1:] if fact else fact

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if FORGET_ALL_RE.search(t):
            return ToolResult("", confirm=Confirm("Точно стереть всю память о тебе? Да или нет?",
                                                  lambda: ToolResult(f"Стёр. Удалено записей: {self.memory.clear()}.")))
        m = REMEMBER_RE.match(t)
        if m and not re.match(r"^запомни(?:,)? (?:заметку|задачу)", t):
            raw = re.sub(r"^(?:запомни|запиши в память|учти|имей в виду)[,:]?\s+(?:что\s+|то что\s+)?", "", text.strip(),
                         flags=re.I)
            self.memory.add(self._first_person(raw.strip(" .")))
            return ToolResult("Запомнил.")
        if RECALL_RE.search(t):
            facts = self.memory.all("fact")
            if not facts:
                return ToolResult("Пока ничего особенного. Скажи «запомни, что...», и я запомню.")
            return ToolResult("Вот что я помню: " + "; ".join(f"{f.text}" for f in facts[-12:]) + ".")
        m = FORGET_RE.match(t)
        if m:
            it = self.memory.find(m.group(1))
            if not it:
                return ToolResult("Такого я и не помнил.")
            self.memory.delete(it.id)
            return ToolResult(f"Забыл: {it.text}.")
        return None

    def tools(self):
        return [fn("remember_fact", "Запомнить важный факт о пользователе надолго (имя, предпочтения, устройства, планы).",
                   {"fact": s("факт одной фразой")}, ["fact"])]

    def call(self, name, args):
        if name == "remember_fact":
            self.memory.add(str(args.get("fact", "")))
            return ToolResult("Запомнил.")
        return None
