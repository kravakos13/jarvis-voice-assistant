"""Small OS helpers (Windows first, Linux/macOS fallbacks for testing)."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)
IS_WIN = sys.platform == "win32"


# ---------------------------------------------------------------- folders
def documents_dir() -> Path:
    """Real 'Documents' folder (respects OneDrive redirection on Windows)."""
    if IS_WIN:
        try:
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            # CSIDL_PERSONAL = 5
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
                return Path(buf.value)
        except Exception:
            pass
    return Path.home() / "Documents"


def data_dir(sub: str, override: str = "") -> Path:
    base = Path(os.environ["JARVIS_DATA_DIR"]) if os.environ.get("JARVIS_DATA_DIR") else documents_dir() / "Jarvis"
    path = Path(os.path.expandvars(override)).expanduser() if override else base / sub
    path.mkdir(parents=True, exist_ok=True)
    return path


def slugify(text: str, max_len: int = 40) -> str:
    table = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
        "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya"})
    s = text.lower().translate(table)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return (s[:max_len].strip("_") or "project")


# ---------------------------------------------------------------- opening things
def open_path(path: str | Path) -> bool:
    """Open a file/folder with its default application."""
    try:
        if IS_WIN:
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            log.info("Нечем открыть %s", path)
            return False
        return True
    except Exception as e:
        log.error("Не удалось открыть %s: %s", path, e)
        return False


def open_in_notepad(path: str | Path) -> bool:
    if IS_WIN:
        try:
            subprocess.Popen(["notepad.exe", str(path)])
            return True
        except Exception as e:
            log.error("notepad: %s", e)
            return False
    return open_path(path)


def spawn(argv: list[str]) -> subprocess.Popen:
    """Start a program detached, without a shell."""
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if IS_WIN else 0
    return subprocess.Popen(argv, shell=False, close_fds=True, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=flags)


# ---------------------------------------------------------------- media keys
VK = {
    "play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
    "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD,
}


def press_media_key(action: str, times: int = 1) -> bool:
    vk = VK.get(action)
    if vk is None:
        return False
    if IS_WIN:
        import ctypes
        KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2
        for _ in range(max(1, times)):
            ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
            ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
        return True
    # Linux fallback for testing: playerctl / xdotool if present
    linux = {"play_pause": ["playerctl", "play-pause"], "next": ["playerctl", "next"],
             "previous": ["playerctl", "previous"], "stop": ["playerctl", "stop"]}
    if action in linux and shutil.which(linux[action][0]):
        subprocess.run(linux[action], check=False)
        return True
    log.info("Медиаклавиши поддерживаются только в Windows (%s)", action)
    return False


# ---------------------------------------------------------------- notifications
def notify(title: str, message: str, app_id: str = "Jarvis") -> bool:
    """Windows toast (winotify) with plyer fallback. Never raises."""
    if IS_WIN:
        try:
            from winotify import Notification, audio
            toast = Notification(app_id=app_id, title=title, msg=message[:250])
            toast.set_audio(audio.Reminder, loop=False)
            toast.show()
            return True
        except Exception as e:
            log.debug("winotify: %s", e)
    try:
        if not IS_WIN:
            raise RuntimeError("plyer only used on Windows")   # its Linux backends print tracebacks
        import warnings
        from plyer import notification
        with warnings.catch_warnings():       # plyer warns (instead of raising) when no backend exists
            warnings.simplefilter("error")
            notification.notify(title=title, message=message[:250], app_name=app_id, timeout=10)
        return True
    except Exception as e:
        log.debug("plyer: %s", e)
    log.info("[уведомление] %s: %s", title, message)
    return False


# ---------------------------------------------------------------- Chrome
def find_chrome(override: str = "") -> Optional[str]:
    if override and Path(os.path.expandvars(override)).exists():
        return os.path.expandvars(override)
    candidates = []
    if IS_WIN:
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for c in candidates:
        if c.exists():
            return str(c)
    for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def chrome_apps_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Chrome Apps"


_APP_ID_RE = re.compile(r"--app-id=([a-p]{32})")
_PROFILE_RE = re.compile(r'--profile-directory=("?)([^"\s]+(?: \d+)?)\1')


def parse_lnk_chrome_args(path: str | Path) -> dict:
    """Extract --app-id / --profile-directory from a Chrome app .lnk without pywin32.
    LNK string data is UTF-16LE, so decode that way and regex the arguments."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return {}
    out = {}
    for text in (raw.decode("utf-16-le", errors="ignore"), raw.decode("latin-1", errors="ignore")):
        m = _APP_ID_RE.search(text)
        if m and "app_id" not in out:
            out["app_id"] = m.group(1)
        p = _PROFILE_RE.search(text)
        if p and "profile" not in out:
            out["profile"] = p.group(2)
    return out


# ---------------------------------------------------------------- autostart
def startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


AUTOSTART_NAME = "Jarvis Assistant.vbs"       # legacy (<=0.9) shell:startup launcher, removed on change
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Jarvis"                           # the installer's autostart checkbox writes the same value


def autostart_command(project_root: Path) -> str:
    """wscript Jarvis.vbs --minimized: sets the working dir and starts the watchdog without a console."""
    windir = os.environ.get("WINDIR", r"C:\Windows")
    return f'"{windir}\\System32\\wscript.exe" "{Path(project_root) / "Jarvis.vbs"}" --minimized'


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:  # type: ignore[attr-defined]
            winreg.QueryValueEx(k, RUN_VALUE)  # type: ignore[attr-defined]
        return True
    except OSError:
        return (startup_dir() / AUTOSTART_NAME).exists()


def set_autostart(enabled: bool, project_root: Path) -> bool:
    """HKCU\\...\\Run\\Jarvis -> wscript Jarvis.vbs --minimized (tray, no console, via watchdog)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        legacy = startup_dir() / AUTOSTART_NAME
        if legacy.exists():
            legacy.unlink()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:  # type: ignore[attr-defined]
            if enabled:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, autostart_command(project_root))  # type: ignore[attr-defined]
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)  # type: ignore[attr-defined]
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        log.error("Автозапуск: %s", e)
        return False
