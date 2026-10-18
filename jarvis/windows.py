"""Top-level windows: list, find by spoken name, close politely, minimize/maximize (Windows).

Why not process names: YouTube / YouTube Music are Chrome PWA windows (chrome.exe started via chrome_proxy.exe
--app-id), so "youtube.exe" does not exist and killing chrome.exe would close every browser window. Instead:
  * EnumWindows -> visible, un-owned, non-tool, non-cloaked top-level windows with a title (+ pid, exe, class).
  * Match the spoken target against the window TITLE (and exe for normal apps).
  * Close exactly that window with WM_CLOSE (what the X button does) and VERIFY it disappeared; the caller only
    says "Закрыл" after verification. A browser window whose *tab* is YouTube is not closed as a whole: the tab
    is closed with Ctrl+W instead.
  * taskkill (/PID, polite, never /IM) only for non-browser apps that ignore WM_CLOSE and show no dialog.
  * Never touches Jarvis itself, the desktop/taskbar shell or system windows.
All OS calls go through a backend object so the logic is unit-tested on any OS (FakeBackend in tests).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

IS_WIN = sys.platform == "win32"

BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe", "browser.exe", "vivaldi.exe",
            "chrome_proxy.exe", "yandex.exe", "chromium.exe", "arc.exe"}
BROWSER_SUFFIX = re.compile(r"\s[-—–]\s(google chrome|microsoft\u200b? edge|mozilla firefox|opera|brave|vivaldi|"
                            r"яндекс браузер|yandex browser|chromium)\s*$", re.I)
SYSTEM_EXES = {"dwm.exe", "csrss.exe", "winlogon.exe", "lsass.exe", "svchost.exe", "sihost.exe", "fontdrvhost.exe",
               "shellexperiencehost.exe", "startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe",
               "searchui.exe", "textinputhost.exe", "lockapp.exe", "logonui.exe", "ctfmon.exe", "runtimebroker.exe",
               "systemsettingsbroker.exe", "securityhealthsystray.exe", "widgets.exe", "gamebar.exe",
               "explorer.exe"}             # explorer: only its folder windows (CabinetWClass) are closable
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
                 "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow", "TopLevelWindowForOverflowXamlIsland"}
EXPLORER_FOLDER_CLASSES = {"CabinetWClass", "ExploreWClass"}


@dataclass
class Window:
    hwnd: int
    title: str
    pid: int
    exe: str = ""        # lower-case base name, e.g. "chrome.exe"
    cls: str = ""

    @property
    def is_browser(self) -> bool:
        return self.exe in BROWSERS

    @property
    def is_browser_tab_window(self) -> bool:
        """A normal browser window (title ends with '- Google Chrome'); a PWA window has no such suffix."""
        return self.is_browser and bool(BROWSER_SUFFIX.search(self.title))


# ------------------------------------------------------------------------------------------------ backends
class Backend:
    def windows(self) -> list[Window]:
        raise NotImplementedError

    def foreground(self) -> int:
        return 0

    def alive(self, hwnd: int) -> bool:
        raise NotImplementedError

    def title(self, hwnd: int) -> str:
        return ""

    def enabled(self, hwnd: int) -> bool:
        return True

    def hung(self, hwnd: int) -> bool:
        return False

    def post_close(self, hwnd: int) -> bool:
        raise NotImplementedError

    def show(self, hwnd: int, cmd: str) -> bool:      # minimize | maximize | restore
        return False

    def activate(self, hwnd: int) -> bool:
        return False

    def ctrl_w(self, hwnd: int) -> bool:
        return False

    def kill(self, pid: int, force: bool = False) -> bool:
        return False

    def show_desktop(self) -> bool:
        return False


class Win32Backend(Backend):
    """ctypes implementation (also works under Wine for the integration test)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.u = ctypes.windll.user32
        self.k = ctypes.windll.kernel32
        try:
            self.dwm = ctypes.windll.dwmapi
        except OSError:
            self.dwm = None
        self.u.GetWindowLongW.restype = ctypes.c_long
        self._exe_cache: dict = {}

    def _exe(self, pid: int) -> str:
        if pid in self._exe_cache:
            return self._exe_cache[pid]
        name = ""
        try:
            import psutil
            name = psutil.Process(pid).name().lower()
        except Exception:
            try:
                h = self.k.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
                if h:
                    buf = self.ct.create_unicode_buffer(1024)
                    size = self.wt.DWORD(1024)
                    if self.k.QueryFullProcessImageNameW(h, 0, buf, self.ct.byref(size)):
                        name = os.path.basename(buf.value).lower()
                    self.k.CloseHandle(h)
            except Exception:
                pass
        self._exe_cache[pid] = name
        return name

    def _cloaked(self, hwnd: int) -> bool:
        if self.dwm is None:
            return False
        val = self.wt.DWORD(0)
        try:
            if self.dwm.DwmGetWindowAttribute(self.wt.HWND(hwnd), 14, self.ct.byref(val), 4) == 0:   # DWMWA_CLOAKED
                return bool(val.value)
        except Exception:
            pass
        return False

    def title(self, hwnd: int) -> str:
        n = self.u.GetWindowTextLengthW(hwnd)
        buf = self.ct.create_unicode_buffer(n + 1)
        self.u.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _cls(self, hwnd: int) -> str:
        buf = self.ct.create_unicode_buffer(256)
        self.u.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def windows(self) -> list[Window]:
        out: list[Window] = []
        ct, wt, u = self.ct, self.wt, self.u
        proc = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def cb(hwnd, _):
            try:
                h = int(hwnd or 0)
                if not h or not u.IsWindowVisible(hwnd) or u.GetWindow(hwnd, 4):     # GW_OWNER
                    return True
                if u.GetWindowLongW(hwnd, -20) & 0x80:                               # WS_EX_TOOLWINDOW
                    return True
                title = self.title(h).strip()
                if not title or self._cloaked(h):
                    return True
                pid = wt.DWORD(0)
                u.GetWindowThreadProcessId(hwnd, ct.byref(pid))
                out.append(Window(h, title, int(pid.value), self._exe(int(pid.value)), self._cls(h)))
            except Exception as e:                                # never break enumeration
                log.debug("enum: %s", e)
            return True
        u.EnumWindows(proc(cb), 0)
        return out

    def foreground(self) -> int:
        return int(self.u.GetForegroundWindow() or 0)

    def alive(self, hwnd: int) -> bool:
        return bool(self.u.IsWindow(hwnd)) and bool(self.u.IsWindowVisible(hwnd))

    def enabled(self, hwnd: int) -> bool:
        return bool(self.u.IsWindowEnabled(hwnd))

    def hung(self, hwnd: int) -> bool:
        try:
            return bool(self.u.IsHungAppWindow(hwnd))
        except Exception:
            return False

    def post_close(self, hwnd: int) -> bool:
        return bool(self.u.PostMessageW(hwnd, 0x0010, 0, 0))                         # WM_CLOSE

    def show(self, hwnd: int, cmd: str) -> bool:
        sw = {"minimize": 6, "maximize": 3, "restore": 9}[cmd]
        self.u.ShowWindow(hwnd, sw)
        if cmd != "minimize":
            self.activate(hwnd)
        return True

    def activate(self, hwnd: int) -> bool:
        u = self.u
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)
        # Alt tap lifts the foreground lock for background processes (documented SetForegroundWindow rules)
        u.keybd_event(0x12, 0, 0, 0)
        u.keybd_event(0x12, 0, 2, 0)
        return bool(u.SetForegroundWindow(hwnd))

    def ctrl_w(self, hwnd: int) -> bool:
        if not self.activate(hwnd):
            return False
        time.sleep(0.15)
        if self.foreground() != hwnd:
            return False
        u = self.u
        u.keybd_event(0x11, 0, 0, 0)
        u.keybd_event(0x57, 0, 0, 0)
        u.keybd_event(0x57, 0, 2, 0)
        u.keybd_event(0x11, 0, 2, 0)
        return True

    def kill(self, pid: int, force: bool = False) -> bool:
        argv = ["taskkill", "/PID", str(pid)] + (["/F"] if force else [])
        try:
            return subprocess.run(argv, capture_output=True, timeout=10,
                                  creationflags=0x08000000).returncode == 0           # CREATE_NO_WINDOW
        except Exception as e:
            log.warning("taskkill %s: %s", pid, e)
            return False

    def show_desktop(self) -> bool:
        u = self.u
        u.keybd_event(0x5B, 0, 0, 0)          # Win+D
        u.keybd_event(0x44, 0, 0, 0)
        u.keybd_event(0x44, 0, 2, 0)
        u.keybd_event(0x5B, 0, 2, 0)
        return True


def default_backend() -> Optional[Backend]:
    if not IS_WIN:
        return None
    try:
        return Win32Backend()
    except Exception as e:
        log.warning("windows backend: %s", e)
        return None


# ------------------------------------------------------------------------------------------------ targets
@dataclass
class Target:
    """What the user means: spoken key -> how to find its window."""
    key: str
    title_ru: str                      # for the reply: «Закрыл ютуб»
    title_re: Optional[str] = None     # regex against the window title
    exes: tuple = ()                   # process names
    exclude_re: Optional[str] = None   # title regex that must NOT match


TARGETS: list[Target] = [
    Target("ютуб музыка", "ютуб музыку", r"youtube music", exclude_re=None),
    Target("ютуб", "ютуб", r"youtube", exclude_re=r"youtube music"),
    Target("хром", "Chrome", r"google chrome$", ("chrome.exe",)),
    Target("эдж", "Edge", r"microsoft\u200b? edge$", ("msedge.exe",)),
    Target("файрфокс", "Firefox", r"mozilla firefox$", ("firefox.exe",)),
    Target("яндекс браузер", "Яндекс Браузер", None, ("browser.exe",)),
    Target("опера", "Opera", None, ("opera.exe",)),
    Target("стим", "Steam", r"^steam$|steam$", ("steam.exe", "steamwebhelper.exe")),
    Target("блокнот", "блокнот", r"блокнот$|notepad$", ("notepad.exe",)),
    Target("телеграм", "Телеграм", r"telegram", ("telegram.exe",)),
    Target("дискорд", "Дискорд", r"discord", ("discord.exe",)),
    Target("проводник", "проводник", None, ("explorer.exe",)),
    Target("диспетчер задач", "диспетчер задач", r"диспетчер задач|task manager", ("taskmgr.exe",)),
    Target("калькулятор", "калькулятор", r"калькулятор|calculator", ("calculatorapp.exe", "calc.exe")),
    Target("ворд", "Word", r"\bword$", ("winword.exe",)),
    Target("эксель", "Excel", r"\bexcel$", ("excel.exe",)),
    Target("вс код", "VS Code", r"visual studio code$", ("code.exe",)),
    Target("спотифай", "Spotify", r"spotify", ("spotify.exe",)),
    Target("вайбер", "Viber", r"viber", ("viber.exe",)),
    Target("эпик", "Epic Games", r"epic games", ("epicgameslauncher.exe",)),
    Target("обс", "OBS", r"\bobs\b", ("obs64.exe", "obs32.exe")),
    Target("настройки", "настройки", r"^параметры$|^settings$", ("systemsettings.exe",)),
    Target("браузер", "браузер", None, tuple(sorted(BROWSERS - {"chrome_proxy.exe"}))),
]

# spoken variants / Whisper mishearings -> target key
ALIASES = {
    "ютуб музыка": ("ютуб музык", "ютуб мьюзик", "ютьюб музык", "youtube music", "yt music", "ю туб музык",
                    "музык", "музон", "музло", "песн", "трек", "плеер"),
    "ютуб": ("ютуб", "ютюб", "ютьюб", "ю туб", "утуб", "юдуб", "ютоб", "ютубчик", "youtube", "you tube", "видос",
             "видео"),
    "хром": ("хром", "гугл хром", "google chrome", "chrome", "хрон", "гугл"),
    "эдж": ("эдж", "edge", "едж", "майкрософт эдж"),
    "файрфокс": ("файрфокс", "firefox", "фаерфокс", "мозилл"),
    "яндекс браузер": ("яндекс браузер", "яндекс", "yandex"),
    "опера": ("опер", "opera"),
    "стим": ("стим", "steam", "стем", "стима"),
    "блокнот": ("блокнот", "блок нот", "notepad", "нотпад", "блакнот"),
    "телеграм": ("телеграм", "телега", "телегу", "telegram", "тг"),
    "дискорд": ("дискорд", "discord", "дс", "дискорт"),
    "проводник": ("проводник", "explorer", "папк"),
    "диспетчер задач": ("диспетчер задач", "диспетчер", "task manager"),
    "калькулятор": ("калькулятор", "калькулятор", "calculator", "калк"),
    "ворд": ("ворд", "word", "вордовский"),
    "эксель": ("эксель", "excel", "ексель"),
    "вс код": ("вс код", "vs code", "вскод", "визуал студио код", "vscode"),
    "спотифай": ("спотифай", "spotify", "спотик"),
    "вайбер": ("вайбер", "viber"),
    "эпик": ("эпик", "epic"),
    "обс": ("обс", "obs"),
    "настройки": ("настройки", "параметры"),
    "браузер": ("браузер", "browser"),
}
BY_KEY = {t.key: t for t in TARGETS}


def resolve_alias(spoken: str) -> Optional[Target]:
    s = spoken.strip().lower().replace("ё", "е")
    if not s:
        return None
    for key, variants in ALIASES.items():          # ordered: "ютуб музыка" before "ютуб"
        for v in variants:
            if len(v) <= 3:                        # short ones (тг, дс, обс) only as whole words
                if re.search(rf"(?:^|\s){re.escape(v)}(?:\s|$)", s):
                    return BY_KEY[key]
            elif s == v or re.search(rf"(?:^|\s){re.escape(v)}", s):
                return BY_KEY[key]
    import difflib
    flat = {v: k for k, vs in ALIASES.items() for v in vs if len(v) >= 4}
    hit = difflib.get_close_matches(s.split()[0] if s.split() else s, list(flat), n=1, cutoff=0.78)
    return BY_KEY[flat[hit[0]]] if hit else None


# ------------------------------------------------------------------------------------------------ manager
class WindowManager:
    def __init__(self, backend: Optional[Backend], own_pids: Optional[set] = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 verify_s: float = 1.5):
        self.b = backend
        self.refresh_own = own_pids is None
        self.own = set(own_pids if own_pids is not None else _own_pids())
        self.sleep, self.clock, self.verify_s = sleep, clock, verify_s
        self.last_fg: Optional[Window] = None        # last foreground window that is not Jarvis / shell

    # ---- protection
    def protected(self, w: Window) -> bool:
        if w.pid in self.own:
            return True
        if w.cls in SHELL_CLASSES:
            return True
        if w.exe == "explorer.exe":
            return w.cls not in EXPLORER_FOLDER_CLASSES
        if w.exe in SYSTEM_EXES:
            return True
        return False

    def candidates(self) -> list[Window]:
        if self.b is None:
            return []
        if self.refresh_own:
            self.own |= _own_pids()                 # the face's QtWebEngine child starts after us
        return [w for w in self.b.windows() if not self.protected(w)]

    # ---- foreground tracking (polled by the skill's thread)
    def track(self) -> None:
        if self.b is None:
            return
        fg = self.b.foreground()
        if not fg or (self.last_fg is not None and self.last_fg.hwnd == fg):
            return
        for w in self.b.windows():
            if w.hwnd == fg:
                if not self.protected(w):
                    self.last_fg = w
                return

    def active(self) -> Optional[Window]:
        """Window meant by «закрой это/окно»: the foreground one unless it is Jarvis, else the previous one."""
        wins = self.candidates()
        fg = self.b.foreground() if self.b else 0
        for w in wins:
            if w.hwnd == fg:
                return w
        if self.last_fg is not None:
            for w in wins:
                if w.hwnd == self.last_fg.hwnd:
                    return w
        return None

    # ---- matching
    def find(self, target: Target) -> list[Window]:
        """Matching windows in Z-order (topmost first). PWA windows before browser windows with that tab."""
        out = []
        for w in self.candidates():
            t = w.title.lower()
            if target.exclude_re and re.search(target.exclude_re, t):
                continue
            by_title = bool(target.title_re and re.search(target.title_re, t))
            by_exe = bool(target.exes and w.exe in target.exes)
            if target.key in ("ютуб", "ютуб музыка"):
                ok = by_title                               # PWA or tab: identified by the title only
            elif target.exes:
                ok = by_exe and (by_title or not target.title_re or target.key in ("стим", "браузер", "проводник"))
            else:
                ok = by_title
            if ok:
                out.append(w)
        out.sort(key=lambda w: 1 if w.is_browser_tab_window and target.key in ("ютуб", "ютуб музыка") else 0)
        return out

    def find_by_name(self, name: str) -> list[Window]:
        """Unknown app: the spoken word against exe names and window titles (fuzzy)."""
        import difflib
        from .wake import translit
        q = name.strip().lower()
        if len(q) < 3:
            return []
        qs = {q, translit(q)}
        out = []
        for w in self.candidates():
            base = w.exe.rsplit(".", 1)[0]
            words = re.findall(r"[\wа-яё]+", w.title.lower())
            if any(x and (x in base or x in w.title.lower()) for x in qs) or \
                    difflib.get_close_matches(translit(q), [base] + [translit(x) for x in words], n=1, cutoff=0.8):
                out.append(w)
        return out

    # ---- actions (each verifies the result)
    def _gone(self, hwnd: int, title_before: str, tab: bool = False) -> bool:
        assert self.b is not None
        deadline = self.clock() + self.verify_s
        while True:
            if not self.b.alive(hwnd):
                return True
            if tab and self.b.title(hwnd) and self.b.title(hwnd) != title_before:
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(0.15)

    def close(self, w: Window) -> str:
        """-> 'closed' | 'tab_closed' | 'dialog' | 'failed' | 'protected'"""
        if self.b is None:
            return "failed"
        if self.protected(w):
            return "protected"
        if w.is_browser_tab_window and re.search(r"youtube", w.title, re.I):
            if self.b.ctrl_w(w.hwnd) and self._gone(w.hwnd, w.title, tab=True):
                return "tab_closed"
            return "failed"
        self.b.post_close(w.hwnd)
        if self._gone(w.hwnd, w.title):
            return "closed"
        if not self.b.enabled(w.hwnd):          # a modal "save changes?" dialog took over
            return "dialog"
        if not w.is_browser and w.exe and w.exe not in SYSTEM_EXES:
            hung = self.b.hung(w.hwnd)
            log.info("WM_CLOSE ignored by %s (%s), taskkill /PID %s%s", w.exe, w.title, w.pid, " /F" if hung else "")
            self.b.kill(w.pid, force=hung)
            if self._gone(w.hwnd, w.title):
                return "closed"
        return "failed"

    def show(self, w: Window, cmd: str) -> bool:
        if self.b is None or self.protected(w):
            return False
        return self.b.show(w.hwnd, cmd)


OWN_EXES = {"python.exe", "pythonw.exe", "python3.exe", "qtwebengineprocess.exe", "jarvis.exe", "jarvisw.exe"}


def _own_pids() -> set:
    """Jarvis itself, its watchdog and its own helpers (face WebEngine). Programs Jarvis *launched*
    (notepad, games…) are children too, but they belong to the user and must stay closable,
    and a parent like explorer.exe (started from the Start menu) must not protect folder windows."""
    pids = {os.getpid()}
    try:
        import psutil
        me = psutil.Process()

        def ours(p) -> bool:
            try:
                return p.name().lower() in OWN_EXES
            except Exception:
                return False
        pids |= {p.pid for p in me.parents()[:2] if ours(p)}             # watchdog / launcher
        pids |= {p.pid for p in me.children(recursive=True) if ours(p)}  # QtWebEngineProcess (face)
    except Exception:
        pass
    return pids
