"""Windows Explorer by voice (1.0.7): «открой второй файл», «открой третью папку», «открой второе», «назад», «вверх».

Order = what the user SEES: UI Automation list items of the folder view in view order (respects the user's sort,
grouping and view mode); file/folder type and full path come from Shell.Application (the same folder, matched by
display name). If UIA is unavailable the Shell's own item order is used. Best-effort: nothing here raises.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import PureWindowsPath as WPath
from typing import Any, Callable, Optional

from .ytpick import CARDINALS, FILLER, _ordinal

log = logging.getLogger(__name__)

EXPLORER_CLASSES = {"CabinetWClass", "ExploreWClass"}
FILE_NOUNS = re.compile(r"^(?:файл\w*|фаил\w*|документ\w*|док)$")
FOLDER_NOUNS = re.compile(r"^(?:папк\w*|папочк\w*|каталог\w*|директори\w*|дирректори\w*)$")
ANY_NOUNS = re.compile(r"^(?:элемент\w*|пункт\w*|объект\w*|штук\w*)$")
VERBS = re.compile(r"^(?:откр\w*|запуст\w*|зайди|зайти|зайдём|перейди|войди|давай|выбер\w*|кликн\w*|нажм\w*|"
                   r"ткни|тыкни|покаж\w*|включ\w*)$")
EX_FILLER = (FILLER - {"ютубе", "ютуб", "youtube", "поиске", "поиска", "выдаче"}) | {
    "проводнике", "проводник", "папке", "списке", "сверху", "по", "счету", "счёту", "мне"}
BACK_RE = re.compile(r"^(?:назад|вернись(?: назад)?|обратно|шаг назад|предыдущая папка|верни назад)$")
UP_RE = re.compile(r"^(?:вверх|наверх|выше|на уровень (?:выше|вверх)|папка выше|в родительскую папку|"
                   r"поднимись(?: выше| вверх| наверх)?|подними(?:сь)? на уровень выше)$")
CARD_20 = dict(CARDINALS, **{"одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
                             "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
                             "девятнадцать": 19, "двадцать": 20})


@dataclass
class ItemPick:
    kind: str            # "file" | "folder" | "any" | "back" | "up"
    n: int = 0           # 1-based; -1 = last
    explicit: bool = False   # a noun was said («файл», «папку») -> Explorer even if YouTube is in front


def parse(norm: str) -> Optional[ItemPick]:
    t = (norm or "").strip()
    if BACK_RE.match(t):
        return ItemPick("back")
    if UP_RE.match(t):
        return ItemPick("up")
    toks = [x for x in t.replace("-", " ").split() if x]
    if not toks or len(toks) > 8:
        return None
    verb, kind, n = False, "any", None
    explicit = False
    i = 0
    while i < len(toks):
        w = toks[i]
        if w in ("номер", "номером", "№") and i + 1 < len(toks) and (toks[i + 1] in CARD_20 or toks[i + 1].isdigit()):
            if n is not None:
                return None
            n = CARD_20.get(toks[i + 1]) or int(toks[i + 1])
            i += 2
            continue
        if VERBS.match(w):
            verb = True
        elif FILE_NOUNS.match(w):
            kind, explicit = "file", True
        elif FOLDER_NOUNS.match(w):
            kind, explicit = "folder", True
        elif ANY_NOUNS.match(w):
            explicit = True
        elif (o := _ordinal(w)) is not None:
            if n is not None:
                return None
            n = o
        elif w not in EX_FILLER:
            return None
        i += 1
    if n is None or not (verb or explicit) or n == 0:
        return None
    return ItemPick(kind, n, explicit)


def pick_item(items: list[dict], p: ItemPick) -> Optional[dict]:
    pool = [x for x in items if p.kind == "any" or (x.get("folder") if p.kind == "folder" else not x.get("folder"))]
    if not pool:
        return None
    if p.n < 0:
        return pool[p.n] if -p.n <= len(pool) else None
    return pool[p.n - 1] if p.n <= len(pool) else None


def _plain(s: str) -> str:
    return " ".join((s or "").lower().replace("ё", "е").split())


def order_by_view(shell_items: list[dict], view_names: list[str]) -> list[dict]:
    """Shell items re-ordered as the view shows them (by display name). Unmatched view names are skipped;
    if nothing matches the shell order is kept."""
    if not view_names:
        return list(shell_items)
    by: dict[str, list[dict]] = {}
    for it in shell_items:
        by.setdefault(_plain(it["name"]), []).append(it)
    out = []
    for nm in view_names:
        lst = by.get(_plain(nm))
        if lst:
            out.append(lst.pop(0))
    return out or list(shell_items)


# ------------------------------------------------------------------------------------------- Windows backend
class ShellBackend:
    """Shell.Application + UIA. Each call creates its own COM objects (called from worker threads)."""

    def _shell_window(self, hwnd: int, title: str = "") -> Any:
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        sh = comtypes.client.CreateObject("Shell.Application", dynamic=True)
        best = None
        wins = sh.Windows()
        for i in range(int(wins.Count)):
            w = wins.Item(i)
            try:
                if w is None or int(w.HWND) != int(hwnd):
                    continue
            except Exception:                            # noqa: BLE001 — IE / dead windows
                continue
            best = best or w
            # Windows 11 tabs share one HWND: the active tab's folder name is the window title
            if title and str(w.LocationName or "").strip() and title.startswith(str(w.LocationName)):
                return w
        return best

    def folder(self, hwnd: int, title: str = "") -> Optional[str]:
        w = self._shell_window(hwnd, title)
        try:
            return str(w.Document.Folder.Self.Path) if w is not None else None
        except Exception:                                # noqa: BLE001
            return None

    def items(self, hwnd: int, title: str = "") -> list[dict]:
        w = self._shell_window(hwnd, title)
        if w is None:
            return []
        out = []
        try:
            fi = w.Document.Folder.Items()
            for i in range(int(fi.Count)):
                it = fi.Item(i)
                out.append({"name": str(it.Name), "path": str(it.Path), "folder": bool(it.IsFolder)
                            and not str(it.Path).lower().endswith((".zip", ".cab"))})
        except Exception as e:                           # noqa: BLE001
            log.debug("shell items: %s", e)
        return out

    def view_names(self, hwnd: int) -> list[str]:
        """Names of the folder view's list items in on-screen order (UIA)."""
        try:
            from .uia import _uia
            UIA, auto = _uia()
            root = auto.ElementFromHandle(hwnd)
            cond = auto.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_ListItemControlTypeId)
            arr = root.FindAll(UIA.TreeScope_Descendants, cond)
            names = []
            for i in range(arr.Length):
                el = arr.GetElement(i)
                nm = str(el.CurrentName or "").strip()
                if nm:
                    names.append(nm)
            return names
        except Exception as e:                           # noqa: BLE001
            log.debug("uia view: %s", e)
            return []

    def navigate(self, hwnd: int, path: str, title: str = "") -> bool:
        w = self._shell_window(hwnd, title)
        try:
            w.Navigate2(path)
            return True
        except Exception as e:                           # noqa: BLE001
            log.debug("navigate: %s", e)
            return False

    def back(self, hwnd: int, title: str = "") -> bool:
        w = self._shell_window(hwnd, title)
        try:
            w.GoBack()
            return True
        except Exception as e:                           # noqa: BLE001 — nothing to go back to
            log.debug("back: %s", e)
            return False

    @staticmethod
    def open_file(path: str) -> bool:
        try:
            os.startfile(path)                           # type: ignore[attr-defined]
            return True
        except Exception as e:                           # noqa: BLE001
            log.warning("Не открылся файл %s: %s", path, e)
            return False


class Explorer:
    def __init__(self, wm: Any, backend: Any = None, sleep: Optional[Callable[[float], None]] = None):
        import time
        self.wm = wm
        self.sh: Any = backend if backend is not None else (ShellBackend() if sys.platform == "win32" else None)
        self.sleep = sleep if sleep is not None else time.sleep

    def windows(self) -> list:
        b = getattr(self.wm, "b", None)
        return [w for w in (b.windows() if b else []) if getattr(w, "cls", "") in EXPLORER_CLASSES]

    def active(self) -> Optional[Any]:
        """Explorer window the user means: the foreground one (or the one before Jarvis)."""
        w = self.wm.active() if self.wm is not None else None
        return w if w is not None and getattr(w, "cls", "") in EXPLORER_CLASSES else None

    def target_for_bare(self) -> Optional[Any]:
        """Bare «открой второе»: Explorer only if it is in front, or — when neither Explorer nor YouTube is in
        front — if Explorer is above YouTube in the z-order (most recently used)."""
        w = self.wm.active() if self.wm is not None else None
        if w is not None:
            if getattr(w, "cls", "") in EXPLORER_CLASSES:
                return w
            if getattr(w, "is_browser", False) and "youtube" in (w.title or "").lower():
                return None
        b = getattr(self.wm, "b", None)
        for x in (b.windows() if b else []):              # EnumWindows = top-to-bottom z-order
            if getattr(x, "cls", "") in EXPLORER_CLASSES:
                return x
            if getattr(x, "is_browser", False) and "youtube" in (x.title or "").lower():
                return None
        return None

    def list_items(self, w) -> list[dict]:
        items = self.sh.items(w.hwnd, w.title)
        return order_by_view(items, self.sh.view_names(w.hwnd))

    def open(self, w, p: ItemPick) -> str:
        if self.sh is None:
            return "Проводником я управляю только в Windows."
        if p.kind == "back":
            before = self.sh.folder(w.hwnd, w.title)
            if self.sh.back(w.hwnd, w.title):
                self.sleep(0.4)
                now = self.sh.folder(w.hwnd)
                if now and now != before:
                    return f"Назад: {WPath(now).name or now}."
            return "Назад некуда."
        if p.kind == "up":
            cur = self.sh.folder(w.hwnd, w.title)
            if not cur:
                return "Не понял, какая папка открыта."
            parent = str(WPath(cur).parent)
            if not parent or parent == cur:
                return "Выше некуда."
            ok = self.sh.navigate(w.hwnd, parent, w.title)
            return f"Поднялся в {WPath(parent).name or parent}." if ok else "Не получилось подняться выше."
        items = self.list_items(w)
        what = {"file": "файлов", "folder": "папок", "any": "элементов"}[p.kind]
        it = pick_item(items, p)
        if it is None:
            have = len([x for x in items if p.kind == "any" or bool(x.get("folder")) == (p.kind == "folder")])
            return f"Здесь всего {have} {what}." if have else f"В этой папке нет {what}."
        name = it["name"]
        if it.get("folder"):
            if not self.sh.navigate(w.hwnd, it["path"], w.title):
                return f"Не получилось открыть папку {name}."
            self.sleep(0.4)
            now = self.sh.folder(w.hwnd)
            if now and _plain(now) != _plain(it["path"]):
                return f"Не получилось открыть папку {name}."
            return f"Открыл папку {name}."
        return f"Открываю {name}." if self.sh.open_file(it["path"]) else f"Не получилось открыть {name}."
