"""Game helper: detects the active game and answers with web guides / screen hints."""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..screen import foreground_window
from .base import Skill, ToolResult

log = logging.getLogger(__name__)

GAME_Q_RE = re.compile(r"\b(в игре|в этой игре|как пройти|как убить|как победить|где найти|где взять|как скрафтить|"
                       r"билд|прокачк\w*|гайд|секрет\w*|босс\w*|квест\w*|подскажи по игре|игровой вопрос)\b")
LOOK_RE = re.compile(r"\b(посмотри на экран и подскажи|что мне тут делать|подскажи по экрану|глянь в игре)\b")
NON_GAMES = re.compile(r"explorer|chrome|firefox|msedge|code|python|cmd|powershell|windowsterminal|telegram|discord|"
                       r"steam\.exe|notepad|devenv|arduino|openscad|solidworks|sldworks", re.I)


class GameSkill(Skill):
    name = "game"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.forced = False      # set by the game hotkey: next phrase is a game question

    def current_game(self) -> str:
        exe, title, _ = foreground_window()
        if exe and not NON_GAMES.search(exe):
            return title or exe.rsplit(".", 1)[0]
        # Steam running game (registry RunningAppID)
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                appid = int(winreg.QueryValueEx(k, "RunningAppID")[0])
            if appid:
                apps = self.ctx.extra.get("launcher")
                if apps:
                    for e in apps.games():
                        if e.target.endswith(f"/{appid}"):
                            return e.name
        except Exception:
            pass
        return self.ctx.extra.get("last_game", "")

    def answer(self, question: str, look: bool = False) -> ToolResult:
        game = self.current_game()
        if game:
            self.ctx.extra["last_game"] = game
        web = self.ctx.extra.get("web")
        screen = self.ctx.extra.get("screen")
        def work():
            ctx_line = f"Игра: {game}." if game else ""
            if look and screen:
                return screen.ask(f"{ctx_line} Посмотри на скриншот игры и дай короткую подсказку по-русски: {question}",
                                  f"{ctx_line} Look at this game screenshot and give a short hint: {question}", True)
            if web:
                ans = web.answer(question, context=f"{game} гайд" if game else "игра гайд")
                if ans.sources and self.ctx.extra.get("on_sources"):
                    self.ctx.extra["on_sources"]([(x.title, x.url) for x in ans.sources[:5]])
                return ans.answer
            return self.ctx.llm.complete([{"role": "system", "content": self.ctx.llm.system_prompt},
                                          {"role": "user", "content": f"{ctx_line} Вопрос по игре: {question}"}])
        return ToolResult(f"Сейчас подскажу{' по ' + game if game else ''}.", then=work)

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if self.forced:
            self.forced = False
            return self.answer(text, look=bool(LOOK_RE.search(t)))
        if LOOK_RE.search(t):
            return self.answer(text, look=True)
        if GAME_Q_RE.search(t):
            return self.answer(text)
        return None
