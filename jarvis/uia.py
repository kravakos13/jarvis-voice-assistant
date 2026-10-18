"""Windows UI Automation (via comtypes, already a dependency): read / set a browser's address bar.

Only Chrome-like browsers with a normal tab window have an address bar; installed web apps (PWA) do not.
Every function is best-effort: returns None/False on any problem and never raises.
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Any, Optional

log = logging.getLogger(__name__)
VK_RETURN = 0x0D


def _uia() -> tuple[Any, Any]:
    import comtypes
    import comtypes.client
    try:
        comtypes.CoInitialize()          # called from worker threads
    except OSError:
        pass
    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as UIA  # type: ignore[import-not-found]
    return UIA, comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)


def _omnibox(hwnd: int) -> tuple[Any, Any]:
    """(edit element, ValuePattern) of the first Edit control of the window = the browser's address bar."""
    UIA, auto = _uia()
    el = auto.ElementFromHandle(hwnd)
    cond = auto.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_EditControlTypeId)
    edit = el.FindFirst(UIA.TreeScope_Descendants, cond)
    if not edit:
        return None, None
    pat = edit.GetCurrentPattern(UIA.UIA_ValuePatternId).QueryInterface(UIA.IUIAutomationValuePattern)
    return edit, pat


def normalize_url(value: str) -> Optional[str]:
    v = (value or "").strip()
    if not v or " " in v:
        return None
    if not v.startswith(("http://", "https://")):
        v = "https://" + v                 # Chrome hides the scheme
    return v


def address_bar(hwnd: int) -> Optional[str]:
    if sys.platform != "win32":
        return None
    try:
        _edit, pat = _omnibox(hwnd)
        return normalize_url(str(pat.CurrentValue)) if pat else None
    except Exception as e:                  # noqa: BLE001
        log.debug("UIA address bar: %s", e)
        return None


def navigate(hwnd: int, url: str, backend: Any = None) -> bool:
    """Type the URL into the address bar of THIS tab and press Enter (same window, same tab)."""
    if sys.platform != "win32":
        return False
    try:
        if backend is not None:
            backend.activate(hwnd)
            time.sleep(0.15)
        edit, pat = _omnibox(hwnd)
        if not pat:
            return False
        edit.SetFocus()
        pat.SetValue(url)
        import ctypes
        u = ctypes.windll.user32       # type: ignore[attr-defined]
        u.keybd_event(VK_RETURN, 0, 0, 0)
        u.keybd_event(VK_RETURN, 0, 2, 0)
        return True
    except Exception as e:                  # noqa: BLE001
        log.debug("UIA navigate: %s", e)
        return False


AD_WORDS = ("реклама", "спонсор", "sponsored", "ad ·", "объявление")


def order_links(links: list[dict], row_px: int = 60) -> list[dict]:
    """Links -> one entry per video in reading order (rows top->bottom, then left->right).
    links: [{'href','name','rect':(l,t,r,b), 'el'}]. Shorts, ads, off-site and invisible (0-size) links dropped."""
    import re
    best: dict[str, dict] = {}
    order: list[str] = []
    for ln in links:
        href = str(ln.get("href") or "")
        name = str(ln.get("name") or "")
        m = re.search(r"(?:youtube\.com)?/watch\?(?:.*&)?v=([\w-]{6,})", href)
        if not m or "/shorts/" in href or "googleadservices" in href or "adurl=" in href:
            continue
        if any(w in name.lower() for w in AD_WORDS):
            continue
        rect = ln.get("rect") or (0, 0, 0, 0)
        if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
            continue
        vid = m.group(1)
        cur = best.get(vid)
        if cur is None:
            best[vid] = {"id": vid, "title": name, "rect": rect, "el": ln.get("el"), "channel": ""}
            order.append(vid)
        elif len(name) > len(cur["title"]):
            cur["title"] = name                  # the title link carries the name, the thumbnail does not
    items = [best[v] for v in order]
    items.sort(key=lambda x: (round(x["rect"][1] / row_px), x["rect"][0]))
    return items


def video_links(hwnd: int, retries: int = 2) -> list[dict]:
    """YouTube video links rendered in the window, in on-screen order (works for PWA windows too).
    Chrome builds its accessibility tree on the first UIA query, so an empty result is retried once."""
    if sys.platform != "win32":
        return []
    try:
        UIA, auto = _uia()
        root = auto.ElementFromHandle(hwnd)
        cond = auto.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_HyperlinkControlTypeId)
        for attempt in range(retries):
            arr = root.FindAll(UIA.TreeScope_Descendants, cond)
            links = []
            for i in range(arr.Length if arr else 0):
                el = arr.GetElement(i)
                try:
                    href = el.GetCurrentPattern(UIA.UIA_ValuePatternId).QueryInterface(
                        UIA.IUIAutomationValuePattern).CurrentValue
                except Exception:               # noqa: BLE001
                    continue
                r = el.CurrentBoundingRectangle
                links.append({"href": href, "name": el.CurrentName, "rect": (r.left, r.top, r.right, r.bottom), "el": el})
            items = order_links(links)
            if items or attempt == retries - 1:
                return items
            time.sleep(0.6)
    except Exception as e:                      # noqa: BLE001
        log.debug("UIA links: %s", e)
    return []


def page_links(hwnd: int) -> list[dict]:
    """All hyperlinks of a browser window: {href, name, rect, el} (unsorted). [] if UIA is unavailable."""
    if sys.platform != "win32":
        return []
    try:
        UIA, auto = _uia()
        root = auto.ElementFromHandle(hwnd)
        cond = auto.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_HyperlinkControlTypeId)
        for attempt in range(2):
            arr = root.FindAll(UIA.TreeScope_Descendants, cond)
            links = []
            for i in range(arr.Length if arr else 0):
                el = arr.GetElement(i)
                try:
                    href = el.GetCurrentPattern(UIA.UIA_ValuePatternId).QueryInterface(
                        UIA.IUIAutomationValuePattern).CurrentValue
                except Exception:               # noqa: BLE001
                    continue
                r = el.CurrentBoundingRectangle
                links.append({"href": href, "name": el.CurrentName, "rect": (r.left, r.top, r.right, r.bottom),
                              "el": el})
            if links or attempt:
                return links
            time.sleep(0.6)                     # Chrome builds the accessibility tree on the first query
    except Exception as e:                      # noqa: BLE001
        log.debug("UIA page links: %s", e)
    return []


NOT_RESULT_HOSTS = ("google.", "gstatic.", "googleusercontent.", "googleadservices.", "doubleclick.", "yandex.ru/clck",
                    "accounts.", "support.google", "policies.google", "maps.google")


def result_links(links: list[dict], min_w: int = 150) -> list[dict[str, Any]]:
    """Search-result links (Google/Yandex/DDG pages) in reading order: external, visible, titled, one per URL.
    Ads («Реклама», googleadservices) and the engine's own navigation are skipped."""
    import urllib.parse
    out: list[dict[str, Any]] = []
    seen: set = set()
    for ln in links:
        href = str(ln.get("href") or "")
        name = " ".join(str(ln.get("name") or "").split())
        left, top, right, bottom = tuple(ln.get("rect") or (0, 0, 0, 0))
        if not href.startswith(("http://", "https://")) or right - left < min_w or bottom <= top or len(name) < 6:
            continue
        low = href.lower()
        if any(h in low.split("?")[0] for h in NOT_RESULT_HOSTS) or name.lower().startswith(("реклама", "спонсор", "ad ")):
            continue
        u = urllib.parse.urlsplit(href)
        key = (u.netloc.lower().removeprefix("www."), u.path.rstrip("/"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"href": href, "title": name, "rect": (left, top, right, bottom), "el": ln.get("el")})
    out.sort(key=lambda x: (x["rect"][1] // 40, x["rect"][0]))
    return out


def browser_tabs(hwnd: int) -> list[dict]:
    """Tabs of a Chromium/Firefox window: [{name, el, selected}] in strip order. [] if unavailable."""
    if sys.platform != "win32":
        return []
    try:
        UIA, auto = _uia()
        root = auto.ElementFromHandle(hwnd)
        cond = auto.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_TabItemControlTypeId)
        arr = root.FindAll(UIA.TreeScope_Descendants, cond)
        out = []
        for i in range(arr.Length if arr else 0):
            el = arr.GetElement(i)
            sel = False
            try:
                sel = bool(el.GetCurrentPattern(UIA.UIA_SelectionItemPatternId).QueryInterface(
                    UIA.IUIAutomationSelectionItemPattern).CurrentIsSelected)
            except Exception:                   # noqa: BLE001
                pass
            out.append({"name": str(el.CurrentName or ""), "el": el, "selected": sel})
        return out
    except Exception as e:                      # noqa: BLE001
        log.debug("UIA tabs: %s", e)
        return []


def select_tab(el: Any) -> bool:
    """Make a browser tab active (SelectionItem.Select, Invoke as fallback)."""
    if el is None or sys.platform != "win32":
        return False
    try:
        UIA, _auto = _uia()
        el.GetCurrentPattern(UIA.UIA_SelectionItemPatternId).QueryInterface(
            UIA.IUIAutomationSelectionItemPattern).Select()
        return True
    except Exception:                           # noqa: BLE001
        return invoke(el)


def invoke(el: Any) -> bool:
    """Click a link element through UI Automation (same window, same tab)."""
    if el is None or sys.platform != "win32":
        return False
    try:
        UIA, _auto = _uia()
        el.GetCurrentPattern(UIA.UIA_InvokePatternId).QueryInterface(UIA.IUIAutomationInvokePattern).Invoke()
        return True
    except Exception as e:                      # noqa: BLE001
        log.debug("UIA invoke: %s", e)
        return False


def browser_audio_playing(exes: tuple = ("chrome.exe", "msedge.exe", "brave.exe", "opera.exe", "vivaldi.exe"),
                          threshold: float = 0.002) -> Optional[bool]:
    """True if a browser audio session has signal right now; None if this cannot be checked (no pycaw)."""
    if sys.platform != "win32":
        return None
    try:
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
        for ses in AudioUtilities.GetAllSessions():
            if ses.Process and ses.Process.name().lower() in exes:
                meter = ses._ctl.QueryInterface(IAudioMeterInformation)
                if meter.GetPeakValue() > threshold:
                    return True
        return False
    except Exception as e:                      # noqa: BLE001
        log.debug("audio meter: %s", e)
        return None


def media_playing(threshold: float = 0.002) -> Optional[bool]:
    """Some OTHER program is making sound right now (Jarvis's own voice excluded). None = cannot check."""
    if sys.platform != "win32":
        return None
    try:
        import os
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
        me = os.getpid()
        for ses in AudioUtilities.GetAllSessions():
            if ses.Process and ses.Process.pid != me:
                if ses._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue() > threshold:
                    return True
        return False
    except Exception as e:                      # noqa: BLE001
        log.debug("audio meter: %s", e)
        return None


def close_window(hwnd: int) -> bool:
    """WindowPattern.Close through UI Automation (fallback for Store apps that ignore WM_CLOSE)."""
    if sys.platform != "win32":
        return False
    try:
        UIA, auto = _uia()
        el = auto.ElementFromHandle(hwnd)
        el.GetCurrentPattern(UIA.UIA_WindowPatternId).QueryInterface(UIA.IUIAutomationWindowPattern).Close()
        return True
    except Exception as e:                      # noqa: BLE001
        log.debug("UIA close: %s", e)
        return False


def focus_in_edit() -> Optional[bool]:
    """True if keyboard focus is in a text box (YouTube search), None if unknown."""
    if sys.platform != "win32":
        return None
    try:
        UIA, auto = _uia()
        el = auto.GetFocusedElement()
        return bool(el) and int(el.CurrentControlType) in (UIA.UIA_EditControlTypeId, UIA.UIA_ComboBoxControlTypeId)
    except Exception as e:                      # noqa: BLE001
        log.debug("UIA focus: %s", e)
        return None


PLAYER_NAMES = ("youtube video player", "видеопроигрыватель youtube", "проигрыватель youtube", "видеоплеер youtube")


def focus_player(hwnd: int) -> bool:
    """Give keyboard focus to YouTube's player (no mouse click): UIA SetFocus on the «YouTube Video Player» group."""
    if sys.platform != "win32":
        return False
    try:
        UIA, auto = _uia()
        root = auto.ElementFromHandle(hwnd)
        arr = root.FindAll(UIA.TreeScope_Descendants, auto.CreatePropertyCondition(
            UIA.UIA_ControlTypePropertyId, UIA.UIA_GroupControlTypeId))
        for i in range(arr.Length if arr else 0):
            el = arr.GetElement(i)
            name = str(el.CurrentName or "").lower()
            if any(n in name for n in PLAYER_NAMES) or str(el.CurrentAutomationId or "") == "movie_player":
                el.SetFocus()
                return True
    except Exception as e:                      # noqa: BLE001
        log.debug("UIA player focus: %s", e)
    return False
