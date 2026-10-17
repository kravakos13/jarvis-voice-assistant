"""Multi-PC sync through a shared folder (Google Drive / OneDrive / Dropbox / network share / USB). No server.

Layout of the shared folder:
    <sync_dir>/Jarvis-Sync/
        notes/                       shared note files (two-way by content hash + mtime)
        notes_manifest.json          {name: {hash, mtime, device}} written by the last device that pushed
        devices/<device_id>/
            records.json             this device's full export: tasks + memory rows, tombstones
            settings.json            shareable settings + updated_at
            device.json              {id, name, last_sync}
Each device writes ONLY its own devices/<id>/ files, so cloud clients never see two writers
on the same file; SQLite files are never put in the cloud folder.

Merge rules (no silent data loss):
  * records: by uuid, last-writer-wins on updated_at; 'reminded' is OR-merged (a fired reminder
    doesn't fire again on the other PC); a tombstone deletes only rows not edited after the deletion.
  * notes: same content -> nothing; one side unchanged since the last sync -> take the other;
    both changed -> keep both: the incoming copy is saved as '<name> (конфликт <ПК>).txt'.
    Deleting a note is not propagated (safer: a deleted note may come back, never lost).
  * settings: only a shareable whitelist; newest device wins. Per-machine settings and secrets
    (audio devices, mic, CPU/GPU, paths, hotkeys, Telegram session, tokens, keys) are never synced.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import platform
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from . import platform_utils as pu
from .config import LOCAL_SETTINGS, PROJECT_ROOT, _read_yaml

log = logging.getLogger(__name__)

TABLES = {  # table -> synced columns (besides uuid, updated_at)
    "tasks": ["text", "due", "done", "reminded", "created"],
    "memory": ["kind", "text", "created"],
}
SHAREABLE = [
    "assistant.name", "assistant.user_name", "assistant.extra_wake_words", "assistant.greeting",
    "assistant.greeting_time_of_day", "assistant.wake_reply", "assistant.greet_on_start",
    "llm.system_prompt", "llm.answer_length", "llm.temperature",
    "tts.length_scale", "tts.pitch_semitones", "tts.fx",
    "gui.color", "gui.animations", "gui.subtitles",
    "profiles.default", "profiles.auto_game_mode", "profiles.game_reminders_voice", "profiles.game_notifications",
    "profiles.night_reminders_voice", "profiles.night_notifications", "profiles.night_schedule", "profiles.night_volume",
    "monitor.gpu_temp_warn", "monitor.cpu_temp_warn", "weather", "alerts.oblast", "alerts.raions",
]
SHARED_FILES = ["aliases.yaml"]          # game/app aliases
NEVER = ("api_hash", "token", "key", "session", "password")


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _get(d: dict, path: str):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _put(d: dict, path: str, v) -> None:
    ks = path.split(".")
    for k in ks[:-1]:
        d = d.setdefault(k, {})
    d[ks[-1]] = v


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def suggest_folders() -> list[Path]:
    """Common cloud / removable folders that exist on this PC."""
    home = Path.home()
    cands = [os.environ.get("OneDrive", ""), os.environ.get("OneDriveConsumer", ""), home / "OneDrive",
             home / "Google Drive", home / "Google Диск", home / "My Drive", home / "Dropbox", home / "Yandex.Disk",
             home / "YandexDisk", home / "iCloudDrive"]
    if pu.IS_WIN:
        for letter in "DEFGHIJ":
            cands += [f"{letter}:/My Drive", f"{letter}:/Мой диск", f"{letter}:/Google Drive"]
    out: list[Path] = []
    for c in cands:
        if c and Path(str(c)).is_dir() and Path(str(c)) not in out:
            out.append(Path(str(c)))
    return out


@dataclass
class SyncResult:
    ok: bool
    message: str
    stats: dict = field(default_factory=dict)
    conflicts: list = field(default_factory=list)


class SyncEngine:
    def __init__(self, cfg: dict, data_root: Optional[Path] = None, project_root: Optional[Path] = None,
                 local_settings: Optional[Path] = None):
        self.cfg = cfg
        self.scfg = cfg.get("sync", {}) or {}
        self.data = Path(data_root) if data_root else pu.data_dir("")
        self.project = Path(project_root) if project_root else PROJECT_ROOT
        self.local_settings = Path(local_settings) if local_settings else LOCAL_SETTINGS
        self._lock = threading.Lock()
        self.device = self._device()

    # ------------------------------------------------------------ identity / state
    def _device(self) -> dict:
        f = self.data / "device.json"
        d = _read_json(f, None)
        if not d or not d.get("id"):
            d = {"id": uuid.uuid4().hex[:12], "name": platform.node() or "ПК"}
            _write_json(f, d)
        if self.scfg.get("device_name"):
            d["name"] = self.scfg["device_name"]
        return d

    @property
    def state_file(self) -> Path:
        return self.data / "sync_state.json"

    def state(self) -> dict:
        return _read_json(self.state_file, {})

    def root(self) -> Optional[Path]:
        d = str(self.scfg.get("folder", "") or "").strip()
        return Path(os.path.expandvars(os.path.expanduser(d))) / "Jarvis-Sync" if d else None

    # ------------------------------------------------------------ main entry
    def sync(self, make_backup: bool = True) -> SyncResult:
        root = self.root()
        if root is None:
            return SyncResult(False, "Папка синхронизации не выбрана (Настройки → Бэкап и синхронизация).")
        if not root.parent.exists():
            return SyncResult(False, f"Папка синхронизации недоступна: {root.parent}")
        if not self._lock.acquire(blocking=False):
            return SyncResult(False, "Синхронизация уже идёт.")
        try:
            if make_backup:
                try:
                    from .backup import make_backup as mb
                    mb(self.cfg, "pre-sync")
                except Exception as e:
                    return SyncResult(False, f"Не удалось сделать бэкап перед синхронизацией: {e}")
            root.mkdir(parents=True, exist_ok=True)
            mine = root / "devices" / self.device["id"]
            others = [p for p in (root / "devices").glob("*") if p.is_dir() and p.name != self.device["id"]]
            st = self.state()
            stats: dict = {"devices": len(others)}
            conflicts: list = []
            stats.update(self._merge_records(others))
            n, c = self._merge_notes(root, st)
            stats["notes"] = n
            conflicts += c
            stats["settings"] = self._merge_settings(others, st)
            self._export(mine)
            st["last_sync"] = utcnow()
            st["last_result"] = "ok"
            _write_json(self.state_file, st)
            _write_json(mine / "device.json", {**self.device, "last_sync": st["last_sync"]})
            msg = (f"Синхронизировано с {len(others)} ПК: задачи/память +{stats.get('imported', 0)}"
                   f"/обновлено {stats.get('updated', 0)}/удалено {stats.get('deleted', 0)}, заметок {n}"
                   + (f", конфликтов {len(conflicts)}" if conflicts else "") + ".")
            log.info(msg)
            return SyncResult(True, msg, stats, conflicts)
        except Exception as e:
            log.exception("sync: %s", e)
            st = self.state()
            st["last_result"] = f"ошибка: {e}"
            _write_json(self.state_file, st)
            return SyncResult(False, f"Синхронизация не удалась: {e}")
        finally:
            self._lock.release()

    # ------------------------------------------------------------ records
    def _db(self, table: str) -> Optional[sqlite3.Connection]:
        name = {"tasks": "planner.sqlite3", "memory": "memory.sqlite3"}[table]
        sub = str({"tasks": self.cfg.get("planner", {}).get("dir", ""),
                   "memory": self.cfg.get("memory", {}).get("dir", "")}[table] or "")
        path = Path(os.path.expandvars(sub)).expanduser() / name if sub else self.data / name
        if not path.exists():
            return None
        conn = sqlite3.connect(str(path), timeout=10)
        from .syncdb import ensure_sync_schema
        ensure_sync_schema(conn, table)
        return conn

    def _merge_records(self, others: list[Path]) -> dict:
        imported = updated = deleted = 0
        for table, cols in TABLES.items():
            conn = self._db(table)
            if conn is None:
                continue
            try:
                with conn:
                    local_tomb = dict(conn.execute("SELECT uuid, deleted_at FROM tombstones WHERE tbl=?", (table,)))
                    for dev in others:
                        data = _read_json(dev / "records.json", {})
                        for rec in data.get(table, []):
                            u, ts = rec.get("uuid"), rec.get("updated_at", "")
                            if not u:
                                continue
                            if u in local_tomb and local_tomb[u] >= ts:
                                continue          # deleted here after the remote edit
                            row = conn.execute(f"SELECT updated_at, {', '.join(cols)} FROM {table} WHERE uuid=?", (u,)).fetchone()
                            vals = [rec.get(c) for c in cols]
                            if row is None:
                                conn.execute(f"INSERT INTO {table}(uuid, updated_at, {', '.join(cols)}) VALUES(?,?{',?' * len(cols)})",
                                             [u, ts] + vals)
                                conn.execute("DELETE FROM tombstones WHERE tbl=? AND uuid=?", (table, u))
                                local_tomb.pop(u, None)
                                imported += 1
                            elif ts > (row[0] or ""):
                                if "reminded" in cols:   # OR-merge: never re-fire a reminder
                                    i = cols.index("reminded")
                                    vals[i] = int(bool(vals[i]) or bool(row[1 + i]))
                                sets = ", ".join(f"{c}=?" for c in cols)
                                extra = ", embedding=NULL" if table == "memory" else ""
                                conn.execute(f"UPDATE {table} SET {sets}, updated_at=?{extra} WHERE uuid=?", vals + [ts, u])
                                updated += 1
                            elif "reminded" in cols and rec.get("reminded") and not row[1 + cols.index("reminded")]:
                                conn.execute(f"UPDATE {table} SET reminded=1, updated_at=? WHERE uuid=?", (row[0], u))
                        for t in data.get("tombstones", []):
                            if t.get("tbl") != table:
                                continue
                            row = conn.execute(f"SELECT updated_at FROM {table} WHERE uuid=?", (t["uuid"],)).fetchone()
                            if row is not None and (row[0] or "") <= t["deleted_at"]:
                                conn.execute(f"DELETE FROM {table} WHERE uuid=?", (t["uuid"],))
                                deleted += 1
                            conn.execute("INSERT OR REPLACE INTO tombstones(tbl, uuid, deleted_at) VALUES(?,?,?)",
                                         (table, t["uuid"], max(t["deleted_at"], local_tomb.get(t["uuid"], ""))))
            finally:
                conn.close()
        return {"imported": imported, "updated": updated, "deleted": deleted}

    def _export(self, mine: Path) -> None:
        out: dict = {"device": self.device, "exported_at": utcnow(), "tombstones": []}
        for table, cols in TABLES.items():
            conn = self._db(table)
            if conn is None:
                out[table] = []
                continue
            try:
                rows = conn.execute(f"SELECT uuid, updated_at, {', '.join(cols)} FROM {table}").fetchall()
                out[table] = [dict(zip(["uuid", "updated_at"] + cols, r)) for r in rows]
                out["tombstones"] += [{"tbl": table, "uuid": u, "deleted_at": d} for u, d in
                                      conn.execute("SELECT uuid, deleted_at FROM tombstones WHERE tbl=?", (table,))]
            finally:
                conn.close()
        _write_json(mine / "records.json", out)
        _write_json(mine / "settings.json", self._settings_export())

    # ------------------------------------------------------------ notes
    def _notes_dir(self) -> Path:
        d = self.cfg.get("notes", {}).get("dir", "")
        p = Path(os.path.expandvars(d)).expanduser() if d else self.data / "Notes"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _merge_notes(self, root: Path, st: dict) -> tuple[int, list]:
        local_dir, shared_dir = self._notes_dir(), root / "notes"
        shared_dir.mkdir(parents=True, exist_ok=True)
        manifest = _read_json(root / "notes_manifest.json", {})
        last = st.get("notes", {})                      # name -> hash at our last sync
        changed, conflicts = 0, []
        names = {p.name for p in local_dir.glob("*.txt")} | {p.name for p in shared_dir.glob("*.txt")}
        for name in sorted(names):
            lp, sp = local_dir / name, shared_dir / name
            lh = _sha(lp.read_bytes()) if lp.exists() else None
            sh = _sha(sp.read_bytes()) if sp.exists() else None
            base = last.get(name)
            if lh == sh:
                pass
            elif sh is None:                              # new/only local -> push
                shutil.copy2(lp, sp)
                changed += 1
            elif lh is None:                              # only shared -> pull
                shutil.copy2(sp, lp)
                lh = sh
                changed += 1
            elif base == lh:                              # we didn't touch it -> take theirs
                shutil.copy2(sp, lp)
                lh = sh
                changed += 1
            elif base == sh:                              # they didn't touch it -> push ours
                shutil.copy2(lp, sp)
                changed += 1
            else:                                         # both changed -> keep both
                who = manifest.get(name, {}).get("device_name", "другой ПК")
                stem = Path(name).stem
                cp = local_dir / f"{stem} (конфликт {who}).txt"
                k = 2
                while cp.exists() and _sha(cp.read_bytes()) != sh:
                    cp = local_dir / f"{stem} (конфликт {who} {k}).txt"
                    k += 1
                shutil.copy2(sp, cp)
                shutil.copy2(lp, sp)                      # ours becomes the shared version
                conflicts.append(cp.name)
                changed += 1
            final = _sha(sp.read_bytes()) if sp.exists() else lh
            if manifest.get(name, {}).get("hash") != final:
                manifest[name] = {"hash": final, "device": self.device["id"], "device_name": self.device["name"],
                                  "at": utcnow()}
            last[name] = final
        for cname in conflicts:                           # push conflict copies right away
            src = local_dir / cname
            shutil.copy2(src, shared_dir / cname)
            h = _sha(src.read_bytes())
            manifest[cname] = {"hash": h, "device": self.device["id"], "device_name": self.device["name"], "at": utcnow()}
            last[cname] = h
        st["notes"] = last
        _write_json(root / "notes_manifest.json", manifest)
        return changed, conflicts

    # ------------------------------------------------------------ settings
    def _shareable_now(self) -> dict:
        local = _read_yaml(self.local_settings) if self.local_settings.exists() else {}
        vals = {}
        for k in SHAREABLE:
            v = _get(local, k)
            if v is not None and not any(n in k for n in NEVER):
                vals[k] = v
        files = {}
        for f in SHARED_FILES:
            p = self.project / f
            if p.exists():
                files[f] = p.read_text(encoding="utf-8")
        return {"values": vals, "files": files}

    def _settings_export(self) -> dict:
        st = self.state()
        return {"device": self.device["name"], "updated_at": st.get("settings_updated_at", ""), **self._shareable_now()}

    def _merge_settings(self, others: list[Path], st: dict) -> str:
        cur = self._shareable_now()
        h = _sha(json.dumps(cur, sort_keys=True, ensure_ascii=False).encode())
        if st.get("settings_hash") != h:                 # changed locally since last sync
            st["settings_hash"] = h
            st["settings_updated_at"] = utcnow()
        best = None
        for dev in others:
            s = _read_json(dev / "settings.json", None)
            if s and s.get("updated_at", "") > st.get("settings_updated_at", "") and (best is None or s["updated_at"] > best["updated_at"]):
                best = s
        if not best:
            _write_json(self.state_file, st)
            return "свои"
        changes: dict = {}
        for k, v in (best.get("values") or {}).items():
            if k in SHAREABLE and not any(n in k for n in NEVER):
                _put(changes, k, v)
        if changes:
            data = _read_yaml(self.local_settings) if self.local_settings.exists() else {}
            from .config import deep_merge
            merged = deep_merge(data, changes)
            self.local_settings.write_text(yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8")
        for f, text in (best.get("files") or {}).items():
            if f in SHARED_FILES and isinstance(text, str):
                yaml.safe_load(text)                     # validate before writing
                (self.project / f).write_text(text, encoding="utf-8")
        new = self._shareable_now()
        st["settings_hash"] = _sha(json.dumps(new, sort_keys=True, ensure_ascii=False).encode())
        st["settings_updated_at"] = best["updated_at"]
        st["settings_applied"] = True
        _write_json(self.state_file, st)
        return f"с {best.get('device', 'другого ПК')}"


class AutoSync:
    def __init__(self, engine: SyncEngine, on_result=None):
        self.engine, self.on_result = engine, on_result or (lambda r: None)
        self._stop = threading.Event()

    def start(self) -> "AutoSync":
        s = self.engine.scfg
        if s.get("auto_on_start"):
            threading.Thread(target=lambda: (self._stop.wait(30), self._run()), daemon=True).start()
        if float(s.get("interval_min", 0) or 0) > 0:
            threading.Thread(target=self._loop, name="jarvis-sync", daemon=True).start()
        return self

    def _run(self):
        try:
            self.on_result(self.engine.sync())
        except Exception as e:
            log.error("autosync: %s", e)

    def _loop(self):
        while not self._stop.wait(float(self.engine.scfg.get("interval_min", 30)) * 60):
            self._run()

    def on_exit(self):
        self._stop.set()
        if self.engine.scfg.get("auto_on_exit"):
            self._run()
