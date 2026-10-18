"""Keyboard output into the focused window (1.0.8).

* text: SendInput + KEYEVENTF_UNICODE (wVk=0, wScan=UTF-16 unit): layout-independent, Cyrillic/emoji-safe;
* keys: hardware SCAN CODES (KEYEVENTF_SCANCODE, +EXTENDED for arrows/Home/End/PgUp/PgDn/Ins/Del/Win), so Enter,
  digits, arrows, F-keys, Esc… do not depend on the keyboard layout; letters go as virtual keys (Ctrl+C is Ctrl+C
  in the Russian layout too, which is what applications check);
* the target is the user's window, never Jarvis: focus_target() restores the window that was in front before
  Jarvis and VERIFIES it is the foreground window before anything is sent.
All functions are best-effort and return False instead of raising.
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Optional

log = logging.getLogger(__name__)

KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x0001, 0x0002, 0x0004, 0x0008
KEYEVENTF_EXTENDED = KEYEVENTF_EXTENDEDKEY

# name -> (virtual key, scan code or None = send as VK, extended)
KEYS: dict[str, tuple[int, Optional[int], bool]] = {
    "enter": (0x0D, 0x1C, False), "esc": (0x1B, 0x01, False), "tab": (0x09, 0x0F, False),
    "space": (0x20, 0x39, False), "backspace": (0x08, 0x0E, False), "delete": (0x2E, 0x53, True),
    "insert": (0x2D, 0x52, True), "up": (0x26, 0x48, True), "down": (0x28, 0x50, True),
    "left": (0x25, 0x4B, True), "right": (0x27, 0x4D, True), "home": (0x24, 0x47, True), "end": (0x23, 0x4F, True),
    "pageup": (0x21, 0x49, True), "pagedown": (0x22, 0x51, True), "ctrl": (0x11, 0x1D, False),
    "shift": (0x10, 0x2A, False), "alt": (0x12, 0x38, False), "win": (0x5B, 0x5B, True),
    "printscreen": (0x2C, 0x37, True), "capslock": (0x14, 0x3A, False), "menu": (0x5D, 0x5D, True),
    "plus": (0xBB, 0x0D, False), "minus": (0xBD, 0x0C, False),
    "browser_back": (0xA6, None, True), "browser_forward": (0xA7, None, True),
}
_F_SCANS = [0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40, 0x41, 0x42, 0x43, 0x44, 0x57, 0x58]
for _i in range(12):
    KEYS[f"f{_i + 1}"] = (0x70 + _i, _F_SCANS[_i], False)
_D_SCANS = [0x0B, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A]
for _d in range(10):
    KEYS[str(_d)] = (0x30 + _d, _D_SCANS[_d], False)
for _c in "abcdefghijklmnopqrstuvwxyz":
    KEYS[_c] = (ord(_c.upper()), None, False)
ALIASES = {"return": "enter", "escape": "esc", "del": "delete", "ins": "insert", "pgup": "pageup", "pgdn": "pagedown",
           "control": "ctrl", "windows": "win", "arrowup": "up", "arrowdown": "down", "spacebar": "space"}
MODIFIERS = ("ctrl", "shift", "alt", "win")
VK = {k: v[0] for k, v in KEYS.items()}          # backwards compatible view


def key_events(name: str, up: bool) -> Optional[tuple]:
    """(wVk, wScan, flags) for one key transition."""
    name = ALIASES.get(name, name)
    spec = KEYS.get(name)
    if spec is None:
        return None
    vk, scan, ext = spec
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if ext else 0)
    if scan is not None:
        return (0, scan, flags | KEYEVENTF_SCANCODE)
    return (vk, _vk_scan(vk), flags)


def _vk_scan(vk: int) -> int:
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        return int(ctypes.windll.user32.MapVirtualKeyW(vk, 0)) & 0xFF
    except Exception:                                    # noqa: BLE001
        return 0


# ------------------------------------------------------------------------------------ SendInput (exact structs)
_INPUT_T = None


def _input_type():
    global _INPUT_T
    if _INPUT_T is not None:
        return _INPUT_T
    import ctypes
    from ctypes import wintypes
    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    _INPUT_T = (INPUT, KEYBDINPUT)
    return _INPUT_T


def _send(events: list) -> bool:
    if sys.platform != "win32" or not events:
        return False
    import ctypes
    INPUT, KEYBDINPUT = _input_type()
    arr = (INPUT * len(events))()
    for i, (vk, scan, flags) in enumerate(events):
        arr[i].type = 1                                   # INPUT_KEYBOARD
        arr[i].ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    u = ctypes.windll.user32
    u.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
    u.SendInput.restype = ctypes.c_uint
    sent = u.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    if sent != len(events):
        log.warning("SendInput: отправлено %d из %d (окно с правами администратора? UIPI)", sent, len(events))
    return sent == len(events)


def unicode_events(text: str) -> list:
    """Key down/up pairs for every UTF-16 code unit; '\\n' becomes a real Enter (chat apps, editors)."""
    ev: list = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            ev += [key_events("enter", False), key_events("enter", True)]
            continue
        data = ch.encode("utf-16-le")
        for j in range(0, len(data), 2):
            unit = int.from_bytes(data[j:j + 2], "little")
            ev += [(0, unit, KEYEVENTF_UNICODE), (0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    return ev


def type_text(text: str) -> bool:
    if not text:
        return True
    try:
        ev = unicode_events(text)
        for i in range(0, len(ev), 64):                  # small bursts: some apps drop very long ones
            if not _send(ev[i:i + 64]):
                return False
            time.sleep(0.008)
        return True
    except Exception as e:                               # noqa: BLE001
        log.debug("SendInput: %s", e)
        return False


def parse_combo(combo: str) -> Optional[list[str]]:
    keys = [ALIASES.get(k, k) for k in (k.strip().lower() for k in combo.replace(" ", "").split("+")) if k]
    if not keys or any(k not in KEYS for k in keys):
        return None
    return keys


def combo_events(combo: str) -> Optional[list]:
    keys = parse_combo(combo)
    if keys is None:
        return None
    down = [key_events(k, False) for k in keys]
    up = [key_events(k, True) for k in reversed(keys)]
    return down + up


def press(combo: str, times: int = 1) -> bool:
    """«ctrl+backspace», «enter», «ctrl+shift+t», «alt+left», «f5», «win+d» … pressed `times` times."""
    ev = combo_events(combo)
    if ev is None:
        log.warning("Неизвестная комбинация: %s", combo)
        return False
    try:
        for i in range(max(1, min(int(times), 50))):
            if not _send(ev):
                return False
            if i:
                time.sleep(0.03)
        return True
    except Exception as e:                               # noqa: BLE001
        log.debug("SendInput %s: %s", combo, e)
        return False


def paste(text: str, restore_after: float = 0.6) -> bool:
    """Clipboard + Ctrl+V (one atomic insert); the previous clipboard text is put back afterwards."""
    try:
        import pyperclip
    except Exception as e:                               # noqa: BLE001
        log.debug("pyperclip: %s", e)
        return False
    try:
        old = pyperclip.paste()
    except Exception:                                    # noqa: BLE001
        old = None
    try:
        pyperclip.copy(text)
        time.sleep(0.05)
        ok = press("ctrl+v")
        time.sleep(restore_after)
        return ok
    except Exception as e:                               # noqa: BLE001
        log.debug("paste: %s", e)
        return False
    finally:
        if old is not None:
            try:
                pyperclip.copy(old)
            except Exception:                            # noqa: BLE001
                pass


def focus_target(wm, settle_s: float = 0.15) -> Optional[int]:
    """Make sure keystrokes go to the user's window, never to Jarvis: if Jarvis (or nothing) is in the foreground,
    bring back the window that was focused before it and check that it really became the foreground window.
    Returns the target hwnd, or None (unknown / could not focus) -> the caller must NOT send anything."""
    if wm is None or getattr(wm, "b", None) is None:
        return None
    try:
        w = wm.active()                                  # foreground unless it is Jarvis -> previous one
        if w is None:
            return None
        if wm.b.foreground() == w.hwnd:
            return w.hwnd
        wm.b.activate(w.hwnd)
        for _ in range(10):
            time.sleep(settle_s / 3)
            if wm.b.foreground() == w.hwnd:
                time.sleep(settle_s)                     # let the app restore its keyboard focus (edit control)
                return w.hwnd
        log.warning("Не удалось вывести окно «%s» на передний план — клавиши не отправляю", w.title[:60])
        return None
    except Exception as e:                               # noqa: BLE001
        log.debug("focus: %s", e)
        return None
