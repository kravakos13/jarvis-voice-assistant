"""Entry point: python -m jarvis [--no-gui | --text | --wav FILE | --say TEXT | --check | --list-devices]"""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

from . import __version__
from .config import load_config, resolve_path

log = logging.getLogger("jarvis")


def setup_logging(cfg: dict, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    if sys.stderr is not None:          # pythonw.exe has no console
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
        root.addHandler(console)
    if cfg.get("file"):
        path = resolve_path(cfg["file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
    for noisy in ("urllib3", "faster_whisper", "httpx", "httpcore", "huggingface_hub", "filelock", "comtypes",
                  "telethon", "primp", "trafilatura", "PIL", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _fix_streams() -> None:
    """utf-8 console on Windows; under pythonw stdout/stderr are None -> make print() harmless."""
    import os
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jarvis", description="Джарвис - офлайн голосовой ассистент")
    p.add_argument("-c", "--config", help="путь к config.yaml")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--text", action="store_true", help="текстовый режим в консоли")
    g.add_argument("--wav", metavar="FILE", help="распознать WAV-файл как команду и ответить")
    g.add_argument("--say", metavar="TEXT", help="озвучить текст через Piper в WAV (--out)")
    g.add_argument("--list-devices", action="store_true", help="показать аудиоустройства")
    g.add_argument("--check", action="store_true", help="проверить Ollama, голос, Whisper и модули")
    p.add_argument("--no-gui", action="store_true", help="без окна: голосовой режим в консоли")
    p.add_argument("--ptt", action="store_true", help="push-to-talk вместо имени-активатора")
    p.add_argument("--minimized", action="store_true", help="запуститься свернутым в трей")
    p.add_argument("--demo", action="store_true", help="GUI без микрофона/моделей (демо анимации)")
    p.add_argument("--out", default="jarvis_say.wav", help="файл для --say")
    p.add_argument("--no-tts", action="store_true", help="не озвучивать ответы")
    p.add_argument("--model", help="переопределить модель Ollama")
    p.add_argument("--whisper", help="переопределить модель Whisper (tiny/base/small/medium/large-v3)")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    p.add_argument("--version", action="version", version=f"jarvis {__version__}")
    return p


def run_check(cfg: dict) -> int:
    from .llm import OllamaChat
    ok_all = True
    llm = OllamaChat(cfg["llm"])
    ok, msg = llm.check()
    print(f"[{'OK' if ok else '!!'}] Ollama / модель {cfg['llm']['model']}: {msg}")
    ok_all &= ok
    try:
        from .tts import PiperTTS
        PiperTTS(cfg["tts"])
        print(f"[OK] Голос Piper {cfg['tts']['piper_voice']}")
    except Exception as e:
        ok_all = False
        print(f"[!!] Piper: {e}")
    try:
        from .audio import list_devices
        list_devices()
        print("[OK] Аудиосистема (sounddevice/PortAudio)")
    except Exception as e:
        ok_all = False
        print(f"[!!] Аудио: {e}")
    try:
        from .router import Router
        from .skills.base import SkillContext
        from .config import PROJECT_ROOT
        r = Router(SkillContext(cfg, llm, say=lambda t: None, notify=lambda a, b: None, project_root=PROJECT_ROOT))
        print(f"[OK] Модули: {', '.join(s.name for s in r.skills)}")
        for k, v in r.failed.items():
            print(f"[!!] Модуль {k}: {v}")
    except Exception as e:
        print(f"[!!] Модули: {e}")
    try:
        from .stt import SpeechToText
        stt = SpeechToText(cfg["stt"])
        print(f"[OK] Whisper {cfg['stt']['model']} ({stt.device}/{stt.compute_type})")
    except Exception as e:
        ok_all = False
        print(f"[!!] Whisper: {e}")
    return 0 if ok_all else 1


def main(argv=None) -> int:
    _fix_streams()
    args = build_parser().parse_args(argv)
    try:   # a restore staged by the Settings window is applied before anything opens the databases
        from .backup import apply_pending_restore
        apply_pending_restore()
    except Exception as e:
        print(f"Восстановление из бэкапа не удалось: {e}")
    cfg = load_config(args.config)
    if args.model:
        cfg["llm"]["model"] = args.model
    if args.whisper:
        cfg["stt"]["model"] = args.whisper
    if args.ptt:
        cfg["assistant"]["mode"] = "ptt"
    setup_logging(cfg["logging"], args.verbose)
    try:
        from .crash import install
        install()
    except Exception as e:
        logging.getLogger(__name__).warning("crash handler: %s", e)

    if args.list_devices:
        from .audio import list_devices
        print(list_devices())
        return 0
    if args.check:
        return run_check(cfg)
    if args.say:
        from .tts import synth_to_wav
        print(f"Сохранено: {synth_to_wav(cfg['tts'], args.say, args.out)}")
        return 0

    use_gui = not (args.no_gui or args.text or args.wav) and cfg["gui"].get("enabled", True)
    if use_gui:
        try:
            from .gui.app import run_gui
        except Exception as e:
            log.warning("GUI недоступен (%s) - запускаю консольный режим", e)
        else:
            return run_gui(cfg, minimized=args.minimized or cfg["gui"].get("start_minimized", False),
                           demo=args.demo, speak=not args.no_tts)

    from .assistant import Assistant
    speak = not args.no_tts and (not args.text or cfg["tts"].get("speak_in_text_mode", True))
    assistant = None
    try:
        assistant = Assistant(cfg, speak=speak, need_stt=not args.text)
        if args.text:
            assistant.run_text()
        elif args.wav:
            assistant.run_wav(args.wav)
        else:
            assistant.run_voice()
    except KeyboardInterrupt:
        print("\nПока! (Ctrl+C)")
    finally:
        if assistant:
            assistant.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
