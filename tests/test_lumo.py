"""«Передать Люмо» by email: subject prefix, MIME structure, attachments cap, Gmail API / SMTP transports
(mocked), failure paths, Google OAuth loopback flow, webhook as the advanced method. Nothing touches the network."""
import email
import email.policy
import json
import smtplib
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import requests

from jarvis import google_auth as ga
from jarvis import lumo
from jarvis.config import load_config
from jarvis.secrets import get_secret, set_secret
from jarvis.skills.base import SkillContext
from jarvis.skills.lumo_skill import SENT_TEXT, LumoSkill

CLIENT = {"installed": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "csecret",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["http://localhost"]}}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture()
def clean(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_OAUTH_CLIENT", str(tmp_path / "absent.json"))
    for n in (ga.TOKEN_SECRET, lumo.SMTP_SECRET, lumo.SECRET_NAME):
        set_secret(n, "")
    ga._cache.clear()
    yield tmp_path
    for n in (ga.TOKEN_SECRET, lumo.SMTP_SECRET, lumo.SECRET_NAME):
        set_secret(n, "")
    ga._cache.clear()


def _with_client(monkeypatch, tmp_path):
    p = tmp_path / "oauth_client.json"
    p.write_text(json.dumps(CLIENT), encoding="utf-8")
    monkeypatch.setenv("JARVIS_OAUTH_CLIENT", str(p))


def _connect(scope=" ".join(ga.SCOPES)):
    set_secret(ga.TOKEN_SECRET, json.dumps({"refresh_token": "1//refresh-xyz", "scope": scope, "client_id": "x"}))


class Resp:
    def __init__(self, code=200, data=None, text=""):
        self.status_code, self._data, self.text = code, data if data is not None else {}, text

    def json(self):
        return self._data


class FakeGoogle:
    """requests-like session: OAuth token endpoint + Gmail upload endpoint."""
    def __init__(self, send_codes=(200,), refresh_code=200, refresh_error=""):
        self.send_codes, self.refresh_code, self.refresh_error = list(send_codes), refresh_code, refresh_error
        self.calls, self.sent, self.refreshes = [], [], 0

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        self.calls.append(url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            self.refreshes += 1
            if self.refresh_code != 200:
                return Resp(self.refresh_code, {"error": self.refresh_error})
            return Resp(200, {"access_token": f"ya29.tok{self.refreshes}", "expires_in": 3599})
        if url == lumo.GMAIL_SEND_URL:
            self.sent.append({"data": data, "headers": headers})
            code = self.send_codes.pop(0) if self.send_codes else 200
            return Resp(code, {"id": "18c0ffee"} if code == 200 else {"error": {"code": code}})
        if url == ga.REVOKE_URI:
            return Resp(200)
        raise AssertionError(url)


class FakeSMTP:
    instances: list = []
    fail_login = False
    fail_connect = False

    def __init__(self, host, port, timeout=None, context=None):
        if FakeSMTP.fail_connect:
            raise OSError("unreachable")
        self.host, self.port, self.context, self.msgs, self.login_args = host, port, context, [], None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pwd):
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad")
        self.login_args = (user, pwd)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.msgs.append((msg.as_bytes(), from_addr, to_addrs))


@pytest.fixture()
def fake_smtp(monkeypatch):
    FakeSMTP.instances, FakeSMTP.fail_login, FakeSMTP.fail_connect = [], False, False
    monkeypatch.setattr(lumo.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(lumo.send_smtp, "__defaults__", (FakeSMTP,))
    return FakeSMTP


def _cfg():
    return load_config(use_local=False)


def _ctx(cfg, said, events):
    class A:
        llm = type("L", (), {"history": [{"role": "user", "content": "какая погода в Сумах?"},
                                         {"role": "assistant", "content": "В Сумах +12, облачно."}]})()

        def emit(self, ev, **kw):
            events.append((ev, kw))
    return SkillContext(cfg=cfg, llm=None, say=lambda t, *a: said.append(t), notify=lambda *a: None,
                        project_root=Path("."), extra={"assistant": A()})


def _wait(lst, n=1):
    for _ in range(100):
        if len(lst) >= n:
            return
        time.sleep(0.03)


def _parse(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.default)


# ------------------------------------------------------------------------------------------------ basics
def test_defaults_and_subject():
    cfg = _cfg()
    assert lumo.recipient(cfg) == "lumo-jarvis@mail.grokbot.com" == lumo.DEFAULT_TO
    assert cfg["lumo"]["method"] == "auto" and lumo.max_total(cfg) == 15 * 1024 * 1024
    assert lumo.subject("найди дешёвые билеты в Варшаву") == "[Джарвис] найди дешёвые билеты в Варшаву"
    long = lumo.subject("проанализируй " + "очень длинную задачу " * 10)
    assert long.startswith("[Джарвис] проанализируй") and long.endswith("…") and len(long) < 80
    assert lumo.subject("") == "[Джарвис] задача (см. вложения)"
    cfg["lumo"]["email_to"] = "not an email"
    assert lumo.recipient(cfg) == lumo.DEFAULT_TO


def test_not_configured_says_and_opens_settings(clean):
    cfg, said, events = _cfg(), [], []
    assert lumo.method(cfg) is None and not lumo.configured(cfg)
    ctx = _ctx(cfg, said, events)
    r = LumoSkill(ctx).match("передай люмо найди билеты", "передай люмо найди билеты")
    assert r.confirm is None and "Google не подключён" in r.reply and "Настройки → Интеграции" in r.reply   # text mode
    opened = []
    ctx.extra["open_settings"] = opened.append                                                         # GUI
    r = LumoSkill(ctx).match("передай люмо найди билеты", "передай люмо найди билеты")
    assert opened == ["integrations"] and "Открываю настройки" in r.reply and r.confirm is None
    with pytest.raises(lumo.LumoError):
        lumo.send(cfg, lumo.build_payload("x", []))


def test_explicit_command_only():
    sk = LumoSkill(_ctx(_cfg(), [], []))
    assert sk.match("включи музыку", "включи музыку") is None
    assert sk.match("какая погода", "какая погода") is None


# ------------------------------------------------------------------------------------------------ MIME
def test_mime_structure_and_attachments(tmp_path):
    cfg = _cfg()
    f = tmp_path / "отчёт.pdf"
    f.write_bytes(b"%PDF-1.4 test")
    atts = [{"filename": "screen.png", "mime": "image/png", "data": PNG}, lumo.file_attachment(f)]
    hist = [{"role": "user", "content": "привет"}, {"role": "assistant", "content": "Добрый день"}]
    p = lumo.build_payload("Найди билеты", hist, ("chrome.exe", "Билеты — Google Chrome"), atts)
    m = _parse(lumo.build_email(cfg, p).as_bytes())
    assert m["To"] == lumo.DEFAULT_TO and m["Subject"] == "[Джарвис] Найди билеты" and m["X-Jarvis-Version"]
    assert m.get_content_type() == "multipart/mixed"
    parts = list(m.iter_parts())
    body = parts[0].get_content()
    assert parts[0].get_content_type() == "text/plain"
    for s in ("Найди билеты", "chrome.exe", "Билеты — Google Chrome", "Пользователь: привет", "Джарвис: Добрый день",
              "screen.png", "отчёт.pdf"):
        assert s in body
    a1, a2 = parts[1], parts[2]
    assert a1.get_content_type() == "image/png" and a1.get_filename() == "screen.png" and a1.get_content() == PNG
    assert a1.get_content_disposition() == "attachment"
    assert a2.get_content_type() == "application/pdf" and a2.get_filename() == "отчёт.pdf"
    assert a2.get_content() == b"%PDF-1.4 test"


def test_attachment_caps(tmp_path):
    cfg = _cfg()
    big = {"filename": "big.bin", "mime": "application/octet-stream", "data": b"0" * (15 * 1024 * 1024 + 1)}
    with pytest.raises(lumo.LumoError, match="15 МБ"):
        lumo.build_email(cfg, lumo.build_payload("t", [], attachments=[big]))
    cfg["lumo"]["max_attachments_mb"] = 1
    with pytest.raises(lumo.LumoError):
        lumo.build_email(cfg, lumo.build_payload("t", [], attachments=[{"filename": "a", "mime": "x/y", "data": b"0" * 1_100_000}]))
    f = tmp_path / "f.bin"
    f.write_bytes(b"0" * 2048)
    with pytest.raises(lumo.LumoError):
        lumo.file_attachment(f, limit=1024)
    no_body = _parse(lumo.build_email(_cfg(), lumo.build_payload("Просто текст", [])).as_bytes())
    assert no_body.get_content_type() == "text/plain"        # no attachments -> simple message


# ------------------------------------------------------------------------------------------------ Gmail API
def test_gmail_send_via_skill(clean, monkeypatch, caplog):
    _with_client(monkeypatch, clean)
    _connect()
    fake = FakeGoogle()
    monkeypatch.setattr(requests, "post", fake.post)
    caplog.set_level("DEBUG")
    cfg, said, events = _cfg(), [], []
    assert lumo.method(cfg) == "gmail"
    sk = LumoSkill(_ctx(cfg, said, events))
    r = sk.match("Передай Люмо найди дешёвые билеты в Варшаву", "передай люмо найди дешёвые билеты в варшаву")
    assert r.confirm and "Отправить?" in r.confirm.prompt and not fake.sent     # nothing before confirmation
    r.confirm.on_yes()
    _wait(said)
    assert said[-1] == SENT_TEXT == "Передал Люмо."
    (sent,) = fake.sent
    assert sent["headers"]["Authorization"] == "Bearer ya29.tok1" and sent["headers"]["Content-Type"] == "message/rfc822"
    m = _parse(sent["data"])
    assert m["Subject"] == "[Джарвис] найди дешёвые билеты в Варшаву" and m["To"] == lumo.DEFAULT_TO
    assert "В Сумах +12, облачно." in m.get_content()                         # recent conversation as context
    assert "1//refresh-xyz" not in caplog.text and "ya29" not in caplog.text     # tokens never logged


def test_gmail_retries_once_on_401_then_fails_cleanly(clean, monkeypatch):
    _with_client(monkeypatch, clean)
    _connect()
    msg = lumo.build_email(_cfg(), lumo.build_payload("t", []))
    fake = FakeGoogle(send_codes=[401, 200])
    assert lumo.send_gmail(msg, session=fake) == "18c0ffee" and fake.refreshes == 2 and len(fake.sent) == 2
    for codes, text in (([403], "переподключи Google"), ([401, 401], "переподключи Google"),
                        ([429], "ограничил"), ([500], "ошибкой 500")):
        ga._cache.clear()
        with pytest.raises(lumo.LumoError, match=text):
            lumo.send_gmail(msg, session=FakeGoogle(send_codes=codes))

    class Down(FakeGoogle):
        def post(self, url, **kw):
            if url == lumo.GMAIL_SEND_URL:
                raise requests.ConnectionError("down")
            return super().post(url, **kw)
    with pytest.raises(lumo.LumoError, match="Нет связи с Gmail"):
        lumo.send_gmail(msg, session=Down())


def test_revoked_token_disconnects(clean, monkeypatch):
    _with_client(monkeypatch, clean)
    _connect()
    with pytest.raises(lumo.LumoError, match="подключи Google заново"):
        lumo.send_gmail(lumo.build_email(_cfg(), lumo.build_payload("t", [])),
                        session=FakeGoogle(refresh_code=400, refresh_error="invalid_grant"))
    assert not ga.connected()


def test_gmail_requires_send_scope(clean, monkeypatch, fake_smtp):
    _with_client(monkeypatch, clean)
    _connect(scope=ga.SCOPE_DRIVE)                      # Drive only -> Gmail not usable
    cfg = _cfg()
    assert lumo.method(cfg) is None
    cfg["lumo"]["smtp_user"] = "me@gmail.com"
    set_secret(lumo.SMTP_SECRET, "abcd efgh ijkl mnop")
    assert lumo.method(cfg) == "smtp"                   # auto falls back to SMTP


def test_no_oauth_client_is_graceful(clean):
    assert not ga.client_available() and ga.load_client() is None
    _connect()
    assert not lumo.gmail_ready()                       # token without a client in the build: unusable, no crash
    with pytest.raises(ga.GoogleAuthError, match="oauth_client.json"):
        ga.authorize(open_browser=lambda u: None)
    with pytest.raises(ga.GoogleAuthError):
        ga.access_token()
    assert "пароль приложения" in lumo.not_configured_message(_cfg())


def test_broken_oauth_client_file(clean, monkeypatch):
    p = clean / "oauth_client.json"
    p.write_text('{"installed": {"client_id": "nope"}}', encoding="utf-8")
    monkeypatch.setenv("JARVIS_OAUTH_CLIENT", str(p))
    assert not ga.client_available()
    p.write_text("{not json", encoding="utf-8")
    assert not ga.client_available()


# ------------------------------------------------------------------------------------------------ OAuth flow
def _browser(code="4/abc", state_override=None, error=None):
    import urllib.parse as up

    def open_browser(url):
        q = up.parse_qs(up.urlparse(url).query)
        assert q["scope"][0].split() == ga.SCOPES and q["code_challenge_method"] == ["S256"]
        assert q["access_type"] == ["offline"] and q["client_id"] == [CLIENT["installed"]["client_id"]]
        params = {"state": state_override or q["state"][0]}
        params.update({"error": error} if error else {"code": code})
        target = q["redirect_uri"][0] + "?" + up.urlencode(params)
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()
    return open_browser


def test_authorize_loopback_pkce(clean, monkeypatch):
    _with_client(monkeypatch, clean)
    got = {}

    class S:
        def post(self, url, data=None, timeout=None, **kw):
            got.update(data)
            return Resp(200, {"access_token": "ya29.first", "expires_in": 3599, "refresh_token": "1//new",
                              "scope": " ".join(ga.SCOPES)})
    granted = ga.authorize(open_browser=_browser(), timeout=10, session=S())
    assert granted == ga.SCOPES and got["code"] == "4/abc" and got["grant_type"] == "authorization_code"
    assert len(got["code_verifier"]) >= 43 and got["redirect_uri"].startswith("http://127.0.0.1:")
    stored = json.loads(get_secret(ga.TOKEN_SECRET))
    assert stored["refresh_token"] == "1//new" and "access_token" not in stored     # only the refresh token persists
    assert ga.connected() and ga.has_scope(ga.SCOPE_GMAIL_SEND) and ga.access_token() == "ya29.first"
    ga.disconnect(session=FakeGoogle())
    assert not ga.connected()


def test_authorize_rejects_bad_state_and_denial(clean, monkeypatch):
    _with_client(monkeypatch, clean)
    with pytest.raises(ga.GoogleAuthError):
        ga.authorize(open_browser=_browser(state_override="forged"), timeout=10, session=FakeGoogle())
    with pytest.raises(ga.GoogleAuthError, match="отменён"):
        ga.authorize(open_browser=_browser(error="access_denied"), timeout=10, session=FakeGoogle())
    with pytest.raises(ga.GoogleAuthError, match="время вышло"):
        ga.authorize(open_browser=lambda u: None, timeout=1.5, session=FakeGoogle())
    assert not ga.connected()


# ------------------------------------------------------------------------------------------------ SMTP
def test_smtp_send_and_failures(clean, fake_smtp, caplog):
    cfg = _cfg()
    cfg["lumo"].update(method="smtp", smtp_user="me@gmail.com")
    assert lumo.method(cfg) is None                      # no password yet
    set_secret(lumo.SMTP_SECRET, "abcd efgh ijkl mnop")
    assert lumo.method(cfg) == "smtp"
    caplog.set_level("DEBUG")
    att = {"filename": "screen.png", "mime": "image/png", "data": PNG}
    assert lumo.send(cfg, lumo.build_payload("Проверь код", [], ("code.exe", "main.py"), [att])) == "smtp"
    (s,) = fake_smtp.instances
    assert (s.host, s.port) == ("smtp.gmail.com", 465) and s.context is not None       # SSL with verification
    assert s.login_args == ("me@gmail.com", "abcdefghijklmnop")
    raw, frm, to = s.msgs[0]
    m = _parse(raw)
    assert frm == "me@gmail.com" and to == [lumo.DEFAULT_TO] and m["From"] == "me@gmail.com"
    assert m["Subject"] == "[Джарвис] Проверь код" and list(m.iter_attachments())[0].get_content() == PNG
    assert "abcd" not in caplog.text
    fake_smtp.fail_login = True
    with pytest.raises(lumo.LumoError, match="пароль приложения"):
        lumo.send(cfg, lumo.build_payload("t", []))
    fake_smtp.fail_login, fake_smtp.fail_connect = False, True
    with pytest.raises(lumo.LumoError, match="Нет связи"):
        lumo.send(cfg, lumo.build_payload("t", []))
    fake_smtp.fail_connect = False
    assert "не отправлялось" in lumo.smtp_check(cfg, "me@gmail.com", "x", smtp_cls=FakeSMTP)


def test_skill_reports_failure(clean, fake_smtp):
    cfg, said = _cfg(), []
    cfg["lumo"]["smtp_user"] = "me@gmail.com"
    set_secret(lumo.SMTP_SECRET, "pw")
    fake_smtp.fail_connect = True
    r = LumoSkill(_ctx(cfg, said, [])).match("передай люмо проверь почту", "передай люмо проверь почту")
    r.confirm.on_yes()
    _wait(said)
    assert said[-1].startswith("Не получилось передать Люмо") and SENT_TEXT not in said


# ------------------------------------------------------------------------------------------------ webhook (advanced)
def test_webhook_only_when_chosen(clean, monkeypatch):
    cfg = _cfg()
    cfg["lumo"]["webhook_url"] = "https://example.invalid/hook"
    set_secret(lumo.SECRET_NAME, "s3cr3t-key")
    assert lumo.method(cfg) is None                      # off by default even when filled in
    cfg["lumo"]["method"] = "webhook"
    assert lumo.method(cfg) == "webhook"
    sent = {}
    monkeypatch.setattr(lumo.requests, "post", lambda url, json=None, headers=None, timeout=None:
                        sent.update(url=url, json=json, headers=headers) or Resp(200, text="ok"))
    att = {"filename": "s.png", "mime": "image/png", "data": PNG}
    assert lumo.send(cfg, lumo.build_payload("найди билеты", [], attachments=[att])) == "webhook"
    p = sent["json"]
    assert p["task"] == "найди билеты" and p["attachments"][0]["filename"] == "s.png"
    import base64
    assert base64.b64decode(p["attachments"][0]["data_base64"]) == PNG
    assert sent["headers"]["Authorization"] == "Bearer s3cr3t-key" and sent["headers"]["X-Webhook-Key"] == "s3cr3t-key"
    monkeypatch.setattr(lumo.requests, "post", lambda *a, **k: Resp(403))
    with pytest.raises(lumo.LumoError, match="не принял ключ"):
        lumo.send(cfg, lumo.build_payload("t", []))


# ------------------------------------------------------------------------------------------------ settings / sync
def test_settings_lumo_section(clean, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    import yaml

    import jarvis.config as config
    import jarvis.gui.settings as st
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "settings.local.yaml")
    monkeypatch.setattr(st, "LOCAL_SETTINGS", tmp_path / "settings.local.yaml")
    saved = {}
    monkeypatch.setattr(st, "save_local_settings", lambda ch: saved.update(ch))
    dlg = st.SettingsDialog(_cfg())
    ch = dlg.collect()
    assert ch["lumo"]["email_to"] == lumo.DEFAULT_TO and ch["lumo"]["method"] == "auto"
    assert not dlg.g_connect.isEnabled() and "Недоступно в этой сборке" in dlg.g_status.text()   # no oauth client
    dlg.select_tab("integrations")
    assert dlg.tabs.tabText(dlg.tabs.currentIndex()) == "Интеграции"
    dlg._set("lumo.email_to", "bad address")
    dlg._set("lumo.smtp_user", "also bad")
    errs = dlg.validate(dlg.collect())
    assert any("адрес почты" in e for e in errs) and any("SMTP" in e for e in errs)
    dlg._set("lumo.email_to", "lumo-jarvis@mail.grokbot.com")
    dlg._set("lumo.smtp_user", "me@gmail.com")
    dlg.smtp_pwd.setText("abcd efgh ijkl mnop")
    monkeypatch.setattr(dlg, "_aliases_path", tmp_path / "aliases.yaml")
    monkeypatch.setattr(dlg, "_devices_path", tmp_path / "devices.yaml")
    dlg._save()
    assert get_secret(lumo.SMTP_SECRET) == "abcd efgh ijkl mnop" and "abcd" not in json.dumps(saved, ensure_ascii=False)
    out = tmp_path / "export.yaml"
    monkeypatch.setattr(st.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    dlg._export()
    assert yaml.safe_load(out.read_text(encoding="utf-8"))["lumo"]["smtp_user"] == ""            # personal address not exported
    dlg._stop_meter()
    _with_client(monkeypatch, clean)
    dlg2 = st.SettingsDialog(_cfg())
    assert dlg2.g_connect.isEnabled() and dlg2.g_connect.text() == "Подключить Google"
    dlg2._stop_meter()


def test_lumo_settings_and_google_token_never_synced():
    from jarvis import sync
    assert not any(k.startswith("lumo") for k in sync.SHAREABLE)
