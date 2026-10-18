"""«назад» / «вперёд» (+ «три раза») for the window in front, and seeking in YouTube / YouTube Music (1.0.8).

* Explorer, browsers, Chromium/Electron windows (YouTube Music app, Discord, Steam, VS Code…): Alt+Left / Alt+Right;
* any other app: the Browser Back/Forward key (VK_BROWSER_BACK -> WM_APPCOMMAND APPCOMMAND_BROWSER_BACKWARD);
* «перемотай назад», «вперёд на 30 секунд», «назад на минуту»: YouTube ←/→ (5 s per press, player focused),
  YouTube Music Shift+←/→ (10 s per press).
«назад» is navigation; «предыдущее / предыдущий трек» stays the media key (media skill).
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from .. import keyinput
from ..explorer import EXPLORER_CLASSES
from ..windows import BROWSERS
from .base import Skill, ToolResult

NUM = {"один": 1, "одну": 1, "раз": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
       "восемь": 8, "девять": 9, "десять": 10, "пятнадцать": 15, "двадцать": 20, "тридцать": 30, "сорок": 40,
       "пятьдесят": 50, "шестьдесят": 60, "девяносто": 90, "сто": 100, "полминуты": 30}
TIMES = r"(?:\s+(?P<n>\d+|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять)\s+(?:раза|раз)|\s+(?P<twice>дважды|трижды))?"
NAV_RE = re.compile(rf"^(?:(?:ну|давай|а)\s+)*(?P<pre>дважды\s+|трижды\s+)?"
                    rf"(?:(?:вернись|сходи|перейди|шаг|страница|на\s+страницу)\s+)?"
                    rf"(?P<dir>назад|вперед|вперёд){TIMES}$")
SEEK_RE = re.compile(r"^(?:(?:ну|давай|а)\s+)*(?:(?P<verb>перемотай|перемотать|промотай|отмотай|мотни|прокрути)\s+)?"
                     r"(?P<dir>назад|вперед|вперёд)?\s*(?:на\s+)?(?P<amount>\d+|[а-я]+)?\s*"
                     r"(?P<unit>секунд\w*|сек|минут\w*|мин)?$")
CHROMIUM_CLASSES = {"Chrome_WidgetWin_1", "Chrome_WidgetWin_0", "MozillaWindowClass"}


def _num(w: Optional[str]) -> Optional[int]:
    if not w:
        return None
    return int(w) if w.isdigit() else NUM.get(w)


def parse_nav(t: str) -> Optional[tuple[str, int]]:
    """-> ("back"|"forward", times)"""
    m = NAV_RE.match(t.replace("ё", "е"))
    if not m:
        return None
    n = 1
    if m.group("n"):
        n = _num(m.group("n")) or 1
    elif m.group("twice") or m.group("pre"):
        n = 2 if (m.group("twice") or m.group("pre")).strip() == "дважды" else 3
    return ("back" if m.group("dir") == "назад" else "forward", max(1, min(n, 10)))


def parse_seek(t: str) -> Optional[tuple[str, int]]:
    """-> ("back"|"forward", seconds). Needs a seek verb or a time unit, so plain «назад» stays navigation."""
    m = SEEK_RE.match(t.replace("ё", "е"))
    whole = m is not None and m.group("amount") in ("минуту", "минутку", "полминуты")
    if not m or not (m.group("verb") or m.group("unit") or whole) or (m.group("verb") is None and m.group("dir") is None):
        return None
    amount, unit = m.group("amount"), m.group("unit") or ""
    if amount and _num(amount) is None and amount not in ("минуту", "минутку", "полминуты"):
        return None
    secs = 10
    if amount in ("минуту", "минутку"):
        secs = 60
    elif amount == "полминуты":
        secs = 30
    elif amount:
        secs = _num(amount) or 10
        if unit.startswith("мин"):
            secs *= 60
    elif unit.startswith("мин"):
        secs = 60
    return ("back" if (m.group("dir") or "вперед") == "назад" else "forward", max(1, min(secs, 600)))


def nav_combo(w) -> str:
    if w.cls in EXPLORER_CLASSES or w.exe in BROWSERS or w.cls in CHROMIUM_CLASSES:
        return "alt+{dir}"
    return "browser_{bdir}"


class NavSkill(Skill):
    name = "nav"

    def __init__(self, ctx, press: Callable[..., bool] = keyinput.press):
        super().__init__(ctx)
        self.press = press

    def _target(self):
        wm = self.ctx.extra.get("wm")
        if wm is None or getattr(wm, "b", None) is None:
            return None, None
        return wm, wm.active()

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        seek = parse_seek(t)
        nav = None if seek else parse_nav(t)
        if not (seek or nav):
            return None
        wm, w = self._target()
        if w is None:
            return None
        if keyinput.focus_target(wm) is None:
            return ToolResult("Не вижу окна, куда нажать.")
        if seek:
            return self.seek(w, *seek)
        assert nav is not None
        d, times = nav
        combo = nav_combo(w).format(dir="left" if d == "back" else "right",
                                    bdir="back" if d == "back" else "forward")
        ok = self.press(combo, times)
        return ToolResult("" if ok else "Не получилось нажать клавиши (только в Windows).")

    def seek(self, w, d: str, secs: int) -> ToolResult:
        title = (w.title or "").lower()
        if "youtube music" in title:
            combo, step = "shift+" + ("left" if d == "back" else "right"), 10
        elif "youtube" in title:
            combo, step = ("left" if d == "back" else "right"), 5
        else:
            return ToolResult("Перематывать умею только в YouTube и YouTube Music.")
        presses = max(1, round(secs / step))
        ok = self.press(combo, presses)
        return ToolResult("" if ok else "Не получилось перемотать.")
