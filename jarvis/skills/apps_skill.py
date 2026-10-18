"""Launch games (Steam/Epic) and any installed app by name."""
from __future__ import annotations

import re
import threading
from typing import Optional

from ..launcher import Launcher
from .base import Skill, ToolResult, fn, s

OPEN_RE = re.compile(r"^(?:пожалуйста |ну |а )?(?:открой|запусти|включи|врубай|вруби|давай поиграем в|хочу поиграть в|запускай)(?: мне)?(?: игру| приложение| программу)? (.+)$")
LIST_RE = re.compile(r"\b(какие|список|что за)\b.*\bигр\w*\b.*\b(установлен\w*|есть)|\bмои игры\b|\bсписок игр\b")
REFRESH_RE = re.compile(r"\bобнови (список|индекс) (игр|приложений|программ)\b")


class AppsSkill(Skill):
    name = "apps"

    def __init__(self, ctx):
        super().__init__(ctx)
        root = ctx.project_root
        self.launcher = Launcher({**ctx.cfg.get("apps", {}), "apps": ctx.cfg.get("tools", {}).get("apps", {})},
                                 aliases_path=(root / "aliases.yaml") if root else None)
        def build():
            from ..resources import lowest_thread_priority
            lowest_thread_priority()                    # 1.0.8: background indexing never competes with the user
            self.launcher.build()
        threading.Thread(target=build, name="jarvis-app-index", daemon=True).start()

    def open(self, name: str, strict: bool = False) -> Optional[ToolResult]:
        e, sc = self.launcher.find(name)
        if not e:
            return None if not strict else ToolResult(f"Не нашёл программу или игру «{name}».")
        try:
            self.launcher.launch(e)
        except Exception as ex:
            return ToolResult(f"Не получилось запустить {e.name}: {ex}")
        return ToolResult(f"Запускаю {e.name}.")

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if REFRESH_RE.search(t):
            n = self.launcher.build()
            return ToolResult(f"Обновил. Нашёл {n} программ и игр.")
        if LIST_RE.search(t):
            games = sorted({g.name for g in self.launcher.games()})
            if not games:
                return ToolResult("Не нашёл установленных игр в Steam или Epic.")
            more = f" и ещё {len(games) - 15}" if len(games) > 15 else ""
            return ToolResult(f"Установлено игр: {len(games)}. " + ", ".join(games[:15]) + more + ".")
        m = OPEN_RE.match(t)
        if m:
            return self.open(m.group(1))
        return None

    def tools(self):
        return [fn("launch_app", "Запустить установленную игру (Steam/Epic) или программу по названию.",
                   {"name": s("название игры или программы")}, ["name"])]

    def call(self, name, args):
        if name == "launch_app":
            return self.open(str(args.get("name", "")), strict=True)
        return None
