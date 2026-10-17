"""'Передать Люмо' — hand a task over to the cloud assistant Люмо by EMAIL. ONLY on explicit command.

Message: To = lumo.email_to (default lumo-jarvis@mail.grokbot.com), Subject "[Джарвис] <short summary>",
text body with the task + context (active app, window title, recent conversation), screenshot / file as real
MIME attachments (total cap lumo.max_attachments_mb, default 15 MB).

Sending methods (lumo.method):
  auto     - Gmail API if Google is connected (gmail.send scope), else SMTP if configured
  gmail    - Gmail API users.messages.send (same Google sign-in as Drive sync, see google_auth.py)
  smtp     - smtp.gmail.com:465 SSL with the user's Gmail address + app password (stored via DPAPI)
  webhook  - advanced/legacy: JSON POST to lumo.webhook_url with a sender key (off unless chosen)
Secrets (app password, webhook key, Google token) are never logged and never stored in YAML."""
from __future__ import annotations

import base64
import datetime as _dt
import io
import logging
import mimetypes
import re
import smtplib
import ssl
from email import policy as _policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Optional

import requests

from . import __version__
from . import google_auth as ga
from .secrets import get_secret

log = logging.getLogger(__name__)
DEFAULT_TO = "lumo-jarvis@mail.grokbot.com"
SUBJECT_PREFIX = "[Джарвис]"
MAX_TOTAL = 15 * 1024 * 1024          # attachments, before base64
MAX_FILE = MAX_TOTAL                  # kept for the GUI file picker
SECRET_NAME = "lumo_sender_key"       # webhook sender key (advanced method)
SMTP_SECRET = "lumo_smtp_app_password"
GMAIL_SEND_URL = "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send?uploadType=media"
EMAIL_RE = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$")
METHODS = ("auto", "gmail", "smtp", "webhook")
# CRLF, no header folding: folding non-ASCII subjects loses spaces between encoded words (CPython bug)
MAIL_POLICY = _policy.SMTP.clone(max_line_length=998)


class LumoError(Exception):
    pass


# ----------------------------------------------------------------- configuration
def _c(cfg: dict) -> dict:
    return cfg.get("lumo", {}) or {}


def recipient(cfg: dict) -> str:
    to = str(_c(cfg).get("email_to") or DEFAULT_TO).strip()
    return to if EMAIL_RE.match(to) else DEFAULT_TO


def max_total(cfg: dict) -> int:
    try:
        mb = float(_c(cfg).get("max_attachments_mb", 15))
    except (TypeError, ValueError):
        mb = 15
    return int(max(1.0, min(mb, 20.0)) * 1024 * 1024)       # Gmail limit is 25 MB after base64


def gmail_ready() -> bool:
    return ga.client_available() and ga.connected() and ga.has_scope(ga.SCOPE_GMAIL_SEND)


def smtp_ready(cfg: dict) -> bool:
    return bool(EMAIL_RE.match(str(_c(cfg).get("smtp_user", "")).strip()) and get_secret(SMTP_SECRET))


def webhook_ready(cfg: dict) -> bool:
    return str(_c(cfg).get("webhook_url", "")).startswith("https://") and bool(get_secret(SECRET_NAME))


def method(cfg: dict) -> Optional[str]:
    """The method that will actually be used, or None when nothing is set up."""
    c = _c(cfg)
    if not c.get("enabled", True):
        return None
    m = str(c.get("method", "auto"))
    if m == "gmail":
        return "gmail" if gmail_ready() else None
    if m == "smtp":
        return "smtp" if smtp_ready(cfg) else None
    if m == "webhook":
        return "webhook" if webhook_ready(cfg) else None
    if gmail_ready():
        return "gmail"
    if smtp_ready(cfg):
        return "smtp"
    return None


def configured(cfg: dict) -> bool:
    return method(cfg) is not None


def not_configured_message(cfg: dict, opening: bool = True) -> str:
    """Short spoken text when nothing is set up. opening=True: the GUI opens Settings → Интеграции now."""
    where = "Открываю настройки, вкладка «Интеграции»" if opening else "Это делается в «Настройки → Интеграции»"
    if str(_c(cfg).get("method", "auto")) == "webhook":
        return f"Webhook для Люмо не настроен. {where}."
    if ga.client_available():
        return f"Google не подключён. {where}: нажми «Подключить Google»."
    return f"Google не подключён, а почта для Люмо не настроена. {where}: укажи Gmail и пароль приложения."


# ----------------------------------------------------------------- attachments
def screenshot_attachment() -> dict:
    from .screen import capture
    img = capture(active_window=False, max_side=1600)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return {"filename": f"screen_{_dt.datetime.now():%Y%m%d_%H%M%S}.png", "mime": "image/png",
            "data": buf.getvalue()}


def file_attachment(path: str | Path, limit: int = MAX_TOTAL) -> dict:
    p = Path(path)
    size = p.stat().st_size
    if size > limit:
        raise LumoError(f"Файл больше {limit // 1024 // 1024} МБ ({size / 1024 / 1024:.1f} МБ)")
    return {"filename": p.name, "mime": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
            "data": p.read_bytes()}


def _att_bytes(a: dict) -> bytes:
    if "data" in a:
        return bytes(a["data"])
    return base64.b64decode(a.get("data_base64", ""))


# ----------------------------------------------------------------- message
def build_payload(task: str, history: list, window: tuple = ("", ""), attachments: Optional[list] = None,
                  turns: int = 6) -> dict:
    chat = [{"role": m.get("role"), "text": str(m.get("content", ""))[:2000]}
            for m in history if m.get("role") in ("user", "assistant") and m.get("content")][-turns:]
    return {"source": "jarvis", "version": __version__, "task": task.strip(),
            "sent_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "context": {"active_app": window[0], "window_title": window[1], "recent_chat": chat},
            "attachments": attachments or []}


def summary(task: str, limit: int = 60) -> str:
    t = re.sub(r"\s+", " ", (task or "").strip()).strip(" .,:;")
    if not t:
        return "задача (см. вложения)"
    t = t.splitlines()[0]
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0] or t[:limit]
    return cut.rstrip(" ,.:;") + "…"


def subject(task: str) -> str:
    return f"{SUBJECT_PREFIX} {summary(task)}"


def body_text(payload: dict) -> str:
    ctx = payload.get("context", {})
    who = {"user": "Пользователь", "assistant": "Джарвис"}
    lines = ["Задача:", payload.get("task") or "(см. вложения)", "", "Контекст:"]
    lines.append(f"• Активное приложение: {ctx.get('active_app') or '—'}")
    if ctx.get("window_title"):
        lines.append(f"• Заголовок окна: {ctx['window_title']}")
    lines.append(f"• Отправлено: {payload.get('sent_at', '')}")
    chat = ctx.get("recent_chat") or []
    if chat:
        lines += ["", "Недавний разговор:"]
        lines += [f"{who.get(m.get('role'), m.get('role'))}: {m.get('text', '')}" for m in chat]
    atts = payload.get("attachments") or []
    if atts:
        lines += ["", "Вложения: " + ", ".join(a.get("filename", "file") for a in atts)]
    lines += ["", f"— Джарвис {payload.get('version', __version__)} (отправлено по команде пользователя)"]
    return "\n".join(lines)


def build_email(cfg: dict, payload: dict, sender: str = "") -> EmailMessage:
    atts = payload.get("attachments") or []
    blobs = [_att_bytes(a) for a in atts]
    total, cap = sum(len(b) for b in blobs), max_total(cfg)
    if total > cap:
        raise LumoError(f"Вложения больше {cap // 1024 // 1024} МБ ({total / 1024 / 1024:.1f} МБ)")
    msg = EmailMessage(policy=MAIL_POLICY)
    msg["To"] = recipient(cfg)
    if sender:
        msg["From"] = sender
    msg["Subject"] = subject(payload.get("task", ""))
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="jarvis.local")
    msg["X-Jarvis-Version"] = str(payload.get("version", __version__))
    msg.set_content(body_text(payload))
    for a, data in zip(atts, blobs):
        maintype, _, subtype = str(a.get("mime") or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream",
                           filename=a.get("filename") or "file")
    return msg


# ----------------------------------------------------------------- transports
def send_gmail(msg: EmailMessage, session=requests) -> str:
    raw = msg.as_bytes()
    for attempt in (0, 1):
        try:
            token = ga.access_token(session=session, force=bool(attempt))
        except ga.GoogleAuthError as e:
            raise LumoError(str(e)) from None
        try:
            r = session.post(GMAIL_SEND_URL, data=raw, timeout=60,
                             headers={"Authorization": f"Bearer {token}", "Content-Type": "message/rfc822"})
        except requests.RequestException as e:
            raise LumoError(f"Нет связи с Gmail: {type(e).__name__}") from None
        if r.status_code == 401 and not attempt:
            continue                                   # token expired early - refresh once
        if r.status_code in (401, 403):
            raise LumoError("Gmail не разрешил отправку — переподключи Google в настройках")
        if r.status_code == 429:
            raise LumoError("Gmail временно ограничил отправку, попробуй позже")
        if r.status_code >= 400:
            raise LumoError(f"Gmail ответил ошибкой {r.status_code}")
        try:
            return str(r.json().get("id", ""))
        except ValueError:
            return ""
    raise LumoError("Gmail не принял доступ")


def send_smtp(cfg: dict, msg: EmailMessage, smtp_cls=smtplib.SMTP_SSL) -> str:
    c = _c(cfg)
    user = str(c.get("smtp_user", "")).strip()
    pwd = get_secret(SMTP_SECRET)
    if not EMAIL_RE.match(user) or not pwd:
        raise LumoError("SMTP не настроен: укажи Gmail и пароль приложения")
    if "From" not in msg:
        msg["From"] = user
    host, port = str(c.get("smtp_host") or "smtp.gmail.com"), int(c.get("smtp_port") or 465)
    try:
        with smtp_cls(host, port, timeout=float(c.get("timeout_s", 30)), context=ssl.create_default_context()) as s:
            s.login(user, pwd.replace(" ", ""))
            s.send_message(msg, from_addr=user, to_addrs=[recipient(cfg)])
    except smtplib.SMTPAuthenticationError:
        raise LumoError("Почта не приняла логин или пароль приложения") from None
    except smtplib.SMTPException as e:
        raise LumoError(f"Почтовый сервер отказал: {type(e).__name__}") from None
    except OSError as e:
        raise LumoError(f"Нет связи с почтовым сервером: {type(e).__name__}") from None
    return str(msg["Message-ID"])


def smtp_check(cfg: dict, user: str, pwd: str, smtp_cls=smtplib.SMTP_SSL) -> str:
    """Login only - nothing is sent."""
    c = _c(cfg)
    try:
        with smtp_cls(str(c.get("smtp_host") or "smtp.gmail.com"), int(c.get("smtp_port") or 465),
                      timeout=20, context=ssl.create_default_context()) as s:
            s.login(user.strip(), pwd.replace(" ", ""))
    except smtplib.SMTPAuthenticationError:
        raise LumoError("Почта не приняла логин или пароль приложения") from None
    except (smtplib.SMTPException, OSError) as e:
        raise LumoError(f"Нет связи с почтовым сервером: {type(e).__name__}") from None
    return "Вход в почту работает ✓ (письмо не отправлялось)"


def webhook_headers(cfg: dict, key: Optional[str] = None) -> dict:
    c = _c(cfg)
    key = key if key is not None else get_secret(SECRET_NAME)
    h = {"Content-Type": "application/json", "User-Agent": f"Jarvis/{__version__}"}
    name = c.get("auth_header", "Authorization") or "Authorization"
    fmt = c.get("auth_format", "Bearer {key}") or "{key}"
    h[name] = fmt.replace("{key}", key)
    extra = c.get("extra_key_header", "X-Webhook-Key")
    if extra and extra.lower() != name.lower():
        h[extra] = key
    return h


headers = webhook_headers          # backwards compatible name


def send_webhook(cfg: dict, payload: dict, key: Optional[str] = None, url: Optional[str] = None) -> str:
    url = url or _c(cfg).get("webhook_url", "")
    if not str(url).startswith("https://"):
        raise LumoError("Webhook URL должен начинаться с https://")
    key = key if key is not None else get_secret(SECRET_NAME)
    if not key:
        raise LumoError("Не задан ключ отправителя")
    body = dict(payload)
    body["attachments"] = [{"filename": a.get("filename"), "mime": a.get("mime"),
                            "data_base64": base64.b64encode(_att_bytes(a)).decode()} for a in payload.get("attachments") or []]
    try:
        r = requests.post(url, json=body, headers=webhook_headers(cfg, key), timeout=float(_c(cfg).get("timeout_s", 30)))
    except requests.RequestException as e:
        raise LumoError(f"Нет связи с Люмо: {type(e).__name__}") from None
    if r.status_code in (401, 403):
        raise LumoError("Люмо не принял ключ (доступ запрещён)")
    if r.status_code >= 400:
        raise LumoError(f"Люмо ответил ошибкой {r.status_code}")
    return r.text[:500]


def send(cfg: dict, payload: dict) -> str:
    """Send with the configured method. -> method name. Raises LumoError with a short Russian reason."""
    m = method(cfg)
    if m is None:
        raise LumoError(not_configured_message(cfg))
    n_att = len(payload.get("attachments") or [])
    if m == "webhook":
        send_webhook(cfg, payload)
    else:
        msg = build_email(cfg, payload, sender=str(_c(cfg).get("smtp_user", "")).strip() if m == "smtp" else "")
        send_gmail(msg) if m == "gmail" else send_smtp(cfg, msg)
    log.info("Передано Люмо (%s): %d символов, вложений %d", m, len(payload.get("task", "")), n_att)
    return m


def test_connection(cfg: dict, url: str, key: str) -> str:
    """Webhook check (advanced method): sends a test JSON, no email."""
    send_webhook(cfg, {"source": "jarvis", "version": __version__, "test": True, "task": "Проверка связи от Джарвиса",
                       "context": {}, "attachments": []}, key=key, url=url)
    return "Связь с Люмо есть ✓"
