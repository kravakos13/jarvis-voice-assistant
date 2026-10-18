"""ChatGPT-style answers from the web (explicit phrases + LLM tool call)."""
from __future__ import annotations

import re
from typing import Optional

from ..websearch import WebSearch
from .base import Skill, ToolResult, fn, s

ASK_RE = re.compile(r"^(?:найди|поищи|посмотри|узнай|проверь)(?: мне)?(?: в интернете| в сети| онлайн)?\s+(?:что |как |кто |где |когда |сколько |почему |какой |какая |какие )?(.+)$")
EXPLICIT_RE = re.compile(r"\b(в интернете|в сети|онлайн|погугли за меня|последние новости|новости)\b")


class WebSkill(Skill):
    name = "web"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.web = WebSearch(ctx.cfg.get("web_search", {}), ctx.llm)

    def ask(self, question: str, context: str = "") -> ToolResult:
        def work():
            ans = self.web.answer(question, context)
            if ans.sources and self.ctx.extra.get("on_sources"):
                self.ctx.extra["on_sources"]([(x.title, x.url) for x in ans.sources[:5]])
            return ans.answer
        return ToolResult("", then=work)   # «Секунду» comes from the assistant only if it takes > ack_after_s

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if re.search(r"^(загугли|погугли|гугли)\b|\bв (гугле|яндексе|хроме|chrome|браузере)\b|\bна ютуб", t):
            return None
        m = ASK_RE.match(t)
        if m and not re.search(r"\b(видео|ютуб\w*|youtube|заметк\w*|задач\w*|файл\w*)\b", t):
            q = re.sub(r"\b(в интернете|в сети|онлайн)\b", "", m.group(0)).strip()
            q = re.sub(r"^(найди|поищи|посмотри|узнай|проверь)( мне)?\s*", "", q)
            return self.ask(q or m.group(1))
        if re.search(r"\b(последние новости|что нового в мире|новости)\b", t) and "телеграм" not in t:
            return self.ask(text)
        return None

    def tools(self):
        return [fn("web_answer", "Найти в интернете свежую или фактическую информацию (новости, курсы, цены, факты, "
                                 "расписания, события после 2023 года) и ответить.",
                   {"query": s("поисковый запрос")}, ["query"])]

    def call(self, name, args):
        if name == "web_answer":
            return self.ask(str(args.get("query", "")))
        return None
