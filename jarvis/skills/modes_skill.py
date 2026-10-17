"""Profiles: work / game / night. Voice, tray and GUI switch; auto game mode for fullscreen games."""
from __future__ import annotations

import logging
import re
import threading
from typing import Optional

from .. import platform_utils as pu
from .base import Skill, ToolResult, fn, s

log = logging.getLogger(__name__)

MODE_RE = re.compile(r"\b(?:включи|режим|перейди в|сделай)?\s*(игровой|рабочий|обычный|ночной|дневной)\s+режим\b|^режим (игры|работы|ночь)$")

FUZZY_RE = re.compile(r"^(?:включи|перейди в|сделай|поставь)?\s*(\w+)\s+режим\w*$")


def fuzzy_mode(t: str) -> Optional[str]:
    """Misheard mode names: 'ничное режим', 'игравой режим' -> closest of the known words."""
    import difflib
    m = FUZZY_RE.match(t)
    if not m:
        return None
    best = difflib.get_close_matches(m.group(1), ["игровой", "рабочий", "обычный", "ночной", "дневной"], n=1, cutoff=0.6)
    return best[0] if best else None


def fullscreen_game() -> bool:
    """Foreground window covers the whole monitor and isn't a browser/desktop/etc."""
    if not pu.IS_WIN:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        from ..screen import foreground_window
        from .game_skill import NON_GAMES
        exe, title, rect = foreground_window()
        if not rect or not exe or NON_GAMES.search(exe):
            return False
        user32 = ctypes.windll.user32
        hmon = user32.MonitorFromWindow(user32.GetForegroundWindow(), 2)

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        m = mi.rcMonitor
        return rect[0] <= m.left and rect[1] <= m.top and rect[2] >= m.right and rect[3] >= m.bottom
    except Exception as e:
        log.debug("fullscreen check: %s", e)
        return False


def steam_game_running() -> bool:
    if not pu.IS_WIN:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return int(winreg.QueryValueEx(k, "RunningAppID")[0]) != 0
    except Exception:
        return False


def in_window(now_hm: str, start: str, end: str) -> bool:
    """'HH:MM' window, may wrap past midnight."""
    return (start <= now_hm < end) if start <= end else (now_hm >= start or now_hm < end)


class ModesSkill(Skill):
    name = "modes"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.cfg = ctx.cfg.get("profiles", {})
        self._stop = threading.Event()
        threading.Thread(target=self._watch, name="jarvis-modewatch", daemon=True).start()

    def _set(self, name: str, auto: bool = False) -> str:
        setter = self.ctx.extra.get("set_profile")
        return setter(name, auto=auto) if setter else "Режимы недоступны."

    def _night_tick(self, cur: str) -> None:
        sch = self.cfg.get("night_schedule") or {}
        if not sch.get("enabled"):
            return
        import datetime as _dt
        inside = in_window(_dt.datetime.now().strftime("%H:%M"), str(sch.get("start", "23:00")), str(sch.get("end", "07:00")))
        if inside and cur == "work" and not self._night_auto:
            self._night_auto = True
            self._set("night", auto=True)
        elif not inside and self._night_auto:
            self._night_auto = False
            if cur == "night":
                self._set("work")

    def _watch(self):
        prev = None
        self._night_auto = False
        while not self._stop.wait(10):
            try:
                cur = (self.ctx.extra.get("get_profile") or (lambda: "work"))()
                self._night_tick(cur)
                if not (self.cfg.get("auto_game_mode", True) and pu.IS_WIN):
                    continue
                playing = fullscreen_game() or steam_game_running()
                if playing and cur == "work":
                    prev = cur
                    self._set("game", auto=True)
                elif not playing and cur == "game" and prev is not None:
                    a = self.ctx.extra.get("assistant")
                    if a is None or a.profile_auto:
                        self._set(prev)
                    prev = None
            except Exception as e:
                log.debug("game watch: %s", e)

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        m = MODE_RE.search(t)
        word = (m.group(1) or m.group(2)) if m else fuzzy_mode(t)
        if not word:
            return None
        name = {"игровой": "game", "игры": "game", "ночной": "night", "ночь": "night"}.get(word, "work")
        return ToolResult(self._set(name))

    def tools(self):
        return [fn("set_mode", "Переключить режим ассистента.", {"mode": s("режим", ["work", "game", "night"])}, ["mode"])]

    def call(self, name, args):
        if name == "set_mode":
            return ToolResult(self._set(str(args.get("mode", "work"))))
        return None
