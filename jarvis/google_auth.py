"""Google sign-in for Jarvis (OAuth 2.0 "installed app" flow with a loopback redirect + PKCE).

One sign-in, combined scopes: drive.file (Google Drive sync, later update) + gmail.send (handoff to Люмо).
The OAuth client (oauth_client.json, Google "Desktop app" client) is injected at build time and may be absent:
then everything here reports "not available" instead of failing.

Only the refresh token (+ granted scopes) is persisted - via secrets.py (Windows Credential Manager / DPAPI),
never in YAML, never logged. The access token lives in memory only."""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets as _rnd
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Callable, Optional

import requests

from .config import PROJECT_ROOT
from .secrets import get_secret, set_secret

log = logging.getLogger(__name__)

SCOPE_DRIVE = "https://www.googleapis.com/auth/drive.file"
SCOPE_GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
SCOPES = [SCOPE_DRIVE, SCOPE_GMAIL_SEND]
TOKEN_SECRET = "google_oauth_token"
CLIENT_FILE = "oauth_client.json"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"

_cache: dict = {}            # in-memory access token: {"token": str, "exp": float}
_lock = threading.Lock()


class GoogleAuthError(Exception):
    pass


def client_path() -> Path:
    env = os.environ.get("JARVIS_OAUTH_CLIENT")
    return Path(env) if env else PROJECT_ROOT / CLIENT_FILE


def load_client() -> Optional[dict]:
    """{'client_id', 'client_secret', ...} of the bundled Desktop-app client, or None when the build has none."""
    p = client_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        c = data.get("installed") or data.get("web") or data
        if not str(c.get("client_id", "")).endswith(".apps.googleusercontent.com"):
            raise ValueError("no client_id")
        return {"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                "auth_uri": c.get("auth_uri", AUTH_URI), "token_uri": c.get("token_uri", TOKEN_URI)}
    except Exception as e:
        log.warning("oauth_client.json не читается (%s) — вход через Google недоступен", type(e).__name__)
        return None


def client_available() -> bool:
    return load_client() is not None


def _stored() -> dict:
    raw = get_secret(TOKEN_SECRET)
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) and d.get("refresh_token") else {}
    except ValueError:
        return {}


def connected() -> bool:
    return bool(_stored())


def has_scope(scope: str) -> bool:
    return scope in str(_stored().get("scope", "")).split()


def account() -> str:
    """Display name of the connected account (best effort, may be '')."""
    return str(_stored().get("account", ""))


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(_rnd.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(client: dict, redirect_uri: str, state: str, challenge: str, scopes: Optional[list] = None) -> str:
    q = {"client_id": client["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
         "scope": " ".join(scopes or SCOPES), "state": state, "code_challenge": challenge,
         "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent",
         "include_granted_scopes": "true"}
    return client.get("auth_uri", AUTH_URI) + "?" + urllib.parse.urlencode(q)


_DONE_PAGE = ("<!doctype html><meta charset='utf-8'><title>Джарвис</title>"
              "<body style='font:16px sans-serif;background:#0b1622;color:#cfefff;text-align:center;padding-top:15%'>"
              "<h2>{}</h2><p>Вкладку можно закрыть и вернуться к Джарвису.</p></body>")


def _wait_for_code(server: http.server.HTTPServer, state: str, timeout: float) -> str:
    box: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404)
                self.end_headers()
                return
            ok = q.get("state", [""])[0] == state and "code" in q
            box["code" if ok else "error"] = q["code"][0] if ok else q.get("error", ["state_mismatch"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_DONE_PAGE.format("Готово, Google подключён" if ok else "Вход отменён").encode())

        def log_message(self, *a):  # no request logging (URL holds the code)
            pass

    server.RequestHandlerClass = H
    server.timeout = 1.0
    end = time.time() + timeout
    while time.time() < end and not box:
        server.handle_request()
    server.server_close()
    if "code" in box:
        return box["code"]
    if "error" in box:
        raise GoogleAuthError("Вход в Google отменён" if box["error"] == "access_denied" else f"Google: {box['error']}")
    raise GoogleAuthError("Не дождался входа в Google (время вышло)")


def authorize(open_browser: Callable[[str], object] = webbrowser.open, timeout: float = 240,
              client: Optional[dict] = None, session=requests) -> list:
    """Interactive sign-in in the default browser. Stores the refresh token; returns the granted scopes."""
    client = client or load_client()
    if not client:
        raise GoogleAuthError("В этой сборке нет ключа приложения Google (oauth_client.json)")
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    redirect = f"http://127.0.0.1:{server.server_address[1]}/"
    state = _rnd.token_urlsafe(24)
    verifier, challenge = _pkce()
    open_browser(auth_url(client, redirect, state, challenge))
    code = _wait_for_code(server, state, timeout)
    try:
        r = session.post(client.get("token_uri", TOKEN_URI), timeout=30, data={
            "code": code, "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
            "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier})
    except requests.RequestException as e:
        raise GoogleAuthError(f"Нет связи с Google: {type(e).__name__}") from None
    if r.status_code != 200:
        raise GoogleAuthError(f"Google не выдал доступ (HTTP {r.status_code})")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise GoogleAuthError("Google не вернул долгосрочный доступ, попробуй подключить ещё раз")
    granted = str(tok.get("scope", " ".join(SCOPES))).split()
    set_secret(TOKEN_SECRET, json.dumps({"refresh_token": tok["refresh_token"], "scope": " ".join(granted),
                                         "client_id": client["client_id"]}))
    with _lock:
        _cache.update(token=tok.get("access_token", ""), exp=time.time() + float(tok.get("expires_in", 0)) - 60)
    log.info("Google подключён, разрешения: %s", ", ".join(s.rsplit("/", 1)[-1] for s in granted))
    return granted


def access_token(session=requests, force: bool = False) -> str:
    """A valid access token (refreshed when needed)."""
    st = _stored()
    if not st:
        raise GoogleAuthError("Google не подключён")
    with _lock:
        if not force and _cache.get("token") and _cache.get("exp", 0) > time.time():
            return _cache["token"]
    client = load_client()
    if not client:
        raise GoogleAuthError("В этой сборке нет ключа приложения Google (oauth_client.json)")
    try:
        r = session.post(client.get("token_uri", TOKEN_URI), timeout=30, data={
            "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
            "refresh_token": st["refresh_token"], "grant_type": "refresh_token"})
    except requests.RequestException as e:
        raise GoogleAuthError(f"Нет связи с Google: {type(e).__name__}") from None
    if r.status_code in (400, 401):
        err = ""
        try:
            err = r.json().get("error", "")
        except ValueError:
            pass
        if err in ("invalid_grant", "unauthorized_client", "invalid_client"):
            disconnect(revoke=False)
            raise GoogleAuthError("Доступ к Google отозван или устарел — подключи Google заново")
    if r.status_code != 200:
        raise GoogleAuthError(f"Google не обновил доступ (HTTP {r.status_code})")
    tok = r.json()
    with _lock:
        _cache.update(token=tok["access_token"], exp=time.time() + float(tok.get("expires_in", 3600)) - 60)
        return _cache["token"]


def disconnect(revoke: bool = True, session=requests) -> None:
    st = _stored()
    if revoke and st:
        try:
            session.post(REVOKE_URI, data={"token": st["refresh_token"]}, timeout=10)
        except Exception:
            pass
    set_secret(TOKEN_SECRET, "")
    with _lock:
        _cache.clear()
