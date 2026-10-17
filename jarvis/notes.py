"""Dictated text notes saved as timestamped .txt files."""
from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import Optional

from . import platform_utils as pu

log = logging.getLogger(__name__)


class Notes:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.dir = pu.data_dir("Notes", self.cfg.get("dir", ""))
        self.open_after_save = bool(self.cfg.get("open_in_notepad", False))
        self._dictation: Optional[list[str]] = None

    # ------------------------------------------------------------ files
    def save(self, text: str, now: Optional[_dt.datetime] = None) -> Path:
        now = now or _dt.datetime.now()
        text = text.strip()
        first = pu.slugify(" ".join(text.split()[:5]), 30)
        path = self.dir / f"{now:%Y-%m-%d_%H-%M-%S}_{first}.txt"
        n = 1
        while path.exists():
            path = path.with_name(f"{path.stem}_{n}.txt")
            n += 1
        path.write_text(f"{now:%d.%m.%Y %H:%M}\n\n{text}\n", encoding="utf-8")
        log.info("Заметка сохранена: %s", path)
        if self.open_after_save:
            pu.open_in_notepad(path)
        return path

    def latest(self, count: int = 3) -> list[tuple[Path, str]]:
        files = sorted(self.dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, count)]
        out = []
        for f in files:
            body = f.read_text(encoding="utf-8", errors="replace").split("\n", 2)
            out.append((f, (body[2] if len(body) > 2 else body[-1]).strip()))
        return out

    def read_latest_reply(self, count: int = 3) -> str:
        items = self.latest(count)
        if not items:
            return "Заметок пока нет."
        parts = [f"Заметка {i}: {text}" for i, (_, text) in enumerate(items, 1)]
        return " ".join(parts)

    # ------------------------------------------------------------ dictation mode
    @property
    def dictating(self) -> bool:
        return self._dictation is not None

    def start_dictation(self) -> None:
        self._dictation = []

    def add_dictation(self, text: str) -> None:
        if self._dictation is not None and text.strip():
            self._dictation.append(text.strip())

    def finish_dictation(self) -> Optional[Path]:
        parts, self._dictation = self._dictation or [], None
        text = " ".join(parts).strip()
        return self.save(text) if text else None

    def cancel_dictation(self) -> None:
        self._dictation = None
