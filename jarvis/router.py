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
    ("chat", "chat_skill", "ChatSkill"),                 # 1.0.8 «Режим беседы»: «давай поболтаем» (LLM answers)
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
    ("explorer", "explorer_skill", "ExplorerSkill"),     # before smarthome/media: «открой второе» by context
    ("nav", "nav_skill", "NavSkill"),                    # «назад/вперёд [N раз]», «перемотай назад на 30 секунд»
    ("keys", "keys_skill", "KeysSkill"),                 # «нажми энтер / контрол цэ / вниз три раза»
    ("hotkeys", "hotkeys_skill", "HotkeysSkill"),        # data-driven: jarvis/commands.yaml (+ user file)
    ("smarthome", "smarthome_skill", "SmartHomeSkill"),
    ("windows", "windows_skill", "WindowsSkill"),
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


# Second chance for the fast path: words that carry no command (mat, politeness, the name, filler)
# and frequent Whisper misspellings. Used ONLY when the phrase as heard matched nothing, so a working
# command is never changed by it.
FILLER_ANY = {
    "блин", "бля", "бл", "ска", "сцука", "сука", "блять", "блядь", "блиин", "нахрен", "нафиг", "нахер", "нахуй", "епта", "епт",
    "ептить", "ебаный", "ебаное", "ебаную", "ебучий", "гребаный", "чертов", "черт", "пожалуйста", "пожалуйсто",
    "плиз", "плз", "please", "слушай", "давай", "ка", "эй", "короче", "типа", "быстренько", "быстро", "срочно",
    "джарвис", "джарвиз", "жарвис", "джервис", "джарвес", "jarvis", "братан", "бро", "дружище", "друг", "уже",
    "мне", "ну", "ой", "ага",
}
FILLER_LEAD = {"а", "и", "так", "ладно", "окей", "ок", "хорошо", "слышь", "вот", "теперь", "щас", "сейчас"}
TYPOS = {"скока": "сколько", "скоко": "сколько", "сколька": "сколько", "щас": "сейчас", "ща": "сейчас",
         "счас": "сейчас", "щаc": "сейчас", "че": "что", "чо": "что", "громкасть": "громкость",
         "граммкость": "громкость", "громкостью": "громкость", "ютюб": "ютуб", "ютубчик": "ютуб",
         "музон": "музыку", "музончик": "музыку", "врубай": "включи", "вруби": "включи", "вырубай": "выключи",
         "выруби": "выключи", "отруби": "выключи", "поставъ": "поставь", "таймэр": "таймер", "напомнить": "напомни"}
_JOIN = {("по", "громче"): "погромче", ("по", "тише"): "потише", ("по", "тиши"): "потише",
         ("по", "громчи"): "погромче", ("по", "тихше"): "потише"}


def clean_command(t: str) -> str:
    """normalize()d phrase -> the same command without filler/mat, with typos fixed ('' if nothing left)."""
    words = [TYPOS.get(w, w) for w in t.split()]
    joined: list[str] = []
    for w in words:
        if joined and (joined[-1], w) in _JOIN:
            joined[-1] = _JOIN[(joined[-1], w)]
        else:
            joined.append(w)
    out = [w for w in joined if w not in FILLER_ANY]
    while out and out[0] in FILLER_LEAD:
        out.pop(0)
    return " ".join(out)


class Router:
    def __init__(self, ctx: SkillContext):
        self.ctx = ctx
        self.skills: list[Skill] = []
        self.by_name: dict[str, Skill] = {}
        self.failed: dict[str, str] = {}
        self.last_skill = ""                      # which skill answered the last phrase (timing log, tests)
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
        if "windows" in self.by_name:
            ctx.extra["wm"] = self.by_name["windows"].wm
        if "media" in self.by_name:
            ctx.extra["media"] = self.by_name["media"].media
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

    def after_turn(self) -> None:
        for s in self.skills:
            fn = getattr(s, "after_turn", None)
            if fn:
                try:
                    fn()
                except Exception as e:                  # noqa: BLE001
                    log.debug("after_turn %s: %s", s.name, e)

    @property
    def awaiting_timeout(self) -> Optional[float]:
        vals: list[float] = [float(v) for s in self.skills if getattr(s, "awaiting", False)
                             if (v := getattr(s, "awaiting_timeout", None))]
        return min(vals) if vals else None

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
        self.last_skill = ""
        from .noname import canonical
        t = canonical(normalize(text))                 # «открыл ютуб» (Whisper) -> «открой ютуб»
        if not t:
            return None
        if self.pending_confirm:
            c, self.pending_confirm = self.pending_confirm, None
            if is_yes(t):
                self.last_skill = "confirm"
                return self._finalize(self._safe(c.on_yes))
            if is_no(t):
                self.last_skill = "confirm"
                return ToolResult(c.on_no_reply)
            # anything else: treat as a new command
        if self.pending_expect:
            cb, self.pending_expect = self.pending_expect, None
            self.last_skill = "expect"
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
                    self.last_skill = skill.name
                    return self._finalize(r)
        notes = self.by_name.get("notes")
        if notes and getattr(notes, "awaiting", False):
            self.last_skill = "notes"
            return notes.handle_dictation(text, t)  # type: ignore[attr-defined]
        r = self._match(text, t)
        if r is None:
            cleaned = clean_command(t)
            if cleaned and cleaned != t:
                r = self._match(cleaned, cleaned)
                if r is not None:
                    log.debug("быстрый путь после очистки: %r -> %r", t, cleaned)
        if r is None:
            from . import fuzzy
            from .noname import canonical
            for v in fuzzy.variants(cleaned or t):
                r = self._match(v, canonical(v))
                if r is not None:
                    log.info("Команда исправлена: %r -> %r", t, v)
                    break
            if r is None and fuzzy.looks_like_command(cleaned or t):
                self.last_skill = "unknown"
                log.info("Похоже на команду, но не понял: %r — в ИИ не отправляю", t)
                return ToolResult("Не понял команду.")
        return self._finalize(r) if r is not None else None

    def _match(self, text: str, t: str) -> Optional[ToolResult]:
        for skill in self.skills:
            try:
                r = skill.match(text, t)
            except Exception as e:
                log.exception("Модуль %s упал на '%s': %s", skill.name, text, e)
                continue
            if r is not None:
                log.debug("-> %s", skill.name)
                self.last_skill = skill.name
                return r
        return None

    @staticmethod
    def _safe(fn) -> ToolResult:
        try:
            return fn() or ToolResult("Готово.")
        except Exception as e:
            log.exception("action failed: %s", e)
            return ToolResult(f"Не получилось: {e}")

    # ------------------------------------------------------------ tool calling
    # 1.0.8 (llm.tools: core, «Нагрузка» обычная/лёгкая): only the tools the model really needs are offered.
    # Direct commands («открой», «пауза», «закрой ютуб», «громкость 30») are handled by the fast path before the LLM,
    # so their schemas only cost prompt tokens (28 tools ≈ 1.9k of the 2.3-2.4k token prompt in users' logs).
    CORE_TOOLS = {"web_answer", "weather", "remember_fact", "add_task", "list_tasks", "set_timer", "play_music",
                  "play_video", "open_website", "launch_app", "calculate", "add_note", "smart_device", "look_at_screen"}

    def tool_schemas(self) -> list[dict]:
        which = str((self.ctx.cfg.get("llm") or {}).get("tools") or "all")
        out = []
        for skill in self.skills:
            try:
                out += skill.tools()
            except Exception as e:
                log.debug("tools() %s: %s", skill.name, e)
        if which == "core":
            out = [t for t in out if t["function"]["name"] in self.CORE_TOOLS]
        return out

    def call_tool(self, name: str, args: dict) -> Optional[ToolResult]:
        for skill in self.skills:
            if any(t["function"]["name"] == name for t in skill.tools()):
                return self._finalize(self._safe(lambda sk=skill: sk.call(name, args or {})))
        log.warning("Неизвестный инструмент: %s", name)
        return None
