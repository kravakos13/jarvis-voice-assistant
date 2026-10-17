"""Index of launchable games/apps: Steam, Epic Games, Start Menu & Desktop shortcuts,
plus the config whitelist. Fuzzy matching of spoken (Russian) names."""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import platform_utils as pu
from .wake import normalize, translit

log = logging.getLogger(__name__)


@dataclass
class Entry:
    name: str
    target: str          # URI, file path or executable name
    kind: str            # steam | epic | shortcut | app
    key: str = ""

    @property
    def is_game(self) -> bool:
        return self.kind in ("steam", "epic")


_NUM = {"один": "1", "два": "2", "три": "3", "четыре": "4", "пять": "5", "шесть": "6", "семь": "7",
        "восемь": "8", "девять": "9", "десять": "10"}


def phonetic(text: str) -> str:
    t = normalize(text)
    t = " ".join(_NUM.get(w, w) for w in t.split())
    t = translit(t)
    for a, b in (("dzh", "j"), ("zh", "j"), ("ph", "f"), ("ck", "k"), ("ch", "c"), ("sh", "s"), ("th", "t"),
                 ("kh", "h"), ("ts", "c"), ("q", "k"), ("x", "ks"), ("w", "v"), ("y", "i"), ("ee", "i"),
                 ("oo", "u"), ("u", "a"), ("c", "k"), ("e", "i"), ("o", "a")):
        t = t.replace(a, b)
    t = re.sub(r"(.)\1+", r"\1", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def score(query_key: str, entry_key: str) -> float:
    if not query_key or not entry_key:
        return 0.0
    if query_key == entry_key:
        return 1.0
    best = difflib.SequenceMatcher(None, query_key, entry_key).ratio()
    ew = entry_key.split()
    qn = len(query_key.split())
    for i in range(len(ew)):                       # compare against windows of words
        for n in (qn, qn + 1):
            win = " ".join(ew[i:i + n])
            if win:
                best = max(best, difflib.SequenceMatcher(None, query_key, win).ratio() * (0.95 if n != len(ew) else 1))
    if entry_key.startswith(query_key) and len(query_key) >= 4:
        best = max(best, 0.9)
    return best


_SKIP = re.compile(r"uninstall|удал|readme|help|справк|website|веб-сайт|documentation|license|лиценз|release notes|"
                   r"redistributable|proton|steam linux runtime|steamworks|support|manual|руководств|changelog|"
                   r"debug|crash ?report|setup|install", re.I)


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def steam_root() -> Optional[Path]:
    if pu.IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:  # type: ignore[attr-defined]
                return Path(winreg.QueryValueEx(k, "SteamPath")[0])  # type: ignore[attr-defined]
        except Exception:
            pass
        for p in (Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Steam",):
            if p.exists():
                return p
    for p in (Path.home() / ".steam" / "steam", Path.home() / ".local" / "share" / "Steam"):
        if p.exists():
            return p
    return None


def steam_games(root: Optional[Path]) -> list[Entry]:
    if not root:
        return []
    libs = {root}
    vdf = _read_text(root / "steamapps" / "libraryfolders.vdf")
    for m in re.finditer(r'"path"\s+"([^"]+)"', vdf):
        libs.add(Path(m.group(1).replace("\\\\", "\\")))
    out = []
    for lib in libs:
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            txt = _read_text(acf)
            appid = re.search(r'"appid"\s+"(\d+)"', txt)
            name = re.search(r'"name"\s+"([^"]+)"', txt)
            if appid and name and not _SKIP.search(name.group(1)) and appid.group(1) != "228980":
                out.append(Entry(name.group(1), f"steam://rungameid/{appid.group(1)}", "steam"))
    return out


def epic_games(manifests: Optional[Path] = None) -> list[Entry]:
    base = manifests or Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    out = []
    for item in base.glob("*.item") if base.exists() else []:
        try:
            d = json.loads(_read_text(item))
        except ValueError:
            continue
        if d.get("bIsIncompleteInstall") or not d.get("AppName"):
            continue
        name = d.get("DisplayName") or d["AppName"]
        if d.get("CatalogNamespace") and d.get("CatalogItemId"):
            ref = f"{d['CatalogNamespace']}%3A{d['CatalogItemId']}%3A{d['AppName']}"
        else:
            ref = d["AppName"]
        out.append(Entry(name, f"com.epicgames.launcher://apps/{ref}?action=launch&silent=true", "epic"))
    return out


def shortcut_dirs() -> list[Path]:
    dirs = []
    if pu.IS_WIN:
        for env, sub in (("APPDATA", r"Microsoft\Windows\Start Menu\Programs"),
                         ("PROGRAMDATA", r"Microsoft\Windows\Start Menu\Programs")):
            if os.environ.get(env):
                dirs.append(Path(os.environ[env]) / sub)
        dirs.append(Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop")
    dirs.append(Path.home() / "Desktop")
    dirs.append(Path.home() / "OneDrive" / "Desktop")
    return [d for d in dirs if d.exists()]


def shortcuts(dirs: Optional[list[Path]] = None) -> list[Entry]:
    out = []
    for d in dirs if dirs is not None else shortcut_dirs():
        for pattern in ("*.lnk", "*.url"):
            for p in d.rglob(pattern):
                name = p.stem
                if not _SKIP.search(name):
                    kind = "shortcut"
                    if p.suffix == ".url" and "steam://" in _read_text(p):
                        kind = "steam"
                    out.append(Entry(name, str(p), kind))
    return out


class Launcher:
    def __init__(self, cfg: dict, aliases_path: Optional[Path] = None):
        self.cfg = cfg or {}
        self.aliases_path = aliases_path
        self.entries: list[Entry] = []
        self.aliases: dict[str, str] = {}
        self._lock = threading.Lock()
        self.min_score = float(self.cfg.get("min_score", 0.72))

    def load_aliases(self) -> None:
        self.aliases = {}
        if self.aliases_path and self.aliases_path.exists():
            try:
                import yaml
                data = yaml.safe_load(self.aliases_path.read_text(encoding="utf-8")) or {}
                self.aliases = {normalize(k): str(v) for k, v in data.items() if v}
            except Exception as e:
                log.error("aliases.yaml: %s", e)

    def build(self) -> int:
        entries: list[Entry] = []
        for name, exe in (self.cfg.get("apps") or {}).items():
            entries.append(Entry(name, exe, "app"))
        for loader in (lambda: steam_games(steam_root()), epic_games, shortcuts):
            try:
                entries += loader()
            except Exception as e:
                log.warning("Индекс приложений: %s", e)
        seen: set[str] = set()
        uniq: list[Entry] = []
        for ent in entries:          # games first, then dedupe by name
            k = normalize(ent.name)
            if k in seen:
                continue
            seen.add(k)
            ent.key = phonetic(ent.name)
            uniq.append(ent)
        self.load_aliases()
        with self._lock:
            self.entries = uniq
        log.info("Индекс приложений: %d (игр: %d)", len(uniq), sum(e.is_game for e in uniq))
        return len(uniq)

    def games(self) -> list[Entry]:
        return [e for e in self.entries if e.is_game]

    def find(self, spoken: str) -> tuple[Optional[Entry], float]:
        q = normalize(spoken)
        q = re.sub(r"^(игру|игра|приложение|программу|прогу)\s+", "", q)
        target = self.aliases.get(q)
        if target:
            for e in self.entries:
                if normalize(e.name) == normalize(target):
                    return e, 1.0
            q = normalize(target)
        qk = phonetic(q)
        best, best_s = None, 0.0
        with self._lock:
            for e in self.entries:
                sc = score(qk, e.key)
                if e.is_game:
                    sc += 0.02
                if sc > best_s:
                    best, best_s = e, sc
        return (best, best_s) if best_s >= self.min_score else (None, best_s)

    @staticmethod
    def launch(e: Entry) -> None:
        if e.kind == "app":
            pu.spawn([e.target])
        elif pu.IS_WIN:
            os.startfile(e.target)  # type: ignore[attr-defined]
        else:
            pu.open_path(e.target)
