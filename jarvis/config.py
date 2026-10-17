"""Configuration: config.yaml (documented defaults) + settings.local.yaml (changes made in the GUI)."""
from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_SETTINGS = PROJECT_ROOT / "settings.local.yaml"

DEFAULT_SYSTEM_PROMPT = (
    "Ты — Джарвис, личный голосовой ассистент мистера Флоки. Живёшь на его компьютере и работаешь офлайн. "
    "Стиль: как Джарвис из «Железного человека», но без чопорности — общаешься на «ты», неформально, по-дружески, "
    "с живым юмором и иронией. Иногда, к месту, можешь назвать его «сэр» или «мистер Флоки», но не в каждой фразе. "
    "Понимаешь сленг, мемы, сарказм и шутки, шутишь в ответ. Русский мат — нормальная часть живой речи: если "
    "собеседник матерится, спокойно поддерживай тот же стиль и сам можешь крепко выразиться к месту. Никогда не "
    "читай нотаций про лексику и не морализируй. Ответы будут произнесены вслух, поэтому коротко: обычно одно-три "
    "предложения, как в живом разговоре. Без markdown, списков, эмодзи и кода, если прямо не просят. "
    "Отвечай по-русски. Если чего-то не знаешь — честно скажи, без выдумок."
)

DEFAULTS: dict[str, Any] = {
    "assistant": {"name": "Джарвис", "user_name": "Флоки", "mode": "wake", "wake_reply": "Слушаю",
                  "greeting": "{hello}, мистер {user_name}, чего желаете?", "greeting_time_of_day": False,
                  "greet_on_start": True, "follow_up_seconds": 6, "extra_wake_words": []},
    "stt": {"model": "small", "device": "auto", "compute_type": "auto", "language": "ru", "beam_size": 1,
            "download_root": "models/whisper", "initial_prompt": "Джарвис, привет."},
    "audio": {"input_device": None, "output_device": None, "sample_rate": 16000, "vad": "webrtc",
              "vad_aggressiveness": 2, "energy_threshold": 0.015, "frame_ms": 30, "silence_ms": 800,
              "min_speech_ms": 250, "max_utterance_s": 15, "pre_roll_ms": 300},
    "llm": {"host": "http://localhost:11434", "model": "qwen2.5:7b", "temperature": 0.8, "num_ctx": 4096,
            "max_history_messages": 16, "timeout_s": 120, "keep_alive": "30m", "aux_keep_alive": "1m",
            "tool_calling": True, "system_prompt": DEFAULT_SYSTEM_PROMPT},
    "tts": {"engine": "piper", "piper_voice": "ru_RU-denis-medium", "voices_dir": "voices", "length_scale": 1.1,
            "fx": {"enabled": True, "intensity": 0.35},
            "volume": 1.0, "pyttsx3_rate": 180, "pyttsx3_voice": "", "speak_in_text_mode": True},
    "tools": {"enabled": True, "websites": {}, "apps": {}},
    "gui": {"enabled": True, "always_on_top": False, "start_minimized": False, "close_to_tray": True,
            "hotkey_show": "ctrl+alt+j", "hotkey_ptt": "ctrl+alt+space", "hotkey_game": "ctrl+alt+g", "fps": 30},
    "autostart": {"enabled": True},
    "logging": {"level": "INFO", "file": "logs/jarvis.log"},
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_path(p: str | os.PathLike) -> Path:
    """Relative paths in the config are relative to the project root."""
    path = Path(os.path.expandvars(str(p))).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


CONFIG_VERSION = 2
REMOVED_SECTIONS = ("whatsapp", "comfyui", "faceswap", "video")


def migrate_local(data) -> tuple[dict, bool]:
    """Upgrade an old settings.local.yaml to the current schema. Returns (data, changed)."""
    if not isinstance(data, dict):
        return {}, True
    ver = int(data.get("config_version", 1) or 1)
    changed = False
    if ver < 2:
        for sec in REMOVED_SECTIONS:              # features removed before 1.0
            data.pop(sec, None)
        a = data.get("assistant") or {}
        if "wake_words" in a:                     # old explicit list -> extra variants
            a["extra_wake_words"] = a.pop("wake_words")
        if str(a.get("name", "")).lower() in ("люмо", "lumo"):   # placeholder name from early builds
            a["name"] = "Джарвис"
        g = data.get("gui") or {}
        if "hotkey_mute" in g:
            g["hotkey_mic"] = g.pop("hotkey_mute")
        data["config_version"] = 2
        changed = True
    return data, changed


def raw_config(path: str | os.PathLike | None = None, use_local: bool = True) -> dict[str, Any]:
    """Merged config WITHOUT placeholder substitution (for the Settings window)."""
    cfg_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    if not cfg_path.exists():
        log.warning("Config %s not found, using defaults", cfg_path)
    try:
        user = _read_yaml(cfg_path)
    except Exception as e:      # broken config.yaml must not kill startup
        log.error("config.yaml повреждён (%s) — беру значения по умолчанию", e)
        user = {}
    cfg = deep_merge(DEFAULTS, user)
    if use_local:
        try:
            local, changed = migrate_local(_read_yaml(LOCAL_SETTINGS)) if LOCAL_SETTINGS.exists() else ({}, False)
            if changed:
                try:
                    with open(LOCAL_SETTINGS, "w", encoding="utf-8") as f:
                        yaml.safe_dump(local, f, allow_unicode=True, sort_keys=False)
                    log.info("settings.local.yaml обновлён до версии %d", CONFIG_VERSION)
                except OSError as e:
                    log.warning("Не удалось сохранить миграцию настроек: %s", e)
            cfg = deep_merge(cfg, local)
        except Exception as e:
            log.error("settings.local.yaml повреждён (%s) — игнорирую", e)
    if not str(cfg["llm"].get("system_prompt") or "").strip():
        cfg["llm"]["system_prompt"] = DEFAULT_SYSTEM_PROMPT
    cfg["llm"]["system_prompt_raw"] = cfg["llm"]["system_prompt"]
    return cfg


def render_prompt(cfg: dict) -> str:
    a = cfg["assistant"]
    return (str(cfg["llm"].get("system_prompt_raw") or cfg["llm"].get("system_prompt") or DEFAULT_SYSTEM_PROMPT)
            .replace("{name}", a.get("name", "Джарвис")).replace("{user_name}", a.get("user_name", "Флоки")))


def update_inplace(base: dict, changes: dict) -> None:
    """Deep-update a live config dict so skills holding sub-dicts see new values."""
    for k, v in (changes or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            update_inplace(base[k], v)
        else:
            base[k] = copy.deepcopy(v)


def load_config(path: str | os.PathLike | None = None, use_local: bool = True) -> dict[str, Any]:
    cfg = raw_config(path, use_local)
    # the prompt may reference the configurable names
    a = cfg["assistant"]
    cfg["llm"]["system_prompt"] = render_prompt(cfg)
    if a.get("name") and a["name"] not in str(cfg["stt"].get("initial_prompt") or ""):
        cfg["stt"]["initial_prompt"] = f"{a['name']}, привет. " + str(cfg["stt"].get("initial_prompt") or "")
    return cfg


def save_local_settings(changes: dict) -> None:
    """Merge `changes` into settings.local.yaml (keeps config.yaml with its comments untouched)."""
    try:
        current = _read_yaml(LOCAL_SETTINGS)
        if not isinstance(current, dict):
            raise ValueError("not a mapping")
    except Exception as e:
        log.error("settings.local.yaml повреждён (%s) — сохраняю копию и начинаю заново", e)
        try:
            LOCAL_SETTINGS.replace(LOCAL_SETTINGS.with_suffix(".yaml.broken"))
        except OSError:
            pass
        current = {}
    merged = deep_merge(current, changes)
    merged["config_version"] = CONFIG_VERSION
    tmp = LOCAL_SETTINGS.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:     # atomic: a crash never leaves half a file
        yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, LOCAL_SETTINGS)
