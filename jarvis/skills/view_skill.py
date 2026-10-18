"""1.0.9: YouTube player view by voice (with or without the name, no-name group «media»):
  «полный экран», «на весь экран», «разверни видео»        -> YouTube fullscreen (f)
  «обычный экран», «малый экран», «выйди из полного экрана», «сверни видео» -> leave fullscreen (Esc)
  «кинотеатр», «широкий экран» (t), «мини-плеер» (i), «субтитры» (c)
Idempotent: the fullscreen state is read from the window (its rect covers the monitor), so «полный экран» twice does
nothing the second time. Keys go to the player, never to the search box: if focus is in a text field, focus moves to
the player through UI Automation (no mouse click); if that fails nothing is pressed.
A browser in front but NOT on YouTube: «полный экран» / «обычный экран» = F11. YouTube Music: F11 (no f shortcut).
Bare «сверни» stays «minimize the window» (windows skill)."""
from __future__ import annotations

import re
from typing import Callable, Optional

from .. import keyinput, uia
from ..windows import BROWSERS
from .base import Skill, ToolResult

V = r"(?:видео|видос|ролик|ютуб|youtube|плеер)"
VV = r"(?:видео|видос\w*|ролик|плеер)"     # «разверни/сверни ютуб» stay window commands (maximize / minimize)
FULL_RE = re.compile(rf"^(?:(?:сделай|включи|открой|давай|разверни|раскрой|перейди в)\s+)?(?:на\s+|в\s+)?"
                     rf"(?:весь\s+экран|полный\s+экран|полноэкранный(?:\s+режим)?|фулл?\s*скрин|full\s*screen)"
                     rf"(?:\s+{V})?$|^(?:разверни|раскрой)\s+{VV}(?:\s+на\s+весь\s+экран)?$|^(?:разверни|раскрой)\s+{V}\s+на\s+весь\s+экран$|^{V}\s+на\s+весь\s+экран$")
EXIT_RE = re.compile(rf"^(?:(?:сделай|включи|верни|давай)\s+)?(?:малый|обычный|маленький|нормальный)\s+(?:экран|режим)$|"
                     rf"^(?:выйди|выйти|выход|убери|выключи|закрой)\s+(?:из\s+)?(?:полн\w+\s+экран\w*|полноэкранн\w+(?:\s+режим\w*)?|"
                     rf"фулл?\s*скрин\w*)$|^(?:сверни|уменьши)\s+{VV}$|^из\s+полного\s+экрана$")
THEATER_RE = re.compile(r"^(?:(?:включи|выключи|сделай|режим)\s+)*(?:кинотеатр\w*|широкий\s+(?:экран|режим))$")
MINI_RE = re.compile(r"^(?:(?:включи|выключи|сделай|открой)\s+)?(?:мини\s*[- ]?\s*плеер\w*|маленький\s+плеер)$")
SUBS_RE = re.compile(r"^(?:(?:включи|выключи|покажи|убери|сделай)\s+)?(?:субтитры|сабы|субтитров)$")


def parse_view(t: str) -> Optional[str]:
    t = " ".join((t or "").lower().replace("ё", "е").replace("-", " ").split())
    for kind, rx in (("full", FULL_RE), ("exit", EXIT_RE), ("theater", THEATER_RE), ("mini", MINI_RE),
                     ("subs", SUBS_RE)):
        if rx.match(t):
            return kind
    return None


KEYS = {"theater": "t", "mini": "i", "subs": "c"}


class ViewSkill(Skill):
    name = "view"

    def __init__(self, ctx, press: Callable[..., bool] = keyinput.press,
                 focus_in_edit: Callable[[], Optional[bool]] = uia.focus_in_edit,
                 focus_player: Callable[[int], bool] = uia.focus_player):
        super().__init__(ctx)
        self.press, self.focus_in_edit, self.focus_player = press, focus_in_edit, focus_player

    def _say(self, text: str) -> ToolResult:
        return ToolResult("" if self.ctx.extra.get("noname") else text)

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        kind = parse_view(t)
        if kind is None:
            return None
        wm = self.ctx.extra.get("wm")
        if wm is None or getattr(wm, "b", None) is None:
            return None
        w = wm.active()
        if w is None:
            return None
        title = (w.title or "").lower()
        browser = w.exe in BROWSERS
        yt = browser and "youtube" in title and "youtube music" not in title
        if not browser:
            return None                                   # «разверни видео» in a player app etc.: other skills
        try:
            covers = bool(wm.b.fullscreen(w.hwnd)[0])
        except Exception:                                 # noqa: BLE001
            covers = False
        if keyinput.focus_target(wm) is None:
            return self._say("Не вижу окна, куда нажать.")
        if kind in ("full", "exit") and not yt:          # any site / YouTube Music: browser fullscreen
            if (kind == "full") == covers:
                return ToolResult("")                     # already as asked
            return ToolResult("" if self.press("f11") else "Не получилось нажать клавиши (только в Windows).")
        if not yt:
            return self._say("Это работает только в YouTube.")
        if kind == "full" and covers or kind == "exit" and not covers:
            return ToolResult("")
        if kind == "exit":
            return ToolResult("" if self.press("esc") else "Не получилось нажать клавиши (только в Windows).")
        if not self._player_focused(w.hwnd):
            return self._say("Курсор в поле поиска — кликни по видео, и повтори.")
        key = "f" if kind == "full" else KEYS[kind]
        return ToolResult("" if self.press(key) else "Не получилось нажать клавиши (только в Windows).")

    def _player_focused(self, hwnd: int) -> bool:
        """Never type «f» into the search box."""
        if self.focus_in_edit() is not True:
            return True
        self.focus_player(hwnd)
        if self.focus_in_edit() is not True:
            return True
        self.press("esc")                                 # closes the suggestions; focus may leave the box
        return self.focus_in_edit() is False
