"""Keyboard output into the focused window: SendInput with KEYEVENTF_UNICODE (Cyrillic-safe, independent of the
keyboard layout) and simple key combos. Everything is best-effort and returns False instead of raising."""
from __future__ import annotations

import logging
import sys
import time
from typing import Optional

log = logging.getLogger(__name__)

KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_EXTENDED = 0x0002, 0x0004, 0x0001
VK = {"enter": 0x0D, "backspace": 0x08, "tab": 0x09, "esc": 0x1B, "space": 0x20, "ctrl": 0x11, "shift": 0x10,
      "alt": 0x12, "win": 0x5B, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "home": 0x24, "end": 0x23,
      "pageup": 0x21, "pagedown": 0x22, "delete": 0x2E, "f5": 0x74, "f11": 0x7A, "plus": 0xBB, "minus": 0xBD,
      "0": 0x30, "printscreen": 0x2C}
EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x24, 0x23, 0x21, 0x22, 0x2E, 0x5B}
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = ord(_c.upper())
for _d in "123456789":
    VK[_d] = ord(_d)


def _send(events: list) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    arr = (INPUT * len(events))()
    for i, (vk, scan, flags) in enumerate(events):
        arr[i].type = 1                                   # INPUT_KEYBOARD
        arr[i].u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    sent = ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    return sent == len(events)


def unicode_events(text: str) -> list:
    """Key down/up pairs for every UTF-16 code unit; '\\n' becomes a real Enter (chat apps, editors)."""
    ev: list = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            ev += [(VK["enter"], 0, 0), (VK["enter"], 0, KEYEVENTF_KEYUP)]
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
        for i in range(0, len(ev), 200):                 # chunks: some apps drop very long bursts
            if not _send(ev[i:i + 200]):
                return False
            time.sleep(0.01)
        return True
    except Exception as e:                               # noqa: BLE001
        log.debug("SendInput: %s", e)
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


def combo_events(combo: str) -> Optional[list]:
    keys = [k.strip().lower() for k in combo.replace(" ", "").split("+") if k.strip()]
    vks = [VK.get(k) for k in keys]
    if not keys or any(v is None for v in vks):
        return None
    down = [(v, 0, KEYEVENTF_EXTENDED if v in EXTENDED else 0) for v in vks]
    up = [(v, 0, (KEYEVENTF_EXTENDED if v in EXTENDED else 0) | KEYEVENTF_KEYUP) for v in reversed(vks)]
    return down + up


def press(combo: str) -> bool:
    """«ctrl+backspace», «enter», «ctrl+shift+t», «alt+left» ..."""
    ev = combo_events(combo)
    if ev is None:
        log.warning("Неизвестная комбинация: %s", combo)
        return False
    try:
        return _send(ev)
    except Exception as e:                               # noqa: BLE001
        log.debug("SendInput %s: %s", combo, e)
        return False


def focus_target(wm) -> Optional[int]:
    """Make sure keystrokes go to the user's window, never to Jarvis: if Jarvis (or nothing) is in the foreground,
    re-activate the window that was focused before it. Returns the target hwnd (None = unknown)."""
    if wm is None or getattr(wm, "b", None) is None:
        return None
    try:
        w = wm.active()                                  # foreground unless it is Jarvis -> previous one
        if w is None:
            return None
        if wm.b.foreground() != w.hwnd:
            wm.b.activate(w.hwnd)
            time.sleep(0.15)
        return w.hwnd
    except Exception as e:                               # noqa: BLE001
        log.debug("focus: %s", e)
        return None
