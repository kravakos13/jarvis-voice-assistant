"""Ollama on a fresh PC: discovery without a refreshed PATH, hidden auto-start (tray app first, `serve` fallback,
waiting for an autostarting tray app), resumable download + silent install, model pulls, actionable messages.
Everything external (HTTP, processes, clock, disk) is mocked."""
import importlib.util
import json
import types
from pathlib import Path

import pytest
import requests

from jarvis import ollama_setup as osu


def cfg(host="http://localhost:11434", **ollama):
    return {"llm": {"host": host, "model": "qwen2.5:7b"}, "code": {"enabled": True, "model": "qwen2.5-coder:7b"},
            "memory": {"enabled": True, "embed_model": "nomic-embed-text"}, "ollama": ollama}


class R:
    def __init__(self, code=200, data=None, chunks=(), headers=None, lines=()):
        self.status_code, self._data, self._chunks, self.headers, self._lines = code, data or {}, chunks, headers or {}, lines

    def json(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        for c in self._chunks:
            if isinstance(c, Exception):
                raise c
            yield c

    def iter_lines(self):
        yield from self._lines

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class Server:
    """Fake Ollama HTTP: down until `up_after` health checks, then answers /api/tags."""
    def __init__(self, up_after=None, models=()):
        self.up_after, self.checks, self.models = up_after, 0, list(models)

    def get(self, url, timeout=None, **kw):
        assert url.endswith("/api/tags")
        self.checks += 1
        if self.up_after is None or self.checks <= self.up_after:
            raise requests.ConnectionError("refused")
        return R(200, {"models": [{"name": m} for m in self.models]})


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture()
def install_dir(tmp_path):
    d = tmp_path / "LocalAppData" / "Programs" / "Ollama"
    d.mkdir(parents=True)
    exe = d / osu.EXE_NAME
    exe.write_bytes(b"MZ")
    return types.SimpleNamespace(root=tmp_path / "LocalAppData", dir=d, exe=exe)


# ------------------------------------------------------------------ discovery
def test_finds_ollama_without_refreshed_path(install_dir):
    env = {"LOCALAPPDATA": str(install_dir.root), "PATH": ""}        # PATH of a session started before setup
    assert osu.find_exe(env=env, which=lambda n: None, registry_dirs=[]) == install_dir.exe
    custom = install_dir.root.parent / "Tools" / "Ollama"
    custom.mkdir(parents=True)
    (custom / osu.EXE_NAME).write_bytes(b"MZ")
    found = osu.find_exe(env={"PATH": ""}, which=lambda n: None, registry_dirs=[str(custom)])   # PATH from registry
    assert found == custom / osu.EXE_NAME
    assert osu.find_exe(env={"PATH": ""}, which=lambda n: None, registry_dirs=[]) is None
    assert osu.find_app(install_dir.exe) is None
    (install_dir.dir / osu.APP_NAME).write_bytes(b"MZ")
    assert osu.find_app(install_dir.exe) == install_dir.dir / osu.APP_NAME


def test_status_and_messages(install_dir):
    up, down = Server(up_after=0), Server()
    assert osu.status(cfg(), session=up) == "running"
    assert osu.status(cfg(), session=down, find=lambda: install_dir.exe) == "stopped"
    assert osu.status(cfg(), session=down, find=lambda: None) == "missing"
    assert osu.status(cfg("http://192.168.1.5:11434"), session=down) == "remote"
    miss = osu.friendly_error(cfg(), "missing")
    assert "не установлена" in miss and "Установить Ollama" in miss and "https://ollama.com/download" in miss
    stop = osu.friendly_error(cfg(), "stopped", find=lambda: install_dir.exe)
    assert str(install_dir.exe) in stop and "меню Пуск" in stop
    assert "192.168.1.5" in osu.friendly_error(cfg("http://192.168.1.5:11434"), "remote")


def test_llm_check_message_is_actionable(monkeypatch):
    from jarvis.llm import OllamaChat
    monkeypatch.setattr(osu, "find_exe", lambda *a, **k: None)
    s = types.SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("x")))
    ok, msg = OllamaChat({"host": "http://localhost:11434", "model": "qwen2.5:7b"}, session=s).check()
    assert not ok and "не установлена" in msg and "Установить Ollama" in msg
    tags = types.SimpleNamespace(get=lambda *a, **k: R(200, {"models": [{"name": "llama3:latest"}]}))
    ok, msg = OllamaChat({"host": "http://localhost:11434", "model": "qwen2.5:7b"}, session=tags).check()
    assert not ok and "не скачана" in msg and "Докачать компоненты" in msg


# ------------------------------------------------------------------ start
def _start(server, find, procs=lambda: [], c=None, **kw):
    clock = Clock()
    calls = []

    def popen(argv, env=None):
        calls.append((list(map(str, argv)), env))
    ok = osu.start(c or cfg(), wait=30, session=server, popen=popen, sleep=clock.sleep, clock=clock,
                   find=find, procs=procs, **kw)
    return ok, calls, clock


def test_start_already_running_does_nothing(install_dir):
    ok, calls, _ = _start(Server(up_after=0), lambda: install_dir.exe)
    assert ok and calls == []


def test_start_prefers_tray_app(install_dir):
    (install_dir.dir / osu.APP_NAME).write_bytes(b"MZ")
    ok, calls, _ = _start(Server(up_after=4), lambda: install_dir.exe)
    assert ok and [c[0] for c in calls] == [[str(install_dir.dir / osu.APP_NAME)]]      # no extra `serve`


def test_start_falls_back_to_hidden_serve(install_dir):
    (install_dir.dir / osu.APP_NAME).write_bytes(b"MZ")
    srv = Server(up_after=16)                              # tray app does not bring the server up within ~12 s
    ok, calls, clock = _start(srv, lambda: install_dir.exe)
    assert ok and calls[0][0] == [str(install_dir.dir / osu.APP_NAME)]
    assert calls[1][0] == [str(install_dir.exe), "serve"] and clock.t <= 30


def test_start_without_tray_app_and_custom_port(install_dir):
    ok, calls, _ = _start(Server(up_after=2), lambda: install_dir.exe, c=cfg("http://127.0.0.1:11500"))
    assert ok and calls[0][0] == [str(install_dir.exe), "serve"] and calls[0][1]["OLLAMA_HOST"] == "127.0.0.1:11500"


def test_start_waits_for_autostarting_tray_app(install_dir):
    ok, calls, _ = _start(Server(up_after=5), lambda: install_dir.exe, procs=lambda: ["ollama app.exe"])
    assert ok and calls == []                              # it was already starting at login: no second server


def test_start_gives_up_after_timeout(install_dir):
    ok, calls, clock = _start(Server(), lambda: install_dir.exe)
    assert not ok and len(calls) == 1 and 29 <= clock.t <= 32


def test_start_not_installed_or_remote():
    assert _start(Server(), lambda: None)[:2] == (False, [])
    ok, calls, _ = _start(Server(), lambda: Path("/x/ollama"), c=cfg("http://10.0.0.2:11434"))
    assert not ok and calls == []


# ------------------------------------------------------------------ download / install
class Downloads:
    def __init__(self, plan):
        self.plan, self.requests = list(plan), []

    def get(self, url, stream=None, timeout=None, headers=None, allow_redirects=None):
        self.requests.append(dict(headers or {}))
        step = self.plan.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_download_resumes_after_interruption(tmp_path, monkeypatch):
    monkeypatch.setattr(osu, "MIN_SETUP_BYTES", 10)
    body = b"MZ" + b"x" * 98
    s = Downloads([R(200, chunks=[body[:40], requests.ConnectionError("reset")], headers={"content-length": "100"}),
                   R(206, chunks=[body[40:]], headers={"content-length": "60"})])
    seen = []
    out = osu.download("https://x/OllamaSetup.exe", tmp_path / "OllamaSetup.exe", lambda l, p: seen.append(p),
                       session=s, sleep=lambda s: None)
    assert out.read_bytes() == body and s.requests[1] == {"Range": "bytes=40-"} and seen[-1] == 100
    assert not (tmp_path / "OllamaSetup.exe.part").exists()


def test_download_rejects_error_pages_and_gives_up(tmp_path, monkeypatch):
    s = Downloads([R(200, chunks=[b"<html>blocked</html>"], headers={"content-length": "20"})])
    with pytest.raises(osu.OllamaSetupError, match="не похож"):
        osu.download("https://x/s.exe", tmp_path / "s.exe", session=s, sleep=lambda s: None)
    s = Downloads([requests.ConnectionError("no net")] * 4)
    with pytest.raises(osu.OllamaSetupError, match="Проверь интернет"):
        osu.download("https://x/s.exe", tmp_path / "s2.exe", session=s, sleep=lambda s: None)
    s = Downloads([R(404)] * 4)
    with pytest.raises(osu.OllamaSetupError):
        osu.download("https://x/s.exe", tmp_path / "s3.exe", session=s, sleep=lambda s: None)


def _fake_download(monkeypatch, tmp_path):
    def dl(url, dest, progress=None, session=None):
        dest.write_bytes(b"MZ" + b"0" * 10)
        return dest
    monkeypatch.setattr(osu, "download", dl)


def test_install_silent_flags_and_result(tmp_path, monkeypatch, install_dir):
    _fake_download(monkeypatch, tmp_path)
    runs = []
    exe = osu.install(cfg(), cache_dir=tmp_path, platform_ok=True, disk_free=lambda p: 50 * 1024 ** 3,
                      find=lambda: install_dir.exe,
                      run=lambda argv, timeout=None: runs.append(argv) or types.SimpleNamespace(returncode=0))
    assert exe == install_dir.exe
    argv = runs[0]
    assert argv[0].endswith("OllamaSetup.exe") and argv[1:5] == ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]
    assert argv[5].startswith("/LOG=") and not (tmp_path / "OllamaSetup.exe").exists()      # setup removed


def test_install_failure_paths(tmp_path, monkeypatch, install_dir):
    _fake_download(monkeypatch, tmp_path)
    ok_run = lambda argv, timeout=None: types.SimpleNamespace(returncode=0)  # noqa: E731
    base = dict(cache_dir=tmp_path, platform_ok=True, disk_free=lambda p: 50 * 1024 ** 3)
    with pytest.raises(osu.OllamaSetupError, match="только в Windows"):
        osu.install(cfg(), cache_dir=tmp_path, platform_ok=False)
    with pytest.raises(osu.OllamaSetupError, match="Мало места"):
        osu.install(cfg(), cache_dir=tmp_path, platform_ok=True, disk_free=lambda p: 2 * 1024 ** 3)
    with pytest.raises(osu.OllamaSetupError, match="кодом 5"):
        osu.install(cfg(), run=lambda a, timeout=None: types.SimpleNamespace(returncode=5), find=lambda: install_dir.exe, **base)

    def slow(a, timeout=None):
        import subprocess
        raise subprocess.TimeoutExpired(a, timeout)
    with pytest.raises(osu.OllamaSetupError, match="30 минут"):
        osu.install(cfg(), run=slow, find=lambda: install_dir.exe, **base)
    with pytest.raises(osu.OllamaSetupError, match="не найден"):
        osu.install(cfg(), run=ok_run, find=lambda: None, **base)
    with pytest.raises(osu.OllamaSetupError, match="https://"):
        osu.install(cfg(setup_url="http://evil/x.exe"), run=ok_run, find=lambda: install_dir.exe, **base)


# ------------------------------------------------------------------ models / ensure
class Pulls(Server):
    def __init__(self, models, fail=()):
        super().__init__(up_after=0, models=models)
        self.fail, self.pulled = set(fail), []

    def post(self, url, json=None, stream=None, timeout=None):
        m = json["model"]
        self.pulled.append(m)
        if m in self.fail:
            return R(200, lines=[b'{"error":"pull model manifest: file does not exist"}'])
        return R(200, lines=[b'{"status":"pulling","total":100,"completed":50}', b"",
                             b'{"status":"pulling","total":100,"completed":100}', b'{"status":"success"}'])


def test_missing_models_and_pull():
    s = Pulls(["qwen2.5:7b", "nomic-embed-text:latest"])
    assert osu.missing_models(cfg(), s) == ["qwen2.5-coder:7b"]
    seen = []
    assert osu.pull_missing(cfg(), lambda l, p: seen.append(p), s) == [] and s.pulled == ["qwen2.5-coder:7b"]
    assert seen == [50, 100]
    s = Pulls([], fail={"qwen2.5:7b"})
    errs = osu.pull_missing(cfg(), None, s)
    assert len(errs) == 1 and "qwen2.5:7b" in errs[0] and s.pulled == ["qwen2.5:7b", "qwen2.5-coder:7b", "nomic-embed-text"]
    c = cfg()
    c["code"]["enabled"] = False
    assert osu.wanted_models(c) == ["qwen2.5:7b", "nomic-embed-text"]


def test_ensure_chain(monkeypatch, install_dir):
    events = []
    state = {"installed": False}
    monkeypatch.setattr(osu, "start", lambda c, wait=30, session=None, progress=None:
                        events.append("start") or state["installed"])
    monkeypatch.setattr(osu, "status", lambda c, session=None: "stopped" if state["installed"] else "missing")

    def inst(c, progress=None, session=None):
        events.append("install")
        state["installed"] = True
        return install_dir.exe
    monkeypatch.setattr(osu, "install", inst)
    monkeypatch.setattr(osu, "pull_missing", lambda c, p=None, s=None: events.append("pull") or [])
    ok, msg = osu.ensure(cfg(), allow_install=False)
    assert not ok and "не установлена" in msg and events == ["start"]
    events.clear()
    ok, msg = osu.ensure(cfg(), allow_install=True)
    assert ok and events == ["start", "install", "start", "pull"]


# ------------------------------------------------------------------ app + installer integration
def test_assistant_autostarts_and_reports(monkeypatch, install_dir):
    from jarvis.assistant import Assistant
    started, events = [], []
    monkeypatch.setattr(osu, "is_running", lambda host, session=None, timeout=2.0: False)
    monkeypatch.setattr(osu, "find_exe", lambda *a, **k: install_dir.exe)
    monkeypatch.setattr(osu, "start", lambda c, wait=30, **k: started.append(wait) or False)
    monkeypatch.setattr(osu, "status", lambda c, session=None, find=None: "stopped")
    fake = types.SimpleNamespace(cfg=cfg(start_timeout_s=25), llm=types.SimpleNamespace(check=lambda: (False, "нет")),
                                 emit=lambda ev, **kw: events.append((ev, kw)))
    ok, _ = Assistant._ensure_ollama(fake)
    assert not ok and started == [25.0]
    assert events[0] == ("notice", {"text": "Запускаю Ollama (локальный ИИ)…"})
    assert ("ollama_problem", {"state": "stopped", "models": [], "text": "нет"}) in events
    events.clear()
    fake.cfg = cfg(auto_start=False)
    started.clear()
    Assistant._ensure_ollama(fake)
    assert started == []                                   # auto-start can be switched off


def test_diagnostics_offers_install(monkeypatch):
    from jarvis import diagnostics
    monkeypatch.setattr(osu, "status", lambda c, session=None, find=None: "missing")
    monkeypatch.setattr(diagnostics.requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()))
    (chk,) = diagnostics._ollama(cfg())
    assert not chk.ok and "не установлена" in chk.detail
    monkeypatch.setattr(diagnostics.pu, "IS_WIN", True)
    (chk,) = diagnostics._ollama(cfg())
    assert ("install_ollama", "Установить Ollama") in chk.fixes


def test_postinstall_installs_starts_and_pulls(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("postinstall", Path(__file__).resolve().parents[1] / "installer" / "postinstall.py")
    pi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pi)
    monkeypatch.setattr(pi, "LOG", tmp_path / "install.log")
    calls = []
    monkeypatch.setattr(osu, "find_exe", lambda *a, **k: None)
    monkeypatch.setattr(osu, "install", lambda c, p=None: calls.append("install") or Path("C:/x/ollama.exe"))
    monkeypatch.setattr(osu, "start", lambda c, wait=30, progress=None: calls.append(("start", wait)) or True)
    monkeypatch.setattr(osu, "missing_models", lambda c: ["qwen2.5:7b"])
    monkeypatch.setattr(osu, "pull_missing", lambda c, p=None: calls.append("pull") or [])
    assert pi.setup_ollama() is True and calls == ["install", ("start", 60), "pull"]
    monkeypatch.setattr(osu, "install", lambda c, p=None: (_ for _ in ()).throw(osu.OllamaSetupError("нет сети")))
    assert pi.setup_ollama() is False
    logtext = (tmp_path / "install.log").read_text(encoding="utf-8")
    assert "нет сети" in logtext and "предложит установить Ollama" in logtext
    json.dumps(logtext)
