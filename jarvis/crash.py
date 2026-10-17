"""Crash handler: unhandled exceptions (main + worker threads) and hard faults are written
to logs/crash_<time>.txt; the watchdog (jarvis.watchdog) decides whether to restart."""
from __future__ import annotations

import datetime as _dt
import faulthandler
import logging
import platform
import sys
import threading
import traceback
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)
_fault_file = None


def crash_dir() -> Path:
    from .config import resolve_path
    d = resolve_path("logs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_report(exc_type, exc, tb, where: str = "main") -> Path:
    path = crash_dir() / f"crash_{_dt.datetime.now():%Y-%m-%d_%H-%M-%S_%f}.txt"
    text = (f"Джарвис {__version__} — отчёт о сбое\n"
            f"Время: {_dt.datetime.now().isoformat(timespec='seconds')}\nПоток: {where}\n"
            f"Python {sys.version.split()[0]} / {platform.platform()}\n\n"
            + "".join(traceback.format_exception(exc_type, exc, tb)))
    path.write_text(text, encoding="utf-8")
    for old in sorted(crash_dir().glob("crash_*.txt"))[:-20]:     # keep the last 20 reports
        try:
            old.unlink()
        except OSError:
            pass
    return path


def install() -> None:
    global _fault_file

    def hook(t, e, tb):
        if issubclass(t, KeyboardInterrupt):
            sys.__excepthook__(t, e, tb)
            return
        p = write_report(t, e, tb)
        log.critical("Необработанная ошибка, отчёт: %s", p, exc_info=(t, e, tb))

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = getattr(args.thread, "name", "?")
        p = write_report(args.exc_type, args.exc_value, args.exc_traceback, name)
        # a dying worker thread is logged; the app keeps running (modules are isolated)
        log.error("Ошибка в потоке %s, отчёт: %s", name, p,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook
    try:   # native crashes (Qt, CUDA, PortAudio) -> logs/fault.log
        _fault_file = open(crash_dir() / "fault.log", "a", encoding="utf-8")
        faulthandler.enable(_fault_file)
    except Exception:
        pass
