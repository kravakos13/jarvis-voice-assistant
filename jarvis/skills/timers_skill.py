"""Timers and stopwatch."""
from __future__ import annotations

import re
import threading
import time
from typing import Optional

from ..planner import words_to_digits
from .base import Skill, ToolResult, fn
from .core import plural_ru

TIMER_RE = re.compile(r"\b(?:поставь|заведи|запусти|включи|установи|сделай)?\s*таймер\w*\s+(?:на\s+)?(.+)$")
UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)?\s*(час\w*|минут\w*|мин|секунд\w*|сек|полчаса|полминуты)")
CANCEL_RE = re.compile(r"\b(отмени|останови|выключи|сбрось|убери)\s+(все\s+)?таймер\w*")
LEFT_RE = re.compile(r"\bсколько (осталось|еще) (на |до )?таймер\w*|\bсколько осталось\b")
SW_START = re.compile(r"\b(запусти|включи|старт|начни)\s+секундомер\w*")
SW_STOP = re.compile(r"\b(останови|выключи|стоп)\s+секундомер\w*")
SW_ASK = re.compile(r"\bсколько (на|показывает) секундомер\w*|\bсекундомер сколько")


def parse_duration(text: str) -> int:
    t = words_to_digits(text.lower())
    total = 0.0
    for m in UNIT_RE.finditer(t):
        n = float(m.group(1)) if m.group(1) else 1.0
        u = m.group(2)
        if u.startswith("полчаса"):
            total += 1800
        elif u.startswith("полминуты"):
            total += 30
        elif u.startswith("час"):
            total += n * 3600
        elif u.startswith("мин"):
            total += n * 60
        else:
            total += n
    return int(total)


def say_duration(sec: int) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s_ = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} {plural_ru(h, 'час', 'часа', 'часов')}")
    if m:
        parts.append(f"{m} {plural_ru(m, 'минута', 'минуты', 'минут')}")
    if s_ or not parts:
        parts.append(f"{s_} {plural_ru(s_, 'секунда', 'секунды', 'секунд')}")
    return " ".join(parts)


class TimersSkill(Skill):
    name = "timers"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.timers: list[tuple[float, threading.Timer, str]] = []
        self.sw_start: Optional[float] = None

    def start_timer(self, seconds: int) -> ToolResult:
        if seconds <= 0:
            return ToolResult("На сколько поставить таймер?")
        label = say_duration(seconds).replace("1 минута", "1 минуту").replace("1 секунда", "1 секунду")
        label = re.sub(r"\b(\d*[02-9]|)1 минута\b", r"\g<1>1 минуту", label)

        def fire():
            self.timers = [x for x in self.timers if x[1] is not timer]
            self.ctx.notify("Джарвис: таймер", f"Таймер на {label} сработал")
            self.ctx.say(f"Время вышло! Таймер на {label}.")
        timer = threading.Timer(seconds, fire)
        timer.daemon = True
        timer.start()
        self.timers.append((time.time() + seconds, timer, label))
        return ToolResult(f"Таймер на {label} пошёл.")

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if CANCEL_RE.search(t):
            n = len(self.timers)
            for _, tm, _ in self.timers:
                tm.cancel()
            self.timers.clear()
            return ToolResult("Таймеры отменил." if n else "Активных таймеров нет.")
        if LEFT_RE.search(t) and self.timers:
            left = min(x[0] for x in self.timers) - time.time()
            return ToolResult(f"Осталось {say_duration(max(0, left))}.")
        if SW_START.search(t):
            self.sw_start = time.time()
            return ToolResult("Секундомер пошёл.")
        if SW_STOP.search(t):
            if self.sw_start is None:
                return ToolResult("Секундомер не запущен.")
            el, self.sw_start = time.time() - self.sw_start, None
            return ToolResult(f"Стоп. Прошло {say_duration(el)}.")
        if SW_ASK.search(t):
            if self.sw_start is None:
                return ToolResult("Секундомер не запущен.")
            return ToolResult(f"Прошло {say_duration(time.time() - self.sw_start)}.")
        m = TIMER_RE.search(t)
        if m and "таймер" in t:
            return self.start_timer(parse_duration(m.group(1)))
        return None

    def tools(self):
        return [fn("set_timer", "Поставить таймер.", {"seconds": {"type": "integer", "description": "длительность в секундах"}}, ["seconds"])]

    def call(self, name, args):
        if name == "set_timer":
            return self.start_timer(int(args.get("seconds", 0)))
        return None
