"""Memory, settings persistence, updater (mocked GitHub), crash reports, diagnostics messages."""
import yaml

from jarvis import config, diagnostics, updater
from jarvis.memory import Memory


def test_memory_crud(tmp_path):
    m = Memory({"use_embeddings": False, "min_score": 0.1}, llm=None, db_path=tmp_path / "m.sqlite3")
    a = m.add("у мистера Флоки кот по имени Барсик")
    m.add("любимая игра — Counter-Strike 2")
    assert [x.text for x in m.search("как зовут кота")][0].startswith("у мистера Флоки кот")
    assert m.update(a.id, "кота зовут Мурзик") and "Мурзик" in m.all()[0].text + m.all()[-1].text
    assert m.delete(a.id) and len(m.all()) == 1
    assert m.clear() == 1 and m.all() == []


def test_settings_save_atomic_and_broken(tmp_path, monkeypatch):
    p = tmp_path / "settings.local.yaml"
    monkeypatch.setattr(config, "LOCAL_SETTINGS", p)
    p.write_text("assistant: [broken", encoding="utf-8")
    config.save_local_settings({"tts": {"volume": 0.5}})
    assert (tmp_path / "settings.local.yaml.broken").exists()
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["tts"]["volume"] == 0.5 and data["config_version"] == config.CONFIG_VERSION
    config.save_local_settings({"gui": {"opacity": 0.8}})
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["tts"]["volume"] == 0.5 and data["gui"]["opacity"] == 0.8
    assert not list(tmp_path.glob("*.tmp"))


class _Resp:
    def __init__(self, code, js):
        self.status_code, self._js = code, js

    def json(self):
        return self._js

    def raise_for_status(self):
        pass


def test_updater_check_and_skip(monkeypatch, tmp_path):
    rel = {"tag_name": "v9.0.0", "body": "• новое", "assets": [
        {"name": "JarvisSetup.exe", "browser_download_url": "https://github.com/x/y/releases/download/v9.0.0/JarvisSetup.exe"}]}
    calls = []
    monkeypatch.setattr(updater.requests, "get", lambda url, **kw: calls.append(url) or _Resp(200, rel))
    monkeypatch.setattr(updater, "_stamp_file", lambda: tmp_path / "stamp.txt")
    cfg = {"update": {"github_repo": "kravakos13/jarvis-voice-assistant", "check_on_start": True}}
    info = updater.check(cfg)
    assert info["status"] == "available" and info["url"].endswith("JarvisSetup.exe")
    assert calls[0] == "https://api.github.com/repos/kravakos13/jarvis-voice-assistant/releases/latest"
    assert updater.auto_check(cfg)["version"] == "v9.0.0"
    assert updater.auto_check(cfg) is None                    # at most once a day
    (tmp_path / "stamp.txt").unlink()
    assert updater.auto_check({"update": {**cfg["update"], "skip_version": "v9.0.0"}}) is None
    assert updater.auto_check({"update": {"check_on_start": False}}) is None
    rel["tag_name"] = "v0.9.0"
    assert updater.check(cfg)["status"] == "latest"
    assert updater.check({"update": {"github_repo": "bad repo"}})["status"] == "error"


def test_crash_report(tmp_path, monkeypatch):
    from jarvis import crash
    monkeypatch.setattr(crash, "crash_dir", lambda: tmp_path)
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        p = crash.write_report(type(e), e, e.__traceback__, "test")
    txt = p.read_text(encoding="utf-8")
    assert "отчёт о сбое" in txt and "RuntimeError: boom" in txt


def test_failure_messages_russian():
    assert "pycaw" in diagnostics.explain_failure("No module named 'pycaw'")
    assert "Настройки" in diagnostics.explain_failure("api_id is required")

    class R:
        failed = {"telegram": "No module named 'telethon'"}

    class A:
        router = R()
    checks = diagnostics._modules(A())
    assert checks and "Telegram" in checks[0].title and "telethon" in checks[0].detail


def test_fuzzy_mode_names():
    from jarvis.skills.modes_skill import fuzzy_mode
    assert fuzzy_mode("включи ничное режим") == "ночной"
    assert fuzzy_mode("игравой режим") == "игровой"
    assert fuzzy_mode("спящий режим") is None and fuzzy_mode("режим полёта") is None
