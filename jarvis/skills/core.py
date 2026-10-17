"""Core: exit, reset, time/date, websites, web search, folders, autostart."""
from __future__ import annotations

import datetime as _dt
import logging
import re
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional

from .. import platform_utils as pu
from ..wake import normalize
from .base import Skill, ToolResult, fn, s

log = logging.getLogger(__name__)

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n)) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def pct(x) -> str:
    """12 -> '12 процентов', 2 -> '2 процента'."""
    n = int(round(float(x)))
    return f"{n} {plural_ru(n, 'процент', 'процента', 'процентов')}"


def units(x, one: str, few: str, many: str) -> str:
    n = int(round(float(x)))
    return f"{n} {plural_ru(n, one, few, many)}"


def say_time(now: _dt.datetime) -> str:
    h, m = now.hour, now.minute
    hours = f"{h} {plural_ru(h, 'час', 'часа', 'часов')}"
    if m == 0:
        return f"Сейчас ровно {hours}."
    return f"Сейчас {hours} {m} {plural_ru(m, 'минута', 'минуты', 'минут')}."


def say_date(now: _dt.datetime) -> str:
    return f"Сегодня {WEEKDAYS[now.weekday()]}, {now.day} {MONTHS_GEN[now.month - 1]} {now.year} года."


EXIT_PHRASES = {
    "стоп", "выход", "выйти", "выключись", "отключись", "завершить работу", "заверши работу",
    "закройся", "до свидания", "стоп стоп", "выключайся", "exit", "quit",
}
EXIT_FIRST_WORDS = ("выход", "выключись", "отключись", "выключайся")
RESET_PHRASES = ("забудь все", "забудь разговор", "новый разговор", "очисти историю", "начнем сначала")
TIME_RE = re.compile(r"\b(который час|сколько (сейчас )?времени|сколько время|какое (сейчас )?время|скажи время|время сейчас)\b")
DATE_RE = re.compile(r"\b(какое (сегодня )?число|какая (сегодня )?дата|какой (сегодня )?день( недели)?|сегодняшн\w* дат\w*)\b")
OPEN_RE = re.compile(r"^(?:пожалуйста |ну |а )?(?:открой|открыть|запусти|запустить|покажи)(?: мне)?(?: пожалуйста)? (.+)$")
SEARCH_RE = re.compile(r"^(?:(?:загугли|погугли|гугли)(?: мне)?|(?:найди|поищи|открой поиск)(?: мне)? (?:в гугле|в яндексе|в браузере))\s+(.+)$")
DOMAIN_RE = re.compile(r"^(?:https?://)?((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(/\S*)?$")
AUTOSTART_RE = re.compile(r"\b(включи|выключи|отключи|убери|добавь)\b.*\b(автозагрузк\w*|автозапуск\w*)")

FOLDERS = {
    "загрузки": "Downloads", "загрузок": "Downloads", "документы": "Documents", "рабочий стол": "Desktop",
    "изображения": "Pictures", "картинки": "Pictures", "фото": "Pictures", "музыку": "Music",
    "видео": "Videos", "заметки": "@notes", "папку заметок": "@notes", "папку с кодом": "@code",
    "модели": "@models", "папку моделей": "@models",
}


def folder_path(key: str) -> Path:
    if key.startswith("@"):
        return pu.data_dir({"@notes": "Notes", "@code": "Code", "@models": "Models"}[key])
    if key == "Documents":
        return pu.documents_dir()
    return Path.home() / key


class CoreSkill(Skill):
    name = "core"

    def __init__(self, ctx, open_url=webbrowser.open, now=_dt.datetime.now):
        super().__init__(ctx)
        t = ctx.cfg.get("tools", {})
        self.websites = {normalize(k): v for k, v in (t.get("websites") or {}).items()}
        self.search_url = t.get("search_url", "https://yandex.ru/search/?text={query}")
        self.allow_sites = bool(t.get("allow_open_websites", True))
        self._open_url = open_url
        self._now = now

    def open_site(self, url: str, spoken: str) -> ToolResult:
        if not self.allow_sites:
            return ToolResult("Открывать сайты мне запрещено в настройках.")
        if not url.startswith(("http://", "https://")):
            return ToolResult("Такую ссылку я открывать не буду.")
        try:
            self._open_url(url)
        except Exception as e:
            log.error("open url failed: %s", e)
            return ToolResult("Не получилось открыть браузер.")
        return ToolResult(f"Открываю {spoken}.")

    def web_search(self, query: str) -> ToolResult:
        url = self.search_url.replace("{query}", urllib.parse.quote_plus(query))
        r = self.open_site(url, "поиск")
        if r.reply.startswith("Открываю"):
            r.reply = f"Ищу: {query}."
        return r

    def set_autostart(self, on: bool) -> ToolResult:
        if not pu.IS_WIN:
            return ToolResult("Автозагрузка настраивается только в Windows.")
        ok = pu.set_autostart(on, self.ctx.project_root)
        if not ok:
            return ToolResult("Не получилось изменить автозагрузку.")
        return ToolResult("Автозагрузку включил." if on else "Автозагрузку выключил.")

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        words = t.split()
        if t in EXIT_PHRASES or (len(words) <= 3 and words[0] in EXIT_FIRST_WORDS):
            return ToolResult("Окей, отключаюсь. Пока!", action="exit")
        if any(p in t for p in RESET_PHRASES):
            return ToolResult("Окей, начинаем с чистого листа.", action="reset")
        m = AUTOSTART_RE.search(t)
        if m:
            return self.set_autostart(m.group(1) in ("включи", "добавь"))
        if TIME_RE.search(t):
            return ToolResult(say_time(self._now()))
        if DATE_RE.search(t):
            return ToolResult(say_date(self._now()))
        m = SEARCH_RE.match(t)
        if m and not re.search(r"\b(на ютуб\w*|в ютуб\w*|youtube|видео)\b", t):
            return self.web_search(m.group(1).strip())
        m = OPEN_RE.match(t)
        if not m:
            return None
        target = re.sub(r"^(?:сайт|страницу|папку|приложение|программу)\s+", "", m.group(1).strip())
        for name, key in FOLDERS.items():
            if target == name or target == "папку " + name:
                path = folder_path(key)
                pu.open_path(path)
                return ToolResult(f"Открываю {name}.")
        tw = target.split()
        for name, url in self.websites.items():
            if target == name or target.startswith(name + " ") or (" " not in name and name in tw):
                return self.open_site(url, name)
        low = re.sub(r"\s+(?:точка|dot)\s+", ".", (text or "").lower())
        for token in re.split(r"[\s,!?;«»\"']+", low):
            dm = DOMAIN_RE.match(token.strip(". "))
            if dm:
                return self.open_site("https://" + dm.group(1) + (dm.group(2) or ""), dm.group(1))
        return None

    def tools(self) -> list[dict]:
        return [
            fn("web_search", "Поиск в интернете в браузере (Google/Яндекс).", {"query": s("что искать")}, ["query"]),
            fn("open_website", "Открыть сайт по адресу.", {"url": s("домен или https-адрес, напр. habr.com")}, ["url"]),
        ]

    def call(self, name: str, args: dict) -> Optional[ToolResult]:
        if name == "web_search":
            return self.web_search(str(args.get("query", "")))
        if name == "open_website":
            url = str(args.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            dom = urllib.parse.urlparse(url).netloc
            if not DOMAIN_RE.match(dom):
                return ToolResult("Не понял адрес сайта.")
            return self.open_site(url, dom)
        return None
