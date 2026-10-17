"""Backups of user data: notes, tasks DB, memory DB, settings (+ aliases/devices).

Restore is staged (written to <data>/_restore) and applied on the next start, before
any SQLite database is opened — Windows can't overwrite open DB files safely."""
from __future__ import annotations

import datetime as _dt
import logging
import shutil
import sqlite3
import threading
import zipfile
from pathlib import Path

from . import platform_utils as pu
from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

PROJECT_FILES = ["settings.local.yaml", "aliases.yaml", "devices.yaml"]
DATA_SKIP = {"Backups", "_restore", "cache", "Code", "Models", "Screenshots"}


def backup_dir(cfg: dict) -> Path:
    return pu.data_dir("Backups", (cfg.get("backup") or {}).get("dir", ""))


def _data_root() -> Path:
    return pu.data_dir("")


def make_backup(cfg: dict, reason: str = "manual") -> Path:
    root = _data_root()
    dest_dir = backup_dir(cfg)
    stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dest = dest_dir / f"jarvis-backup_{stamp}_{reason}.zip"
    tmp = dest.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name in PROJECT_FILES:
            p = PROJECT_ROOT / name
            if p.exists():
                z.write(p, f"project/{name}")
        for p in root.rglob("*"):
            rel = p.relative_to(root)
            if not p.is_file() or rel.parts[0] in DATA_SKIP or p.suffix in (".part", ".session-journal"):
                continue
            if rel.parts[0].startswith("telegram") and not (cfg.get("backup") or {}).get("include_telegram", False):
                continue    # the Telegram session is a login key — not backed up unless asked
            if p.suffix == ".sqlite3":
                # consistent copy of a live DB via the sqlite backup API
                snap = dest_dir / (p.name + ".snap")
                src = sqlite3.connect(str(p))
                dst = sqlite3.connect(str(snap))
                with dst:
                    src.backup(dst)
                src.close()
                dst.close()
                z.write(snap, f"data/{rel.as_posix()}")
                snap.unlink(missing_ok=True)
            else:
                z.write(p, f"data/{rel.as_posix()}")
    tmp.replace(dest)
    prune(cfg)
    log.info("Бэкап: %s", dest)
    return dest


def list_backups(cfg: dict) -> list[Path]:
    return sorted(backup_dir(cfg).glob("jarvis-backup_*.zip"), reverse=True)


def prune(cfg: dict) -> None:
    keep = int((cfg.get("backup") or {}).get("keep", 10))
    for old in list_backups(cfg)[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def stage_restore(zip_path: Path) -> None:
    """Validate the archive and stage it for the next start."""
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if not any(n.startswith(("data/", "project/")) for n in names):
            raise ValueError("Это не бэкап Джарвиса")
        for n in names:
            if n.startswith("/") or ".." in Path(n).parts:
                raise ValueError("Небезопасный путь в архиве")
        stage = _data_root() / "_restore"
        shutil.rmtree(stage, ignore_errors=True)
        z.extractall(stage)


def apply_pending_restore() -> bool:
    """Call at startup before opening databases."""
    stage = _data_root() / "_restore"
    if not stage.exists():
        return False
    try:
        root = _data_root()
        for p in (stage / "data").rglob("*") if (stage / "data").exists() else []:
            if p.is_file():
                dest = root / p.relative_to(stage / "data")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
        for p in (stage / "project").glob("*") if (stage / "project").exists() else []:
            if p.name in PROJECT_FILES:
                shutil.copy2(p, PROJECT_ROOT / p.name)
        log.info("Восстановление из бэкапа применено")
        return True
    finally:
        shutil.rmtree(stage, ignore_errors=True)


class AutoBackup:
    """Daily backup in a background thread (checks hourly)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._stop = threading.Event()

    def start(self) -> "AutoBackup":
        if (self.cfg.get("backup") or {}).get("enabled", True):
            threading.Thread(target=self._loop, name="jarvis-backup", daemon=True).start()
        return self

    def _due(self) -> bool:
        b = list_backups(self.cfg)
        if not b:
            return True
        age = _dt.datetime.now().timestamp() - b[0].stat().st_mtime
        return age > 24 * 3600 * float((self.cfg.get("backup") or {}).get("every_days", 1))

    def _loop(self):
        self._stop.wait(120)          # don't slow down startup
        while not self._stop.is_set():
            try:
                if self._due():
                    make_backup(self.cfg, "auto")
            except Exception as e:
                log.warning("Автобэкап не удался: %s", e)
            self._stop.wait(3600)

    def stop(self):
        self._stop.set()
