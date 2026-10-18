"""Diary / tasks / reminders."""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

from ..planner import Planner, human_dt, parse_task, parse_task_llm, reminder_text
from .base import Skill, ToolResult, fn, s

ADD_RE = re.compile(r"^(?:напомни|поставь напоминание|добавь задачу|добавь в план\w*|запланируй|создай задачу|создай напоминание|новая задача)\b")
LIST_RE = re.compile(r"\b(какие|мои|список|что у меня|покажи|прочитай|расскажи)\b.*\b(задач\w*|дел\w*|план\w*|напоминани\w*|на сегодня|на завтра)\b|^что у меня (на )?(сегодня|завтра)")
DONE_RE = re.compile(r"\b(отметь|выполнил\w*|сделал\w*|готово|заверши|закрой)\b.*\bзадач\w*|\bзадач\w* .*\b(выполнен\w*|сделан\w*)")
ALARM_RE = re.compile(r"^(?:поставь|заведи|установи|включи|сделай|создай)\s+(?:мне\s+)?будильник\w*\s*(.*)$"
                      r"|^разбуди\s+(?:меня\s+)?(.+)$|^будильник\s+(?:на\s+)?(.+)$")
DEL_RE = re.compile(r"^(?:удали|убери|отмени)\s+(?:задачу|напоминание)\b(.*)$")


def alarm_phrase(when: str) -> str:
    """'на 7' / 'в 6:30' / 'завтра на 7 утра' -> a planner phrase 'напомни … будильник'.
    A bare hour up to 11 means the morning (nobody sets an alarm for 7 pm by saying 'на 7')."""
    w = re.sub(r"\b(\d{1,2})[ .:-](\d{2})\b", r"\1:\2", when.strip())     # normalize() turned 6:30 into "6 30"
    w = re.sub(r"^на\s+", "в ", w)
    w = re.sub(r"\bна (\d)", r"в \1", w)
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\b", w)
    if m and not re.search(r"утр|вечер|дня|ночи|через", w) and int(m.group(1)) <= 11:
        w = w[:m.end()] + " утра" + w[m.end():]
    if not re.match(r"^(в|во|завтра|сегодня|через|послезавтра|утром|вечером)\b", w):
        w = "в " + w
    return f"напомни {w} будильник"


class PlannerSkill(Skill):
    name = "planner"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.planner = Planner(ctx.cfg.get("planner", {}))
        self.use_llm_fallback = bool(ctx.cfg.get("planner", {}).get("llm_date_fallback", True))

    def start(self):
        def on_rem(task):
            self.ctx.notify("Джарвис: напоминание", task.text)
            self.ctx.say(reminder_text(task))
        self.planner.start_scheduler(on_rem, on_morning=self.ctx.say)

    def add(self, text: str) -> ToolResult:
        p = parse_task(text)
        if p.due is None and self.use_llm_fallback and re.search(r"\d|утр|вечер|ночь|недел|месяц|после|через", text.lower()):
            p2 = parse_task_llm(text, self.ctx.llm)
            if p2.due:
                p = p2
        t = self.planner.add(p.text, p.due)
        if p.due:
            return ToolResult(f"Записал задачу номер {t.id}: {p.text}. Напомню {human_dt(p.due)}.")
        return ToolResult(f"Записал задачу номер {t.id}: {p.text}. Без времени, просто в список.")

    def alarm(self, when: str) -> ToolResult:
        p = parse_task(alarm_phrase(when)) if when.strip() else None
        if p is None:
            return ToolResult("На какое время поставить будильник? Скажи, например: «поставь будильник на 7 утра».")
        if p.due is None:
            return ToolResult("На какое время поставить будильник? Скажи, например: «поставь будильник на 7 утра».")
        self.planner.add("Будильник", p.due)
        return ToolResult(f"Будильник поставил: {human_dt(p.due)}.")

    def list_reply(self, t: str) -> ToolResult:
        today = _dt.date.today()
        if "завтра" in t:
            return ToolResult(self.planner.summary_for(today + _dt.timedelta(days=1), "завтра"))
        if "сегодня" in t:
            return ToolResult(self.planner.summary_for(today, "сегодня"))
        tasks = self.planner.list()
        if not tasks:
            return ToolResult("Открытых задач нет.")
        parts = []
        for x in tasks[:10]:
            when = f", {human_dt(x.due)}" if x.due else ""
            parts.append(f"номер {x.id}: {x.text}{when}")
        return ToolResult(f"Всего открытых задач: {len(tasks)}. " + "; ".join(parts) + ".")

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        m = ALARM_RE.match(t)
        if m:
            return self.alarm(next((g for g in m.groups() if g), ""))
        m = DEL_RE.match(t)
        if m:
            task = self.planner.find(m.group(1))
            if not task:
                return ToolResult("Не нашёл такую задачу.")
            self.planner.delete(task.id)
            return ToolResult(f"Удалил задачу: {task.text}.")
        if DONE_RE.search(t):
            task = self.planner.find(t)
            if not task:
                return ToolResult("Не понял, какую задачу отметить. Назови номер.")
            self.planner.set_done(task.id)
            return ToolResult(f"Отметил как выполненную: {task.text}. Красавчик.")
        if LIST_RE.search(t) and not ADD_RE.match(t):
            return self.list_reply(t)
        if ADD_RE.match(t) and not re.match(r"^напомни (мне )?(что|о чем) (мы|я) (говорил|обсужд)", t):
            return self.add(text)
        return None

    def tools(self):
        return [
            fn("add_task", "Добавить задачу или напоминание на дату/время.",
               {"text": s("фраза пользователя целиком, включая время, напр. 'завтра в 9 позвонить маме'")}, ["text"]),
            fn("list_tasks", "Прочитать задачи.", {"day": s("за какой день", ["today", "tomorrow", "all"])}),
        ]

    def call(self, name, args):
        if name == "add_task":
            return self.add(str(args.get("text", "")))
        if name == "list_tasks":
            d = args.get("day", "all")
            return self.list_reply({"today": "сегодня", "tomorrow": "завтра"}.get(d, ""))
        return None
