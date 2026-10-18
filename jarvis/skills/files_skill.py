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
OPEN_FILE_RE = re.compile(r"^(?:(?:джарвис|ну|давай|пожалуйста)\s+)*(?:открой|открыть|открою|запусти|покажи)\s+"
                          r"(?:мне\s+)?(?:мой\s+|мою\s+|мои\s+)?(?P<kind>файл\w*|документ\w*|папк\w*|фото\w*|"
                          r"картинк\w*|таблиц\w*|презентаци\w*)\s+(?:с\s+именем\s+|под\s+названием\s+|"
                          r"который\s+называется\s+)?(?P<q>.+)$")
PICK_RE = re.compile(r"^(?:(?:открой|давай|ну)\s+)?(?P<n>первый|первую|первое|второй|вторую|второе|третий|третью|"
                     r"третье|1|2|3|последний|последнюю)(?:\s+(?:файл|папку|вариант))?$")
PICK = {"перв": 0, "втор": 1, "трет": 2, "1": 0, "2": 1, "3": 2, "послед": -1}
PREFERRED = ("desktop", "рабочий стол", "documents", "документы", "downloads", "загрузки")
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

    # ------------------------------------------------------------ 1.0.9: «открой файл / папку X» fast path
    def _explorer_dirs(self) -> list[str]:
        """Folders open in Explorer (the window title is the folder name, or its full path if Windows shows it)."""
        wm = self.ctx.extra.get("wm")
        try:
            wins = [w for w in (wm.candidates() if wm is not None and wm.b is not None else []) if w.exe == "explorer.exe"
                    and w.cls == "CabinetWClass"]
        except Exception:                                # noqa: BLE001
            return []
        return [w.title.strip().lower() for w in wins if w.title.strip()]

    def _live_dirs(self, open_dirs: list[str]) -> list[Path]:
        home = Path(os.environ.get("USERPROFILE") or Path.home())
        out = [home / n for n in ("Desktop", "Documents", "Downloads", "Рабочий стол", "Документы", "Загрузки")]
        out += [home / "OneDrive" / n for n in ("Desktop", "Documents", "Рабочий стол", "Документы")]
        try:
            from ..diagbundle import desktop_dir
            out.append(desktop_dir())
        except Exception:                                # noqa: BLE001
            pass
        out += [Path(d) for d in open_dirs if re.match(r"^[a-z]:[\\/]", d)]   # Explorer shows the full path
        uniq: list[Path] = []
        for d in out:
            if d not in uniq and d.is_dir():
                uniq.append(d)
        return uniq

    def _live_items(self, open_dirs: list[str], cap: int = 5000) -> list[tuple[str, str]]:
        """1.0.9: the index is refreshed minutes after start and then every few hours, so a file saved a minute ago
        was «не нашёл». The Desktop / Documents / Downloads / open Explorer folders are listed live (top level
        only, capped, cached 5 s — has_match + open_file ask twice per phrase)."""
        now = time.monotonic()
        cache = getattr(self, "_live_cache", None)
        if cache and now - cache[0] < 5.0 and cache[1] == open_dirs:
            return cache[2]
        items: list[tuple[str, str]] = []
        for d in self._live_dirs(open_dirs):
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.name.startswith((".", "~$")) or e.name.lower() in ("desktop.ini", "thumbs.db"):
                            continue
                        items.append((e.path, phonetic(Path(e.name).stem)))
                        if len(items) >= cap:
                            break
            except OSError:
                continue
            if len(items) >= cap:
                break
        self._live_cache = (now, list(open_dirs), items)
        return items

    @staticmethod
    def _rank(q: str, qk: str, path: str, key: str) -> float:
        stem = Path(path).stem.lower().replace("ё", "е")
        name = Path(path).name.lower().replace("ё", "е")
        digits = re.sub(r"\D", "", q)
        if digits and digits == re.sub(r"\s", "", q):  # «1111»: phonetic keys drop digits -> raw match
            return 1.0 if stem == q or name == q else 0.92 if stem.startswith(q) else 0.85 if q in stem else 0.0
        if stem == q or name == q or (qk and key == qk):
            return 1.0
        if len(qk) < 3:                                 # «я», «a»: only exact names, never substrings
            return 0.0
        words = key.split()
        if key.startswith(qk + " ") or qk in words:
            return 0.93
        if key.startswith(qk):
            return 0.9
        if len(qk) >= 4 and qk in key:
            return 0.85
        sc = score(qk, key)
        return sc * 0.9 if sc >= 0.78 else 0.0

    def find_items(self, query: str, folders: bool = False, limit: int = 3) -> list[tuple[float, str]]:
        q = re.sub(r"[«»\"']", "", query).strip().lower().replace("ё", "е")
        q = re.sub(r"\s+(?:пожалуйста|джарвис)$", "", q)
        if not q:
            return []
        qk = phonetic(q)
        open_dirs = self._explorer_dirs()
        scored = []
        live = self._live_items(open_dirs)
        seen = {p for p, _ in live}
        for path, key in live + [x for x in self.index if x[0] not in seen]:
            base = self._rank(q, qk, path, key)
            if base <= 0:
                continue
            low = path.lower().replace("\\", "/")
            parent = low.rsplit("/", 2)[-2] if low.count("/") >= 1 else ""
            parent_full = low.rsplit("/", 1)[0]
            bonus = 0.04 if any(f"/{d}/" in low for d in PREFERRED) else 0.0
            if parent in open_dirs or parent_full in [d.replace("\\", "/") for d in open_dirs]:
                bonus += 0.06
            scored.append((round(base + bonus - 0.004 * low.count("/"), 4), base, path))
        scored.sort(key=lambda x: (-x[0], len(x[2])))
        out: list[tuple[float, str]] = []
        self._totals: dict[str, float] = {}
        for total, base, path in scored:
            if folders != os.path.isdir(path) and os.path.exists(path):
                continue
            if not os.path.exists(path):
                continue                                 # stale index entry
            out.append((base, path))
            self._totals[path] = total
            if len(out) >= limit:
                break
        if not out and self.cfg.get("use_windows_search", True) and not folders:
            out = [(0.8, p) for p in windows_search(q, limit)]
        return out

    def has_match(self, query: str, folders: bool = False) -> bool:
        try:
            return bool(self.find_items(query, folders, 1))
        except Exception:                                # noqa: BLE001
            return False

    @staticmethod
    def _where(path: str) -> str:
        low = path.lower().replace("\\", "/")
        for d, ru in (("desktop", "на рабочем столе"), ("рабочий стол", "на рабочем столе"),
                      ("downloads", "в загрузках"), ("загрузки", "в загрузках"),
                      ("documents", "в документах"), ("документы", "в документах")):
            if f"/{d}/" in low:
                return ru
        return "в папке " + Path(path).parent.name

    def open_file(self, query: str, folders: bool = False) -> ToolResult:
        items = self.find_items(query, folders)
        what = "папку" if folders else "файл"
        if not items:
            return ToolResult("" if self.ctx.extra.get("noname") else f"Не нашёл {what} «{query}».")
        exact = [p for b, p in items if b >= 1.0]
        tot = getattr(self, "_totals", {})
        if len(exact) > 1 and tot.get(exact[0], 0) - tot.get(exact[1], 0) >= 0.03:
            exact = exact[:1]                            # same name: Desktop / open Explorer folder wins
        clear = items[0][0] >= 0.85 and (len(items) == 1 or items[1][0] <= items[0][0] - 0.07)
        if len(exact) == 1 or (not exact and clear):
            path = exact[0] if exact else items[0][1]
            pu.open_path(path)
            self.results = [path]
            return ToolResult(f"Открываю {Path(path).name}.")
        self.results = [p for _, p in items]
        listing = "; ".join(f"{i + 1} — {Path(p).name} {self._where(p)}" for i, p in enumerate(self.results))
        links = [(Path(p).name + "  —  " + str(Path(p).parent), Path(p).as_uri()) for p in self.results]
        pick = self._pick_optional
        return ToolResult(f"Нашёл несколько: {listing}. Какой открыть — первый, второй?", links=links, expect=pick)

    def _pick_optional(self, text: str):
        try:
            return self._pick(text)
        except Exception as e:                           # noqa: BLE001
            log.warning("pick: %s", e)
            return ToolResult("Не получилось открыть.")
    _pick_optional.optional = True                       # type: ignore[attr-defined]

    def _pick(self, text: str) -> ToolResult:
        t = re.sub(r"[^\w\s]", "", (text or "").lower()).strip()
        m = PICK_RE.match(t)
        if re.match(r"^(нет|не надо|отмена|никакой|никакую)$", t):
            return ToolResult("Ок, не открываю.")
        if not m or not self.results:
            return None                                  # type: ignore[return-value]  # a new command
        n = m.group("n")
        i = next(v for k, v in PICK.items() if n.startswith(k))
        if i >= len(self.results):
            return ToolResult(f"Вариантов только {len(self.results)}.")
        path = self.results[i]
        pu.open_path(path)
        return ToolResult(f"Открываю {Path(path).name}.")

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        m = OPEN_FILE_RE.match(t)
        if m:
            from .core import FOLDERS
            q = m.group("q").strip()
            if m.group("kind").startswith("папк") and (q in FOLDERS or "папку " + q in FOLDERS):
                return None                              # «открой папку загрузки» — core's known folders
            return self.open_file(q, folders=m.group("kind").startswith("папк"))
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
