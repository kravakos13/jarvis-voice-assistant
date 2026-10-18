"""Quiet, visible Hugging Face downloads (Whisper models).

* ``quiet()`` must run BEFORE ``huggingface_hub`` is imported: its constants are read at import time.
  It hides the Windows "cache-system uses symlinks" UserWarning, the server-sent "unauthenticated requests /
  set a HF_TOKEN" warning (an ``X-HF-Warning`` header re-emitted through the ``huggingface_hub`` logger),
  never sends a locally stored HF token and disables telemetry.
* faster-whisper's ``download_model`` passes a do-nothing tqdm class, so the console looked frozen for
  minutes. ``SizeMonitor`` polls the model folder in the HF cache and prints "N из M МБ" every couple of
  seconds (plus a heartbeat when the size is not growing).
"""
from __future__ import annotations

import logging
import os
import threading
import time
import warnings
from pathlib import Path
from typing import Callable, Optional

_ENV = {
    "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_EXPERIMENTAL_WARNING": "1",
    "HF_HUB_DISABLE_UPDATE_CHECK": "1",
}
WHISPER_REPOS = {
    "tiny": "Systran/faster-whisper-tiny", "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small", "medium": "Systran/faster-whisper-medium",
    "large-v2": "Systran/faster-whisper-large-v2", "large-v3": "Systran/faster-whisper-large-v3",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}
WHISPER_FILES = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json")


def quiet() -> None:
    for k, v in _ENV.items():
        os.environ.setdefault(k, v)
    warnings.filterwarnings("ignore", module=r"huggingface_hub(\..*)?")
    warnings.filterwarnings("ignore", message=r".*cache-system uses symlinks.*")
    warnings.filterwarnings("ignore", message=r".*unauthenticated requests.*")
    for name in ("huggingface_hub", "huggingface_hub.utils._http", "huggingface_hub.file_download"):
        logging.getLogger(name).setLevel(logging.ERROR)


def repo_for(size_or_id: str) -> str:
    return size_or_id if "/" in size_or_id else WHISPER_REPOS.get(size_or_id, f"Systran/faster-whisper-{size_or_id}")


def repo_folder(cache_dir: Path, repo_id: str) -> Path:
    return Path(cache_dir) / ("models--" + repo_id.replace("/", "--"))


def folder_size(p: Path) -> int:
    total = 0
    if not p.exists():
        return 0
    for f in p.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def expected_size(repo_id: str, session=None, timeout: float = 10) -> int:
    """Total bytes of the files faster-whisper needs (0 if unknown/offline)."""
    import requests
    s = session or requests
    try:
        r = s.get(f"https://huggingface.co/api/models/{repo_id}/tree/main", timeout=timeout)
        r.raise_for_status()
        total = 0
        for it in r.json():
            path = str(it.get("path", ""))
            if it.get("type") == "file" and (path in WHISPER_FILES or path.startswith("vocabulary.")):
                lfs = it.get("lfs") or {}
                total += int(lfs.get("size") or it.get("size") or 0)
        return total
    except Exception:
        return 0


def fmt_mb(n: int) -> str:
    return f"{n / (1 << 20):.0f}"


class SizeMonitor:
    """Background thread printing download progress of a folder."""

    def __init__(self, folder: Path, total: int = 0, label: str = "Whisper", interval: float = 2.0,
                 out: Optional[Callable[[str], None]] = None, clock: Callable[[], float] = time.monotonic):
        self.folder, self.total, self.label, self.interval = Path(folder), total, label, interval
        self.out = out or (lambda s: print("\r" + s.ljust(70), end="", flush=True))
        self.clock = clock
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None
        self.start_size = folder_size(self.folder)
        self.started = clock()
        self.lines: list[str] = []

    def line(self) -> str:
        size = folder_size(self.folder)
        el = int(self.clock() - self.started)
        if self.total > 0:
            pct = min(100, size * 100 // self.total)
            return f"  {self.label}: {pct}% ({fmt_mb(size)} из {fmt_mb(self.total)} МБ), {el} с"
        return f"  {self.label}: скачано {fmt_mb(size)} МБ, {el} с (идёт загрузка, не закрывай окно)"

    def tick(self) -> str:
        s = self.line()
        self.lines.append(s)
        self.out(s)
        return s

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.tick()

    def __enter__(self) -> "SizeMonitor":
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *a) -> None:
        self._stop.set()
        if self._t:
            self._t.join(timeout=self.interval + 1)
