"""File search by voice: Windows Search index (if available) + own cached index of user folders."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .. import platform_utils as pu
from ..launcher import phonetic, score
from .base import Skill, ToolResult

log = logging.getLogger(__name__)

FIND_RE = re.compile(r"^(?:найди|поищи|где)\s+(?:мой\s+|мои\s+)?(?:файл\w*|документ\w*|папк\w*|фото\w*|картинк\w*|презентаци\w*|таблиц\w*)\s+(.+)$")
OPEN_N_RE = re.compile(r"^открой\s+(первый|второй|третий|четвертый|пятый|1|2|3|4|5)(?:\s+(?:файл|результат))?$")
FOLDER_N_RE = re.compile(r"^(?:открой|покажи)\s+папку\s+(первого|второго|третьего|1|2|3)(?:\s+файла)?$")
ORD = {"первый": 0, "второй": 1, "третий": 2, "четвертый": 3, "пятый": 4, "первого": 0, "второго": 1, "третьего": 2}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "AppData", "$RECYCLE.BIN", "System Volume Information", "Windows",
             ".venv", "venv", "site-packages", ".cache"}


def windows_search(query: str, limit: int = 10) -> list[str]:
    """Query the Windows Search index via PowerShell + ADODB (no pywin32 needed)."""
    if not pu.IS_WIN:
        return []
    q = query.replace("'", "''")
    ps = (
        "$c=New-Object -ComObject ADODB.Connection;"
        "$c.Open(\"Provider=Search.CollatorDSO;Extended Properties='Application=Windows';\");"
        f"$r=$c.Execute(\"SELECT TOP {limit} System.ItemPathDisplay FROM SYSTEMINDEX WHERE System.FileName LIKE '%{q}%' "
        "AND SCOPE='file:' ORDER BY System.DateModified DESC\");"
        "while(-not $r.EOF){$r.Fields.Item('System.ItemPathDisplay').Value;$r.MoveNext()}")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=10, encoding="utf-8", errors="replace", creationflags=0x08000000)
        return [l.strip() for l in out.stdout.splitlines() if l.strip()]
    except Exception as e:
        log.debug("windows search: %s", e)
        return []


class FilesSkill(Skill):
    name = "files"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.cfg = ctx.cfg.get("files", {})
        self.roots = [Path(os.path.expandvars(os.path.expanduser(r))) for r in
                      (self.cfg.get("roots") or ["~/Desktop", "~/Documents", "~/Downloads", "~/Pictures", "~/Videos", "D:/"])]
        self.index: list[tuple[str, str]] = []      # (path, phonetic name)
        self.results: list[str] = []
        self._built = 0.0
        self._building = False
        self._load_cache()
        # 1.0.8: no full disk walk at startup (it competed with Whisper/LLM loading): the cached index is used at
        # once, the refresh runs later at the lowest thread priority and throttled
        delay = float(self.cfg.get("index_delay_s", 120))
        threading.Thread(target=self._delayed_build, args=(delay,), name="jarvis-files-index", daemon=True).start()

    @staticmethod
    def _cache_path() -> Path:
        from ..config import LOCAL_SETTINGS
        return Path(LOCAL_SETTINGS).parent / "cache" / "files_index.json"

    def _load_cache(self) -> None:
        try:
            import json
            data = json.loads(self._cache_path().read_text(encoding="utf-8"))
            self.index = [tuple(x) for x in data.get("items", [])]   # type: ignore[misc]
            self._built = float(data.get("built", 0))
            log.info("Индекс файлов из кэша: %d", len(self.index))
        except Exception:                                # noqa: BLE001
            pass

    def _save_cache(self) -> None:
        try:
            import json
            p = self._cache_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"built": self._built, "items": self.index}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except Exception as e:                           # noqa: BLE001
            log.debug("files cache: %s", e)

    def _delayed_build(self, delay: float) -> None:
        if self.index and time.time() - self._built < 6 * 3600:
            delay = max(delay, 600.0)                     # fresh cache: refresh much later
        time.sleep(max(0.0, delay))
        self.build_index()

    def build_index(self) -> int:
        if self._building:
            return len(self.index)
        self._building = True
        try:
            from ..resources import lowest_thread_priority
            lowest_thread_priority()
            items, max_items = [], int(self.cfg.get("max_index", 300000))
            dirs = 0
            for root in self.roots:
                if not root.exists():
                    continue
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                    for name in filenames + dirnames:
                        items.append((os.path.join(dirpath, name), phonetic(Path(name).stem)))
                    dirs += 1
                    if dirs % 200 == 0:
                        time.sleep(0.02)                  # throttle: disk + CPU stay free for the user
                    if len(items) > max_items:
                        break
            self.index, self._built = items, time.time()
            log.info("Индекс файлов: %d", len(items))
            self._save_cache()
            return len(items)
        finally:
            self._building = False

    def search(self, query: str, limit: int = 5) -> list[str]:
        hits = windows_search(query, limit * 2) if self.cfg.get("use_windows_search", True) else []
        if len(hits) < limit:
            if time.time() - self._built > 3600:
                threading.Thread(target=self.build_index, daemon=True).start()
            qk = phonetic(query)
            scored = []
            for path, key in self.index:
                sc = 1.0 if qk and qk in key else score(qk, key)
                if sc >= 0.6:
                    scored.append((sc, path))
            scored.sort(key=lambda x: (-x[0], len(x[1])))
            hits += [p for _, p in scored if p not in hits]
        return hits[:limit]

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        m = OPEN_N_RE.match(t)
        if m and self.results:
            i = ORD.get(m.group(1), None)
            i = int(m.group(1)) - 1 if i is None else i
            if i < len(self.results):
                pu.open_path(self.results[i])
                return ToolResult(f"Открываю {Path(self.results[i]).name}.")
        m = FOLDER_N_RE.match(t)
        if m and self.results:
            i = ORD.get(m.group(1), None)
            i = int(m.group(1)) - 1 if i is None else i
            if i < len(self.results):
                pu.open_path(Path(self.results[i]).parent)
                return ToolResult("Открываю папку.")
        m = FIND_RE.match(t)
        if m:
            q = m.group(1).strip()
            self.results = self.search(q)
            if not self.results:
                return ToolResult(f"Файлов по запросу «{q}» не нашёл.")
            names = "; ".join(f"{i + 1}: {Path(p).name}" for i, p in enumerate(self.results[:3]))
            links = [(Path(p).name + "  —  " + str(Path(p).parent), Path(p).as_uri()) for p in self.results]
            return ToolResult(f"Нашёл {len(self.results)}. {names}. Скажи «открой первый».", links=links)
        return None
