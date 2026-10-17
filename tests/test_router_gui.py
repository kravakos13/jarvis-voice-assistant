from pathlib import Path

import pytest

from jarvis.config import load_config


class FakeLLM:
    model = "fake"
    keep_alive = "30m"

    def complete(self, messages, **kw):
        return "Hello, how are you?" if "английский" in messages[-1]["content"] else "Привет"

    def unload_all(self):
        pass


@pytest.fixture(scope="module")
def router():
    from jarvis.router import Router
    from jarvis.skills.base import SkillContext
    cfg = load_config(use_local=False)
    cfg["alerts"]["enabled"] = False         # no network polling in tests
    cfg["monitor"]["interval_s"] = 3600
    said = []
    ctx = SkillContext(cfg=cfg, llm=FakeLLM(), say=lambda t, *a: said.append(t), notify=lambda *a: None,
                       project_root=Path("."))
    r = Router(ctx)
    r._said = said
    return r


def test_all_modules_load_or_degrade(router):
    # on Linux only Windows-specific modules (audio_devices -> pycaw) may be disabled
    assert set(router.failed) <= {"audio_devices", "telegram", "smarthome"}, router.failed


@pytest.mark.parametrize("phrase,skill_reply", [
    ("переведи на английский привет как дела", "Hello, how are you?"),
    ("какая температура компьютера", None),
    ("диктовка", "Диктую"),
    ("найди файл отчёт", None),
])
def test_routes(router, phrase, skill_reply):
    r = router.handle(phrase)
    assert r is not None, phrase
    if skill_reply:
        assert skill_reply in (r.reply or ""), r.reply
    d = router.by_name.get("dictate")
    if d:
        d.active = False


def test_translator_mode(router):
    r = router.handle("режим переводчика на английский")
    assert "переводчика" in r.reply
    assert router.awaiting_input and router.stt_language() is None   # auto language detection
    r = router.handle("как дела")
    assert r.voice == "en_US-ryan-medium"
    r = router.handle("стоп перевод")
    assert "выключен" in r.reply and not router.awaiting_input


def test_weather_not_hijacked_by_pc_temp(router):
    r = router.handle("какая температура на улице")
    assert r is None or "процессор" not in (r.reply or "")


def test_settings_dialog_offscreen(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    import jarvis.config as config
    import jarvis.gui.settings as st
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "settings.local.yaml")
    monkeypatch.setattr(st, "LOCAL_SETTINGS", tmp_path / "settings.local.yaml")
    saved = {}
    monkeypatch.setattr(st, "save_local_settings", lambda ch: saved.update(ch))
    cfg = load_config(use_local=False)
    dlg = st.SettingsDialog(cfg)
    assert dlg.tabs.count() == 13
    ch = dlg.collect()
    assert dlg.validate(ch) == [], dlg.validate(ch)
    assert ch["assistant"]["name"] == "Джарвис" and ch["gui"]["hotkey_mic"] == "ctrl+alt+m"
    assert "{name}" in ch["llm"]["system_prompt"]
    dlg._set("profiles.night_schedule.start", "25:99")
    dlg._set("gui.hotkey_mic", "ctrl+alt+j")
    errs = dlg.validate(dlg.collect())
    assert any("ЧЧ:ММ" in e for e in errs) and any("одинаковое" in e for e in errs)
    dlg._set("profiles.night_schedule.start", "23:00")
    dlg._set("gui.hotkey_mic", "ctrl+alt+m")
    dlg._set("tts.length_scale", 1.25)
    monkeypatch.setattr(dlg, "_aliases_path", tmp_path / "aliases.yaml")
    monkeypatch.setattr(dlg, "_devices_path", tmp_path / "devices.yaml")
    dlg._save()
    assert saved["tts"]["length_scale"] == 1.25
    assert [k for k in dlg.changed_keys if k != "smarthome.devices"] == ["tts.length_scale"], dlg.changed_keys
    dlg._stop_meter()
