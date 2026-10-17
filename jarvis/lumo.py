"""'Передать Люмо' — forward a task to the cloud assistant via webhook. ONLY on explicit command.

Payload: {"source":"jarvis","version":..., "task": str, "context": {...}, "attachments": [...]}.
The sender key is sent in a configurable header and is never logged."""
from __future__ import annotations

import base64
import datetime as _dt
import io
import logging
from pathlib import Path
from typing import Optional

import requests

from . import __version__
from .secrets import get_secret

log = logging.getLogger(__name__)
MAX_FILE = 5 * 1024 * 1024
SECRET_NAME = "lumo_sender_key"


class LumoError(Exception):
    pass


def configured(cfg: dict) -> bool:
    c = cfg.get("lumo", {})
    return bool(c.get("enabled", True) and str(c.get("webhook_url", "")).startswith("https://") and get_secret(SECRET_NAME))


def headers(cfg: dict, key: Optional[str] = None) -> dict:
    c = cfg.get("lumo", {})
    key = key if key is not None else get_secret(SECRET_NAME)
    h = {"Content-Type": "application/json", "User-Agent": f"Jarvis/{__version__}"}
    name = c.get("auth_header", "Authorization") or "Authorization"
    fmt = c.get("auth_format", "Bearer {key}") or "{key}"
    h[name] = fmt.replace("{key}", key)
    extra = c.get("extra_key_header", "X-Webhook-Key")
    if extra and extra.lower() != name.lower():
        h[extra] = key
    return h


def screenshot_attachment() -> dict:
    from .screen import capture
    img = capture(active_window=False, max_side=1600)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return {"filename": f"screen_{_dt.datetime.now():%Y%m%d_%H%M%S}.png", "mime": "image/png",
            "data_base64": base64.b64encode(buf.getvalue()).decode()}


def file_attachment(path: str | Path) -> dict:
    p = Path(path)
    size = p.stat().st_size
    if size > MAX_FILE:
        raise LumoError(f"Файл больше 5 МБ ({size // 1024 // 1024} МБ)")
    import mimetypes
    return {"filename": p.name, "mime": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
            "data_base64": base64.b64encode(p.read_bytes()).decode()}


def build_payload(task: str, history: list, window: tuple = ("", ""), attachments: Optional[list] = None,
                  turns: int = 6) -> dict:
    chat = [{"role": m.get("role"), "text": str(m.get("content", ""))[:2000]}
            for m in history if m.get("role") in ("user", "assistant") and m.get("content")][-turns:]
    return {"source": "jarvis", "version": __version__, "task": task.strip(),
            "sent_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "context": {"active_app": window[0], "window_title": window[1], "recent_chat": chat},
            "attachments": attachments or []}


def send(cfg: dict, payload: dict, key: Optional[str] = None, url: Optional[str] = None) -> str:
    url = url or cfg.get("lumo", {}).get("webhook_url", "")
    if not str(url).startswith("https://"):
        raise LumoError("Webhook URL должен начинаться с https://")
    key = key if key is not None else get_secret(SECRET_NAME)
    if not key:
        raise LumoError("Не задан ключ отправителя")
    try:
        r = requests.post(url, json=payload, headers=headers(cfg, key),
                          timeout=float(cfg.get("lumo", {}).get("timeout_s", 30)))
    except requests.RequestException as e:
        raise LumoError(f"Нет связи с Люмо: {type(e).__name__}") from None
    if r.status_code in (401, 403):
        raise LumoError("Люмо не принял ключ (доступ запрещён)")
    if r.status_code >= 400:
        raise LumoError(f"Люмо ответил ошибкой {r.status_code}")
    log.info("Передано Люмо: %d символов, вложений %d, HTTP %d", len(payload.get("task", "")),
             len(payload.get("attachments", [])), r.status_code)
    return r.text[:500]


def test_connection(cfg: dict, url: str, key: str) -> str:
    send(cfg, {"source": "jarvis", "version": __version__, "test": True, "task": "Проверка связи от Джарвиса",
               "context": {}, "attachments": []}, key=key, url=url)
    return "Связь с Люмо есть ✓"
