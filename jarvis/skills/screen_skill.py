"""Screen understanding: 'что на экране', 'прочитай текст на экране', 'объясни ошибку'."""
from __future__ import annotations

import re
from typing import Optional

from ..screen import ScreenReader
from .base import Skill, ToolResult, fn, s

WHAT_RE = re.compile(r"\b(что (у меня )?на экране|что ты видишь|посмотри на экран|глянь на экран|что здесь происходит)\b")
READ_RE = re.compile(r"\b(прочитай|прочти|зачитай)\b.*\b(текст )?(на|с) экран\w*")
ERROR_RE = re.compile(r"\b(объясни|что за|разбери|почему)\b.*\b(ошибк\w*|эррор|error)\b")


class ScreenSkill(Skill):
    name = "screen"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.reader = ScreenReader(ctx.cfg.get("screen", {}), ctx.llm)

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if READ_RE.search(t):
            return ToolResult("Читаю.", then=self.reader.read_text)
        if ERROR_RE.search(t) and re.search(r"экран|тут|здесь|эту|это", t):
            return ToolResult("Смотрю.", then=lambda: self.reader.ask(
                "На скриншоте сообщение об ошибке. Объясни по-русски кратко, в чём причина и как исправить.",
                "There is an error message in this screenshot. Explain the cause and how to fix it briefly.", True))
        if WHAT_RE.search(t):
            return ToolResult("Смотрю.", then=lambda: self.reader.ask(
                "Опиши по-русски кратко, что на экране: какое приложение открыто и что происходит.",
                "Briefly describe what is on this screen: which app is open and what is happening."))
        return None

    def tools(self):
        return [fn("look_at_screen", "Посмотреть на экран пользователя и ответить на вопрос о нём.",
                   {"question": s("вопрос про экран")}, ["question"])]

    def call(self, name, args):
        if name == "look_at_screen":
            q = str(args.get("question", "Что на экране?"))
            return ToolResult("Смотрю.", then=lambda: self.reader.ask(q, q))
        return None
