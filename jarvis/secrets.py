"""Secret storage (never in YAML, never logged).

Windows: Windows Credential Manager via `keyring` (DPAPI-protected, per user);
fallback: DPAPI-encrypted file via win32crypt. Other OS: keyring if a backend exists,
else a 0600 file in the data dir (for testing only)."""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Optional

from . import platform_utils as pu

log = logging.getLogger(__name__)
SERVICE = "Jarvis-Assistant"


def _file(name: str) -> Path:
    return pu.data_dir("secrets") / f"{name}.bin"


def set_secret(name: str, value: str) -> None:
    value = value or ""
    try:
        import keyring
        if value:
            keyring.set_password(SERVICE, name, value)
        else:
            try:
                keyring.delete_password(SERVICE, name)
            except Exception:
                pass
        _file(name).unlink(missing_ok=True)
        return
    except Exception as e:
        log.debug("keyring недоступен (%s), использую файл", type(e).__name__)
    f = _file(name)
    if not value:
        f.unlink(missing_ok=True)
        return
    data = value.encode("utf-8")
    if pu.IS_WIN:
        import win32crypt
        data = b"DPAPI" + win32crypt.CryptProtectData(data, "Jarvis", None, None, None, 0)
    else:
        data = b"PLAIN" + base64.b64encode(data)
    f.write_bytes(data)
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass


def get_secret(name: str) -> str:
    try:
        import keyring
        v = keyring.get_password(SERVICE, name)
        if v:
            return v
    except Exception:
        pass
    f = _file(name)
    if not f.exists():
        return ""
    raw = f.read_bytes()
    try:
        if raw.startswith(b"DPAPI"):
            import win32crypt
            return win32crypt.CryptUnprotectData(raw[5:], None, None, None, 0)[1].decode("utf-8")
        if raw.startswith(b"PLAIN"):
            return base64.b64decode(raw[5:]).decode("utf-8")
    except Exception as e:
        log.error("Секрет %s не читается: %s", name, type(e).__name__)
    return ""


def mask(value: Optional[str]) -> str:
    return "" if not value else ("•" * 6 + value[-2:] if len(value) > 6 else "•" * 6)
