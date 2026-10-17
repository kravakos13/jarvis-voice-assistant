"""Watchdog launcher: `pythonw -m jarvis.watchdog [args]`.

Runs `python -m jarvis` as a child process and restarts it after a crash, at most
MAX_CRASHES times within WINDOW_S (no crash loops). Exit code 0 = normal quit,
75 = restart requested (e.g. after settings), anything else = crash.
Also guarantees a single running instance (autostart + double click)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

RESTART_CODE = 75
MAX_CRASHES = 3
WINDOW_S = 600
ROOT = Path(__file__).resolve().parents[1]


def _single_instance() -> Optional[object]:
    if sys.platform == "win32":
        import ctypes
        h = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\JarvisAssistantWatchdog")
        if ctypes.windll.kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            return None
        return h
    import fcntl
    f = open(Path(os.environ.get("TMPDIR", "/tmp")) / "jarvis-watchdog.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return f


def _notify(text: str) -> None:
    try:
        sys.path.insert(0, str(ROOT))
        from jarvis.platform_utils import notify
        notify("Джарвис", text)
    except Exception:
        pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    lock = _single_instance()
    if lock is None:
        return 0                                   # already running
    env = dict(os.environ, JARVIS_WATCHDOG="1")
    crashes: list[float] = []
    while True:
        started = time.time()
        code = subprocess.call([sys.executable, "-m", "jarvis"] + argv, cwd=str(ROOT), env=env)
        if code == 0:
            return 0
        if code == RESTART_CODE:
            argv = [a for a in argv if a != "--minimized"]
            continue
        now = time.time()
        crashes = [t for t in crashes if now - t < WINDOW_S] + [now]
        if len(crashes) >= MAX_CRASHES:
            _notify(f"Джарвис аварийно завершился {len(crashes)} раза за 10 минут и больше не перезапускается. "
                    f"Отчёты: {ROOT / 'logs'}")
            return code
        _notify("Джарвис перезапускается после сбоя...")
        time.sleep(min(30, 3 * len(crashes)) if now - started < 60 else 2)
        if "--minimized" not in argv:
            argv.append("--minimized")


if __name__ == "__main__":
    sys.exit(main())
