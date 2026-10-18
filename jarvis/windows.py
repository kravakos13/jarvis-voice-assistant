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
# 1.0.9 UWP / Store apps: the visible top-level is an ApplicationFrameWindow of ApplicationFrameHost.exe; the app
# itself (CalculatorApp.exe…) owns a child Windows.UI.Core.CoreWindow. Close the FRAME; never kill the host (it
# hosts every Store app); taskkill only apps without unsaved state.
UWP_FRAME = "ApplicationFrameWindow"
UWP_HOST = "applicationframehost.exe"
NEVER_KILL = {UWP_HOST, "explorer.exe", "dwm.exe", "csrss.exe", "winlogon.exe", "svchost.exe", "lsass.exe",
              "sihost.exe", "ctfmon.exe", "runtimebroker.exe", "searchhost.exe", "startmenuexperiencehost.exe",
              "shellexperiencehost.exe", "textinputhost.exe", "systemsettings.exe"}
UWP_SAFE_KILL = {"calculatorapp.exe", "calculator.exe", "calc.exe"}


@dataclass
class Window:
    hwnd: int
    title: str
    pid: int
    exe: str = ""        # lower-case base name, e.g. "chrome.exe"
    cls: str = ""
    host_pid: int = 0    # UWP: ApplicationFrameHost pid when pid/exe were resolved from the child CoreWindow

    @property
    def is_uwp_frame(self) -> bool:
        return self.cls == UWP_FRAME

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


    def sys_close(self, hwnd: int) -> bool:           # WM_SYSCOMMAND SC_CLOSE (UWP frames)
        return False

    def uia_close(self, hwnd: int) -> bool:           # UI Automation WindowPattern.Close
        return False

    # 1.0.9 game guard / dialog detection (fakes may omit them)
    def exe_path(self, pid: int) -> str:              # full image path, '' if unknown
        return ""

    def fullscreen(self, hwnd: int) -> tuple:         # (covers its whole monitor, has no caption = borderless)
        return (False, False)

    def has_popup(self, hwnd: int) -> bool:           # an owned modal/popup («Сохранить изменения?») is up
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
                p, cls = int(pid.value), self._cls(h)
                exe, host = self._exe(p), 0
                if cls == UWP_FRAME:
                    rp = self._uwp_app_pid(h, p)
                    if rp:
                        p, host, exe = rp, p, self._exe(rp)
                out.append(Window(h, title, p, exe, cls, host))
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

    def _uwp_app_pid(self, frame: int, host_pid: int) -> int:
        """PID of the Store app inside a frame: its child Windows.UI.Core.CoreWindow (0 if suspended/minimized)."""
        ct, wt, u = self.ct, self.wt, self.u
        found = [0]
        proc = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def cb(child, _):
            try:
                if self._cls(int(child or 0)) == "Windows.UI.Core.CoreWindow":
                    pid = wt.DWORD(0)
                    u.GetWindowThreadProcessId(child, ct.byref(pid))
                    if int(pid.value) and int(pid.value) != host_pid:
                        found[0] = int(pid.value)
                        return False
            except Exception:                                      # noqa: BLE001
                pass
            return True
        try:
            u.EnumChildWindows(wt.HWND(frame), proc(cb), 0)
        except Exception as e:                                     # noqa: BLE001
            log.debug("uwp child: %s", e)
        return found[0]

    def sys_close(self, hwnd: int) -> bool:
        return bool(self.u.PostMessageW(hwnd, 0x0112, 0xF060, 0))                    # WM_SYSCOMMAND, SC_CLOSE

    def uia_close(self, hwnd: int) -> bool:
        from . import uia
        return uia.close_window(hwnd)

    def exe_path(self, pid: int) -> str:
        try:
            import psutil
            return str(psutil.Process(pid).exe() or "")
        except Exception:
            pass
        try:
            k = self.k
            k.OpenProcess.restype = self.wt.HANDLE
            h = k.OpenProcess(0x1000, False, pid)                                    # QUERY_LIMITED_INFORMATION
            if h:
                buf = self.ct.create_unicode_buffer(1024)
                size = self.wt.DWORD(1024)
                ok = k.QueryFullProcessImageNameW(self.wt.HANDLE(h), 0, buf, self.ct.byref(size))
                k.CloseHandle(self.wt.HANDLE(h))
                return buf.value if ok else ""
        except Exception as e:
            log.debug("exe_path: %s", e)
        return ""

    def fullscreen(self, hwnd: int) -> tuple:
        ct, wt, u = self.ct, self.wt, self.u
        try:
            class MONITORINFO(ct.Structure):  # type: ignore[name-defined]
                _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]
            r = wt.RECT()
            if not u.GetWindowRect(wt.HWND(hwnd), ct.byref(r)):
                return (False, False)
            u.MonitorFromWindow.restype = wt.HANDLE
            mon = u.MonitorFromWindow(wt.HWND(hwnd), 2)                              # MONITOR_DEFAULTTONEAREST
            mi = MONITORINFO()
            mi.cbSize = ct.sizeof(MONITORINFO)
            if not u.GetMonitorInfoW(wt.HANDLE(mon), ct.byref(mi)):
                return (False, False)
            m = mi.rcMonitor
            covers = r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom
            style = int(u.GetWindowLongW(hwnd, -16)) & 0xFFFFFFFF                     # GWL_STYLE
            return (bool(covers), (style & 0x00C00000) != 0x00C00000)                # WS_CAPTION
        except Exception as e:
            log.debug("fullscreen: %s", e)
            return (False, False)

    def has_popup(self, hwnd: int) -> bool:
        try:
            u = self.u
            u.GetLastActivePopup.restype = self.wt.HWND
            p = int(u.GetLastActivePopup(self.wt.HWND(hwnd)) or 0)
            return bool(p and p != hwnd and u.IsWindowVisible(p))
        except Exception:
            return False

    def show(self, hwnd: int, cmd: str) -> bool:
        sw = {"minimize": 6, "maximize": 3, "restore": 9}[cmd]
        self.u.ShowWindow(hwnd, sw)
        if cmd != "minimize":
            self.activate(hwnd)
        return True

    def activate(self, hwnd: int) -> bool:
        """Bring a window to the front despite the foreground lock: attach our input queue to the current
        foreground thread and the target's thread (AttachThreadInput), then SetForegroundWindow/BringWindowToTop.
        No Alt-tap trick: an injected Alt can open the target app's menu bar, and typed text would go there.
        Last resort: minimize + restore (the restored window is activated by the system)."""
        u, k, wt = self.u, self.k, self.wt
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)                        # SW_RESTORE
        fg = int(u.GetForegroundWindow() or 0)
        if fg == hwnd:
            return True
        cur = int(k.GetCurrentThreadId())
        threads = set()
        for h in (fg, hwnd):
            if h:
                threads.add(int(u.GetWindowThreadProcessId(wt.HWND(h), None)))
        attached = []
        try:
            for t in threads - {cur, 0}:
                if u.AttachThreadInput(cur, t, True):
                    attached.append(t)
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)              # activation restores the app's own focused control
        finally:
            for t in attached:
                u.AttachThreadInput(cur, t, False)
        if int(u.GetForegroundWindow() or 0) == hwnd:
            return True
        u.ShowWindow(hwnd, 6)                            # SW_MINIMIZE
        u.ShowWindow(hwnd, 9)                            # SW_RESTORE -> activated
        u.SetForegroundWindow(hwnd)
        return int(u.GetForegroundWindow() or 0) == hwnd

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
    Target("калькулятор", "калькулятор", r"калькулятор|calculator", ("calculatorapp.exe", "calculator.exe", "calc.exe")),
    Target("фото", "фотографии", r"^(?:фотографии|photos|фото)\b|- (?:фотографии|photos)$", ("photos.exe", "microsoft.photos.exe")),
    Target("медиаплеер", "медиаплеер", r"медиаплеер|media player|кино и тв|films & tv", ("microsoft.media.player.exe", "video.ui.exe")),
    Target("магазин", "Microsoft Store", r"microsoft store", ("winstore.app.exe",)),
    Target("терминал", "терминал", r"windows powershell|командная строка|terminal|powershell$", ("windowsterminal.exe",)),
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
    "медиаплеер": ("медиаплеер", "медиа плеер", "media player", "кино и тв"),
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
    "калькулятор": ("калькулятор", "calculator", "калк", "calc", "кальк", "калькулятр", "колькулятор"),
    "фото": ("фотографии", "фотки", "photos", "просмотр фото"),
    "магазин": ("майкрософт стор", "microsoft store", "магазин приложений", "магазин"),
    "терминал": ("терминал", "terminal", "командную строку", "командная строка", "powershell", "павершелл"),
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
        self.prev_fg: Optional[Window] = None        # the user's window before last_fg
        # 1.0.9: «закрой» = the window that was in front when the user STARTED speaking
        self._start: Optional[tuple] = None          # (fg hwnd, last_fg hwnd) at speech start (cheap, mic thread)
        self.cmd_fg: Optional[tuple] = None          # frozen for the utterance being processed
        self.cmd_at = 0.0

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
                    if self.last_fg is not None and self.last_fg.hwnd != w.hwnd:
                        self.prev_fg = self.last_fg          # «вернись» = the window before this one
                    self.last_fg = w
                return

    # ---- utterance snapshot (1.0.9)
    def mark_start(self) -> None:
        """Speech started (mic thread): remember what was in front. Only two ints, no enumeration."""
        if self.b is None:
            return
        try:
            self._start = (int(self.b.foreground() or 0), self.last_fg.hwnd if self.last_fg else 0)
        except Exception:                                   # noqa: BLE001
            self._start = None

    def commit_utterance(self) -> None:
        """The utterance whose start was marked is now being handled: freeze its snapshot."""
        self.cmd_fg, self.cmd_at, self._start = self._start, self.clock(), None

    def clear_utterance(self) -> None:
        self.cmd_fg = None

    def close_target(self, max_age_s: float = 20.0) -> Optional[Window]:
        """Window for a bare «закрой»: the foreground one at speech start; Jarvis/shell in front -> the user's
        window before it. No (fresh) snapshot -> active()."""
        snap = self.cmd_fg if self.cmd_fg and self.clock() - self.cmd_at <= max_age_s else None
        if snap is None:
            return self.active()
        fg, last = snap
        if not fg:
            return self.active()
        wins = {w.hwnd: w for w in self.candidates()}
        if fg in wins:
            return wins[fg]
        front = next((w for w in self.b.windows() if w.hwnd == fg), None) if self.b is not None else None
        if front is not None and front.pid in self.own:     # Jarvis was in front -> the user's window before it
            return wins.get(last)
        return None                                         # desktop / taskbar / system window, or already gone

    def previous(self) -> Optional[Window]:
        """The user's window before the current one (still open, not Jarvis)."""
        if self.prev_fg is None:
            return None
        cur = self.active()
        for w in self.candidates():
            if w.hwnd == self.prev_fg.hwnd and (cur is None or w.hwnd != cur.hwnd):
                return w
        return None

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
                ok = ok or (w.is_uwp_frame and by_title)            # Store app (exe unresolved when minimized)
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

    def close(self, w: Window, whole: bool = False, kill: bool = True) -> str:
        """-> 'closed' | 'tab_closed' | 'dialog' | 'failed' | 'protected'.
        whole: the whole window even if it is a browser with a YouTube tab (bare «закрой»).
        kill: allow a graceful taskkill when WM_CLOSE is ignored (never for games; never after a dialog)."""
        if self.b is None:
            return "failed"
        if self.protected(w):
            return "protected"
        if not whole and w.is_browser_tab_window and re.search(r"youtube", w.title, re.I):
            if self.b.ctrl_w(w.hwnd) and self._gone(w.hwnd, w.title, tab=True):
                return "tab_closed"
            return "failed"
        self.b.post_close(w.hwnd)
        if self._gone(w.hwnd, w.title):
            return "closed"
        if not self.b.enabled(w.hwnd) or self.b.has_popup(w.hwnd):   # a "save changes?" dialog took over
            return "dialog"
        if w.is_uwp_frame:                                            # Store app: SC_CLOSE, then UIA Close
            for step in (self.b.sys_close, self.b.uia_close):
                try:
                    step(w.hwnd)
                except Exception as e:                                # noqa: BLE001
                    log.debug("uwp close: %s", e)
                if self._gone(w.hwnd, w.title):
                    return "closed"
            if kill and w.exe in UWP_SAFE_KILL and w.host_pid and w.pid != w.host_pid:
                log.info("Store app %s ignores close, taskkill /PID %s", w.exe, w.pid)
                self.b.kill(w.pid, force=True)                        # calculator: nothing to lose
                if self._gone(w.hwnd, w.title):
                    return "closed"
            return "failed"
        if kill and not w.is_browser and w.exe and w.exe not in SYSTEM_EXES and w.exe not in NEVER_KILL:
            hung = self.b.hung(w.hwnd)
            log.info("WM_CLOSE ignored by %s (%s), taskkill /PID %s%s", w.exe, w.title, w.pid, " /F" if hung else "")
            self.b.kill(w.pid, force=hung)
            if self._gone(w.hwnd, w.title):
                return "closed"
            if self.b.alive(w.hwnd) and (not self.b.enabled(w.hwnd) or self.b.has_popup(w.hwnd)):
                return "dialog"
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
