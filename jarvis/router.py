"""Command router: keyword fast path over isolated skills + Ollama tool calling."""
from __future__ import annotations

import importlib
import logging
from typing import Callable, Optional

from .skills.base import Confirm, Skill, SkillContext, ToolResult, is_no, is_yes
from .wake import normalize

log = logging.getLogger(__name__)

# (config section with `enabled`, module, class). ORDER MATTERS: first match wins.
SKILLS = [
    ("diagnostics", "diag_skill", "DiagSkill"),
    ("lumo", "lumo_skill", "LumoSkill"),
    ("profiles", "modes_skill", "ModesSkill"),
    ("alerts", "alerts_skill", "AlertsSkill"),
    ("dictate", "dictate_skill", "DictateSkill"),
    ("translator", "translator_skill", "TranslatorSkill"),
    ("monitor", "monitor_skill", "MonitorSkill"),
    ("audio_devices", "audio_devices_skill", "AudioDevicesSkill"),
    ("files", "files_skill", "FilesSkill"),
    ("memory", "memory_skill", "MemorySkill"),
    ("notes", "notes_skill", "NotesSkill"),
    ("planner", "planner_skill", "PlannerSkill"),
    ("timers", "timers_skill", "TimersSkill"),
    ("weather", "weather_skill", "WeatherSkill"),
    ("system", "system_skill", "SystemSkill"),
    ("smarthome", "smarthome_skill", "SmartHomeSkill"),
    ("telegram", "telegram_skill", "TelegramSkill"),
    ("media", "media_skill", "MediaSkill"),
    ("tools", "core", "CoreSkill"),
    ("calc", "calc_skill", "CalcSkill"),
    ("clipboard", "clipboard_skill", "ClipboardSkill"),
    ("screen", "screen_skill", "ScreenSkill"),
    ("game", "game_skill", "GameSkill"),
    ("web_search", "web_skill", "WebSkill"),
    ("code", "code_skill", "CodeSkill"),
    ("cad", "cad_skill", "CadSkill"),
    ("apps", "apps_skill", "AppsSkill"),
]


class Router:
    def __init__(self, ctx: SkillContext):
        self.ctx = ctx
        self.skills: list[Skill] = []
        self.by_name: dict[str, Skill] = {}
        self.failed: dict[str, str] = {}
        self.pending_confirm: Optional[Confirm] = None
        self.pending_expect: Optional[Callable[[str], ToolResult]] = None
        for section, module, cls in SKILLS:
            sec = ctx.cfg.get(section, {}) or {}
            if not sec.get("enabled", section in ("tools",)):
                continue
            try:
                mod = importlib.import_module(f".skills.{module}", __package__)
                skill = getattr(mod, cls)(ctx)
                self.skills.append(skill)
                self.by_name[skill.name] = skill
            except Exception as e:     # isolated: one broken module doesn't break the app
                self.failed[section] = str(e)
                log.warning("Модуль '%s' отключён: %s", section, e)
        # share services between skills
        if "web" in self.by_name:
            ctx.extra["web"] = self.by_name["web"].web
        if "screen" in self.by_name:
            ctx.extra["screen"] = self.by_name["screen"].reader
        if "apps" in self.by_name:
            ctx.extra["launcher"] = self.by_name["apps"].launcher
        log.info("Модули: %s", ", ".join(s.name for s in self.skills))

    def start_background(self) -> None:
        planner = self.by_name.get("planner")
        if planner:
            planner.start()

    @property
    def awaiting_input(self) -> bool:
        """True while a skill expects the next phrase without the wake word
        (confirmation, dictation, translator conversation...)."""
        if self.pending_confirm or self.pending_expect:
            return True
        return any(getattr(s, "awaiting", False) for s in self.skills)

    def stt_language(self):
        """Whisper language for the next phrase: 'default' (config) or None (auto) for the translator."""
        for s in self.skills:
            lang = getattr(s, "stt_language", None)
            if lang is not None and lang != "default":
                return None if lang == "auto" else lang
        return "default"

    def _finalize(self, r: Optional[ToolResult]) -> Optional[ToolResult]:
        if r is None:
            return None
        if r.confirm:
            self.pending_confirm = r.confirm
            r.reply = r.confirm.prompt
        if r.expect:
            self.pending_expect = r.expect
        return r

    def handle(self, text: str) -> Optional[ToolResult]:
        t = normalize(text)
        if not t:
            return None
        if self.pending_confirm:
            c, self.pending_confirm = self.pending_confirm, None
            if is_yes(t):
                return self._finalize(self._safe(c.on_yes))
            if is_no(t):
                return ToolResult(c.on_no_reply)
            # anything else: treat as a new command
        if self.pending_expect:
            cb, self.pending_expect = self.pending_expect, None
            return self._finalize(self._safe(lambda: cb(text)))
        # modal skills (dictation, translator) get every phrase first
        for skill in self.skills:
            if getattr(skill, "awaiting", False) and hasattr(skill, "handle_modal"):
                try:
                    r = skill.handle_modal(text, t)
                except Exception as e:
                    log.exception("modal %s: %s", skill.name, e)
                    r = None
                if r is not None:
                    return self._finalize(r)
        notes = self.by_name.get("notes")
        if notes and getattr(notes, "awaiting", False):
            return notes.handle_dictation(text, t)  # type: ignore[attr-defined]
        for skill in self.skills:
            try:
                r = skill.match(text, t)
            except Exception as e:
                log.exception("Модуль %s упал на '%s': %s", skill.name, text, e)
                continue
            if r is not None:
                log.debug("-> %s", skill.name)
                return self._finalize(r)
        return None

    @staticmethod
    def _safe(fn) -> ToolResult:
        try:
            return fn() or ToolResult("Готово.")
        except Exception as e:
            log.exception("action failed: %s", e)
            return ToolResult(f"Не получилось: {e}")

    # ------------------------------------------------------------ tool calling
    def tool_schemas(self) -> list[dict]:
        out = []
        for skill in self.skills:
            try:
                out += skill.tools()
            except Exception as e:
                log.debug("tools() %s: %s", skill.name, e)
        return out

    def call_tool(self, name: str, args: dict) -> Optional[ToolResult]:
        for skill in self.skills:
            if any(t["function"]["name"] == name for t in skill.tools()):
                return self._finalize(self._safe(lambda sk=skill: sk.call(name, args or {})))
        log.warning("Неизвестный инструмент: %s", name)
        return None
