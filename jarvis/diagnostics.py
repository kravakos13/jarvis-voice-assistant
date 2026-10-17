"""Self-diagnostics: microphone, Whisper, Ollama + models, Piper, audio output, internet, alerts, Telegram.

run(cfg, assistant, quick) -> list[Check]. Quick mode (startup) does no recording/synthesis."""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import requests

from . import platform_utils as pu
from .config import resolve_path

log = logging.getLogger(__name__)


@dataclass
class Check:
    id: str
    title: str
    ok: Optional[bool]          # True ok, False problem, None warning/skipped
    detail: str
    fixes: list = field(default_factory=list)   # [(fix_id, label)]


def _mic(cfg, quick) -> Check:
    try:
        import sounddevice as sd
        from .audio import resolve_device
        dev = resolve_device(cfg["audio"].get("input_device"), "input")
        info = sd.query_devices(dev if dev is not None else sd.default.device[0], "input")
        name = info["name"]
        if quick:
            return Check("mic", "Микрофон", True, f"Найден: {name}")
        rec = sd.rec(int(16000 * 1.0), samplerate=16000, channels=1, dtype="float32", device=dev)
        sd.wait()
        level = float(np.sqrt(np.mean(rec ** 2)))
        if level < 1e-5:
            return Check("mic", "Микрофон", False, f"{name}: тишина (0). Проверь доступ к микрофону в Windows или выбери другое устройство.",
                         [("mic_privacy", "Доступ к микрофону"), ("sound_settings", "Настройки звука")])
        return Check("mic", "Микрофон", True, f"{name}: уровень {min(100, level * 800):.0f}%")
    except Exception as e:
        return Check("mic", "Микрофон", False, f"Микрофон недоступен: {e}",
                     [("mic_privacy", "Доступ к микрофону"), ("sound_settings", "Настройки звука")])


def _whisper(cfg, assistant, quick) -> Check:
    stt = getattr(assistant, "stt", None) if assistant else None
    if stt is not None and getattr(stt, "model", None) is not None:
        return Check("whisper", "Распознавание (Whisper)", True, f"Модель {cfg['stt'].get('model')} на {getattr(stt, 'device', '?')}")
    try:
        import faster_whisper  # noqa: F401
    except Exception as e:
        return Check("whisper", "Распознавание (Whisper)", False, f"faster-whisper не установлен: {e}")
    if quick:
        return Check("whisper", "Распознавание (Whisper)", None, "Ещё не загружена (текстовый режим или загрузка).")
    try:
        from .stt import SpeechToText
        s = SpeechToText(cfg["stt"])
        return Check("whisper", "Распознавание (Whisper)", True, f"Загружается на {s.device}")
    except Exception as e:
        return Check("whisper", "Распознавание (Whisper)", False, f"Не загрузилась: {e}")


def _ollama(cfg) -> list[Check]:
    host = cfg["llm"].get("host", "http://localhost:11434")
    try:
        tags = requests.get(f"{host}/api/tags", timeout=3).json()
    except Exception:
        fixes = [("start_ollama", "Запустить Ollama")] if (shutil.which("ollama") or pu.IS_WIN) else []
        return [Check("ollama", "Ollama (ИИ)", False, "Ollama не запущена или не установлена.",
                      fixes + [("ollama_site", "Скачать Ollama")])]
    have = {m.get("name", "") for m in tags.get("models", [])}
    have |= {n.split(":")[0] for n in have if n.endswith(":latest")}
    out = [Check("ollama", "Ollama (ИИ)", True, f"Работает, моделей: {len(tags.get('models', []))}")]
    wanted = [("Основная модель", cfg["llm"].get("model"), True)]
    if cfg.get("code", {}).get("enabled"):
        wanted.append(("Модель для кода", cfg["code"].get("model"), False))
    if cfg.get("memory", {}).get("enabled"):
        wanted.append(("Эмбеддинги памяти", cfg["memory"].get("embed_model", "nomic-embed-text"), False))
    for title, m, required in wanted:
        if not m:
            continue
        ok = m in have or f"{m}:latest" in have
        out.append(Check(f"model:{m}", title, True if ok else (False if required else None),
                         f"{m} {'есть' if ok else 'не скачана'}", [] if ok else [(f"pull:{m}", f"Скачать {m}")]))
    return out


def _piper(cfg, quick) -> Check:
    t = cfg["tts"]
    if t.get("engine", "piper") != "piper":
        return Check("tts", "Голос", None, f"Движок: {t.get('engine')}")
    voice = t.get("piper_voice", "ru_RU-denis-medium")
    path = resolve_path(t.get("voices_dir", "voices")) / f"{voice}.onnx"
    if not path.exists():
        return Check("tts", "Голос (Piper)", False, f"Голос {voice} не скачан.", [("download_voice", "Скачать голос")])
    if quick:
        return Check("tts", "Голос (Piper)", True, f"{voice}")
    try:
        from .tts import PiperTTS
        t0 = time.time()
        audio, _ = PiperTTS(t).synthesize("Проверка.")
        return Check("tts", "Голос (Piper)", bool(audio.size), f"{voice}: синтез {time.time() - t0:.1f} с")
    except Exception as e:
        return Check("tts", "Голос (Piper)", False, f"Ошибка синтеза: {e}")


def _output(cfg) -> Check:
    try:
        import sounddevice as sd
        from .audio import resolve_device
        dev = resolve_device(cfg["audio"].get("output_device"), "output")
        info = sd.query_devices(dev if dev is not None else sd.default.device[1], "output")
        return Check("output", "Вывод звука", True, info["name"])
    except Exception as e:
        return Check("output", "Вывод звука", False, f"Нет устройства вывода: {e}", [("sound_settings", "Настройки звука")])


def _internet() -> Check:
    for url in ("https://www.google.com/generate_204", "https://1.1.1.1", "https://ya.ru"):
        try:
            requests.head(url, timeout=4)
            return Check("internet", "Интернет", True, "Есть")
        except Exception:
            continue
    return Check("internet", "Интернет", None, "Нет связи — поиск, погода и тревоги не работают (остальное офлайн).")


def _alerts(cfg, assistant) -> Optional[Check]:
    a = cfg.get("alerts", {})
    if not a.get("enabled"):
        return None
    if a.get("backend") == "alerts_in_ua" and not a.get("alerts_in_ua_token"):
        return Check("alerts", "Воздушные тревоги", False, "Для alerts.in.ua нужен токен.", [("settings:integrations", "Указать токен")])
    sk = assistant.router.by_name.get("alerts") if assistant else None
    try:
        if sk:
            active, _, _ = sk.fetch()
        else:
            active = None
            requests.get("https://siren.pp.ua/api/v3/alerts", timeout=8).raise_for_status()
        return Check("alerts", "Воздушные тревоги", True, "Источник отвечает" + ("" if active is None else (" — сейчас ТРЕВОГА" if active else " — тревоги нет")))
    except Exception as e:
        return Check("alerts", "Воздушные тревоги", False, f"Источник недоступен: {e}")


def _telegram(cfg) -> Optional[Check]:
    t = cfg.get("telegram", {})
    if not t.get("enabled"):
        return None
    if not t.get("api_id") or not t.get("api_hash"):
        return Check("telegram", "Telegram", False, "Не указаны api_id / api_hash.", [("settings:integrations", "Настроить")])
    sess = pu.data_dir("", t.get("dir", "")) / "telegram.session"
    if not sess.exists():
        return Check("telegram", "Telegram", False, "Вход не выполнен.", [("telegram_login", "Войти в Telegram")])
    return Check("telegram", "Telegram", True, "Сессия есть")


MODULE_TITLES = {
    "audio_devices": "Переключение аудиоустройств", "telegram": "Telegram", "smarthome": "Умный дом",
    "screen": "Понимание экрана", "web_search": "Ответы из интернета", "media": "YouTube / YouTube Music",
    "memory": "Долговременная память", "planner": "Задачи и напоминания", "code": "Код для микроконтроллеров",
    "cad": "3D-модели", "monitor": "Температура ПК", "alerts": "Воздушные тревоги", "dictate": "Диктовка в окно",
    "translator": "Переводчик", "files": "Поиск файлов", "apps": "Игры и программы", "lumo": "Передать Люмо",
}
PKG_HINT = {"pycaw": "Windows-модуль pycaw (работает только в Windows)", "telethon": "пакет telethon",
            "paho": "пакет paho-mqtt", "mss": "пакет mss", "ddgs": "пакет ddgs", "trafilatura": "пакет trafilatura",
            "keyboard": "пакет keyboard", "pyperclip": "пакет pyperclip", "winotify": "пакет winotify",
            "cadquery": "пакет cadquery (requirements-cad.txt)"}


def explain_failure(err: str) -> str:
    import re
    m = re.search(r"No module named '([\w.]+)'", err)
    if m:
        pkg = m.group(1).split(".")[0]
        return f"не установлен {PKG_HINT.get(pkg, 'пакет ' + pkg)}. Переустанови Джарвиса (установщик докачает пакеты)."
    if "api_id" in err or "api_hash" in err:
        return "не настроены ключи (Настройки → Интеграции)."
    return f"ошибка загрузки: {err[:160]}"


def _modules(assistant) -> list[Check]:
    failed = getattr(getattr(assistant, "router", None), "failed", {}) or {}
    return [Check(f"module:{k}", f"Модуль «{MODULE_TITLES.get(k, k)}»", None if "pycaw" in v else False,
                  "Отключён: " + explain_failure(v)) for k, v in failed.items()]


def run(cfg: dict, assistant=None, quick: bool = False) -> list[Check]:
    checks: list[Check] = []
    for fn in (lambda: [_mic(cfg, quick)], lambda: [_whisper(cfg, assistant, quick)], lambda: _ollama(cfg),
               lambda: [_piper(cfg, quick)], lambda: [_output(cfg)], lambda: [_internet()],
               lambda: [_alerts(cfg, assistant)], lambda: [_telegram(cfg)], lambda: _modules(assistant)):
        try:
            checks += [c for c in fn() if c is not None]
        except Exception as e:      # a diagnostic must never crash
            log.exception("diag: %s", e)
    return checks


def spoken_summary(checks: list[Check]) -> str:
    bad = [c for c in checks if c.ok is False]
    warn = [c for c in checks if c.ok is None]
    if not bad and not warn:
        return "Все системы в норме, сэр."
    parts = []
    if bad:
        parts.append("Проблемы: " + "; ".join(f"{c.title} — {c.detail}" for c in bad[:4]))
    if warn:
        parts.append("Замечания: " + "; ".join(c.title for c in warn[:3]))
    return ". ".join(parts) + ". Подробности в окне."


def fix(fix_id: str, cfg: dict) -> str:
    """Run a fix-it action. Returns a human message."""
    if fix_id == "start_ollama":
        exe = shutil.which("ollama")
        app = Path(pu.os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe" if pu.IS_WIN else None
        if app and app.exists():
            pu.spawn([str(app)])
        elif exe:
            pu.spawn([exe, "serve"])
        else:
            return "Ollama не установлена."
        return "Запускаю Ollama..."
    if fix_id.startswith("pull:"):
        from .updater import pull_models
        m = fix_id[5:]
        sub = {"llm": dict(cfg["llm"], model=m), "code": {}, "memory": {"embed_model": None}}
        errs = pull_models(sub)
        return f"Ошибка: {errs[0]}" if errs else f"Модель {m} скачана."
    if fix_id == "download_voice":
        from .tts import ensure_voice
        v = cfg["tts"].get("piper_voice", "ru_RU-denis-medium")
        ensure_voice(v, resolve_path(cfg["tts"].get("voices_dir", "voices")))
        return f"Голос {v} скачан. Перезапусти Джарвиса."
    if fix_id == "mic_privacy":
        pu.open_path("ms-settings:privacy-microphone") if pu.IS_WIN else None
        return "Открываю настройки доступа к микрофону."
    if fix_id == "sound_settings":
        pu.open_path("ms-settings:sound") if pu.IS_WIN else None
        return "Открываю настройки звука."
    if fix_id == "ollama_site":
        import webbrowser
        webbrowser.open("https://ollama.com/download")
        return "Открываю страницу загрузки Ollama."
    if fix_id == "telegram_login":
        from .config import PROJECT_ROOT
        import sys
        script = PROJECT_ROOT / "scripts" / "telegram_login.py"
        py = sys.executable.replace("pythonw.exe", "python.exe")
        if pu.IS_WIN:
            subprocess.Popen([py, str(script)], creationflags=0x00000010)   # CREATE_NEW_CONSOLE
        else:
            subprocess.Popen([py, str(script)])
        return "Открыл окно входа в Telegram."
    return "Неизвестное действие."
