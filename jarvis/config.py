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
                  "greet_on_start": True, "follow_up_seconds": 0, "extra_wake_words": [],
                  "conversation": {"enabled": True, "idle_s": 22},
                  "noname": {"enabled": True, "groups": {g: True for g in (
                      "media", "youtube", "nav", "browser", "windows", "explorer", "keys", "dictation", "apps",
                      "music", "smarthome", "search")}},
                  "request_timeout_s": 6, "request_silence_ms": 1200, "max_request_s": 15, "tts_tail_ms": 500,
                  "ack_after_s": 2.5},
    "stt": {"model": "small", "device": "cpu", "compute_type": "auto", "language": "ru", "beam_size": 1,
            "download_root": "models/whisper", "initial_prompt": "", "vad_filter": True},
    "audio": {"input_device": None, "output_device": None, "sample_rate": 16000, "vad": "webrtc",
              "vad_aggressiveness": 2, "energy_threshold": 0.015, "frame_ms": 30, "silence_ms": 500,
              "min_speech_ms": 250, "max_utterance_s": 15, "pre_roll_ms": 300},
    "llm": {"host": "http://localhost:11434", "model": "qwen2.5:7b", "temperature": 0.6, "num_ctx": None, "placement": "gpu",
            "num_predict": 512, "top_p": 0.9, "top_k": 40, "repeat_penalty": 1.1,
            "max_history_messages": 16, "timeout_s": 120, "keep_alive": None, "aux_keep_alive": "1m",
            "num_thread": None, "tools": None, "light_model": False,
            "tool_calling": True, "system_prompt": DEFAULT_SYSTEM_PROMPT},
    "tts": {"engine": "piper", "piper_voice": "ru_RU-denis-medium", "voices_dir": "voices", "length_scale": 1.1,
            "fx": {"enabled": True, "intensity": 0.35},
            "volume": 1.0, "pyttsx3_rate": 180, "pyttsx3_voice": "", "speak_in_text_mode": True},
    "tools": {"enabled": True, "websites": {}, "apps": {}},
    "windows": {"enabled": True, "never_close": []},
    "explorer": {"enabled": True},
    "view": {"enabled": True},                 # 1.0.9 «полный экран», «кинотеатр», «субтитры»
    "hotkeys": {"enabled": True},
    "nav": {"enabled": True},
    "chat": {"enabled": True},              # module switch; the user toggle is assistant.conversation.enabled
    "keys": {"enabled": True},
    "gui": {"enabled": True, "always_on_top": False, "start_minimized": False, "close_to_tray": True,
            "hotkey_show": "ctrl+alt+j", "hotkey_ptt": "ctrl+alt+space", "hotkey_game": "ctrl+alt+g", "hotkey_stop": "ctrl+alt+x", "fps": None},
    "load": {"preset": "balanced", "log_every_s": 300},
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


CONFIG_VERSION = 9
MIGRATION_NOTICES: list[str] = []      # shown once by the assistant after start (1.0.9)
OLD_PROMPT = "Джарвис, привет. Джарвис, включи музыку."
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
        ver = 2
        changed = True
    if ver < 3:                                   # 1.0.4: old LLM defaults caused Chinese drift
        llm_raw = data.get("llm")
        llm: dict = llm_raw if isinstance(llm_raw, dict) else {}
        if llm.get("temperature") in (0.8, "0.8"):
            llm.pop("temperature")                # -> new default 0.6 (a value the user changed is kept)
        try:
            if int(llm.get("num_ctx", 0) or 0) and int(llm["num_ctx"]) <= 4096:
                llm.pop("num_ctx")                # -> 8192: system prompt + tools + history must fit
        except (TypeError, ValueError):
            llm.pop("num_ctx", None)
        data["config_version"] = 3
        changed = True
    if ver < 4:                                   # 1.0.4: strict wake word — no follow-up listening by default
        a = data.get("assistant")
        if isinstance(a, dict) and str(a.get("follow_up_seconds", "")).strip() in ("6", "6.0"):
            a.pop("follow_up_seconds")            # old default pinned by Settings -> new default 0 (off)
        data["config_version"] = 4
        changed = True
    if ver < 5:                                   # 1.0.5: 8192 context did not fit next to Whisper in 6 GB VRAM
        llm5 = data.get("llm")
        if isinstance(llm5, dict) and str(llm5.get("num_ctx", "")).strip() == "8192":
            llm5.pop("num_ctx")                   # 1.0.4 default pinned by Settings -> 6144 (a user's own value is kept)
        data["config_version"] = 5
        changed = True
    if ver < 6:                                   # 1.0.6: GPU Whisper without CUDA libs broke voice mode
        stt = data.get("stt")
        if isinstance(stt, dict):
            if str(stt.get("device", "")).strip().lower() in ("cuda", "auto"):
                stt.pop("device")                 # -> new default "cpu" (GPU is opt-in, falls back safely)
            if str(stt.get("compute_type", "")).strip().lower() in ("float16", "int8_float16"):
                stt.pop("compute_type")           # GPU-only types fail on the CPU fallback -> auto
        data["config_version"] = 6
        changed = True
    if ver < 7:                                   # 1.0.7: the old default prompt made Whisper «hear» «Джарвис, привет»
        stt = data.get("stt")
        if isinstance(stt, dict) and ("привет" in str(stt.get("initial_prompt", "")).lower()
                                      or str(stt.get("initial_prompt", "")).strip() == OLD_PROMPT):
            stt.pop("initial_prompt")             # greeting-like prompts are echoed by Whisper as phantom input
        data["config_version"] = 7
        changed = True
    if ver < 8:                                   # 1.0.8: «Нагрузка» presets; old defaults pinned by Settings -> preset
        llm = data.get("llm")  # type: ignore[assignment]
        if isinstance(llm, dict):
            if str(llm.get("keep_alive", "")).strip() == "30m":
                llm.pop("keep_alive")
            if str(llm.get("num_ctx", "")).strip() in ("6144", "8192"):
                llm.pop("num_ctx")
        au = data.get("audio")
        if isinstance(au, dict) and str(au.get("silence_ms", "")).strip() == "800":
            au.pop("silence_ms")                   # old default pinned by Settings -> 500 ms (faster commands)
        gui = data.get("gui")
        if isinstance(gui, dict) and str(gui.get("fps", "")).strip() == "30":
            gui.pop("fps")
        oc = data.get("ollama")
        if isinstance(oc, dict) and isinstance(oc.get("server_env"), dict):
            for k in ("OLLAMA_FLASH_ATTENTION", "OLLAMA_KV_CACHE_TYPE"):
                oc["server_env"].pop(k, None)      # garbage output on GTX 16xx (users' logs) -> safe defaults
        data["config_version"] = 8
        changed = True
    if ver < 9:                                   # 1.0.9: «Оперативка» was picked as a 1.0.7-crash workaround (cublas)
        llm = data.get("llm")  # type: ignore[assignment]
        if isinstance(llm, dict) and str(llm.get("placement", "")).strip().lower() == "cpu":
            llm["placement"] = "gpu"              # -> num_gpu=0 on every load, ~28 tok/s prompt (user's server.log)
            MIGRATION_NOTICES.append("ИИ снова работает на видеокарте (раньше в настройках стояло «Оперативка» — "
                                     "ответы были в 5–10 раз медленнее). Вернуть можно в Настройки → ИИ → "
                                     "«Где работает ИИ».")
            log.warning("Миграция 1.0.9: llm.placement cpu -> gpu")
        data["config_version"] = 9
        changed = True
    return data, changed


def apply_load_preset(cfg: dict) -> dict:
    """Fill the «Нагрузка» preset into keys the user did not set explicitly."""
    from .resources import LIGHT_MODEL, preset
    pr = preset(cfg)
    llm, stt, gui = cfg.setdefault("llm", {}), cfg.setdefault("stt", {}), cfg.setdefault("gui", {})
    if not llm.get("num_ctx"):
        llm["num_ctx"] = pr["num_ctx"]
    if not llm.get("keep_alive") and llm.get("keep_alive") != 0:
        llm["keep_alive"] = pr["keep_alive"]
    if llm.get("num_thread") in (None, ""):
        llm["num_thread"] = pr["threads"]
    if not llm.get("tools"):
        llm["tools"] = pr["tools"]
    if llm.get("light_model") or pr.get("model"):
        llm["model"] = pr.get("model") or LIGHT_MODEL          # only by the user's explicit choice
    if stt.get("cpu_threads") in (None, "", 0):
        stt["cpu_threads"] = pr["threads"]
    if not gui.get("fps"):
        gui["fps"] = pr["face_fps"]
    llm.setdefault("vram_headroom_gb", pr["vram_headroom_gb"])
    llm.setdefault("min_free_ram_gb", pr["min_free_ram_gb"])
    return cfg


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
    # 1.0.7: the name goes to Whisper as a HOTWORD. Until 1.0.6 «{name}, привет.» was prepended to initial_prompt and
    # Whisper echoed it on noise -> phantom «Джарвис, привет.» commands (seen in users' logs).
    if a.get("name") and not cfg["stt"].get("hotwords"):
        cfg["stt"]["hotwords"] = str(a["name"])
    apply_load_preset(cfg)
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
