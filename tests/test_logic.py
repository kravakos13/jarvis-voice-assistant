import datetime as dt
import zipfile
from pathlib import Path

import numpy as np
import pytest

from jarvis.config import load_config, raw_config, render_prompt, update_inplace


@pytest.fixture(scope="module")
def cfg():
    return load_config(use_local=False)


def test_config_defaults(cfg):
    assert cfg["assistant"]["name"] == "Джарвис"
    assert cfg["update"]["github_repo"] == "kravakos13/jarvis-voice-assistant"
    assert cfg["telegram"]["enabled"] is False and cfg["telegram"]["notify_new_messages"] is False
    assert "{name}" not in cfg["llm"]["system_prompt"] and "Джарвис" in cfg["llm"]["system_prompt"]
    raw = raw_config(use_local=False)
    assert "{name}" in raw["llm"]["system_prompt_raw"]
    raw["assistant"]["name"] = "Пятница"
    assert "Пятница" in render_prompt(raw)


def test_update_inplace_keeps_refs():
    base = {"monitor": {"gpu_temp_warn": 83}}
    ref = base["monitor"]
    update_inplace(base, {"monitor": {"gpu_temp_warn": 70}})
    assert ref["gpu_temp_warn"] == 70


def test_wake_variants():
    from jarvis.wake import WakeWordDetector
    d = WakeWordDetector.from_name("Джарвис", ["jarvis"])
    assert d.detect("Джарвис, включи музыку").command.startswith("включи")
    assert d.detect("жарвис").detected
    assert d.detect("Джарвису привет").detected
    assert not d.detect("включи музыку").detected


def test_punctuation():
    from jarvis.skills.dictate_skill import apply_punctuation
    assert apply_punctuation("привет запятая как дела вопросительный знак") == "Привет, как дела?"
    assert apply_punctuation("раз точка два новая строка три") == "Раз. Два\nТри"


def test_modes_window():
    from jarvis.skills.modes_skill import MODE_RE, in_window
    assert in_window("23:30", "23:00", "07:00") and in_window("03:00", "23:00", "07:00")
    assert not in_window("12:00", "23:00", "07:00")
    assert MODE_RE.search("включи игровой режим").group(1) == "игровой"


def test_alerts_text():
    from jarvis.skills.alerts_skill import AlertsSkill, ru_place
    assert ru_place("Сумська область") == "Сумской области"
    assert ru_place("Шосткинський район") == "Шосткинском районе"
    sk = AlertsSkill.__new__(AlertsSkill)
    sk.oblast = "Сумська область"
    assert sk.describe(True, ["Сумська область"], ["дрони"]).startswith("Внимание! Воздушная тревога в Сумской области")
    assert sk.describe(False, [], []) == "Отбой воздушной тревоги в Сумской области."


def test_hotkey_conflicts():
    from jarvis.gui.widgets import hotkey_conflicts, normalize_hotkey
    assert normalize_hotkey("Alt + Ctrl + M") == "ctrl+alt+m"
    errs = hotkey_conflicts({"a": "ctrl+alt+m", "b": "alt+ctrl+m", "c": "alt+tab"})
    assert any("одинаковое" in e for e in errs) and any("занято" in e for e in errs)
    assert hotkey_conflicts({"a": "ctrl+alt+m", "b": "ctrl+alt+j"}) == []


def test_updater_versions(monkeypatch):
    from jarvis import updater

    class R:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data
    rel = {"tag_name": "v1.2.0", "body": "## 1.2.0\n- новое", "assets": [
        {"name": "JarvisSetup.exe", "browser_download_url": "https://github.com/x/y/releases/download/v1.2.0/JarvisSetup.exe"}]}
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: R(rel))
    cfg = {"update": {"github_repo": "kravakos13/jarvis-voice-assistant", "check_on_start": True}}
    info = updater.check(cfg)
    assert info["status"] == "available" and info["url"].endswith("JarvisSetup.exe")
    assert updater.auto_check(cfg)["version"] == "v1.2.0"
    assert updater.auto_check(cfg) is None                       # once per day
    updater._stamp_file().unlink()
    cfg["update"]["skip_version"] = "v1.2.0"
    assert updater.auto_check(cfg) is None                       # skipped version
    rel["tag_name"] = "v1.0.0"
    assert updater.check(cfg)["status"] == "latest"
    assert updater.check({"update": {"github_repo": ""}})["status"] == "not_configured"
    with pytest.raises(ValueError):
        updater.download("https://evil.example.com/x.exe")


def test_backup_roundtrip(cfg, tmp_path):
    import sqlite3
    from jarvis import backup, platform_utils as pu
    notes = pu.data_dir("Notes")
    (notes / "n1.txt").write_text("заметка", encoding="utf-8")
    db = pu.data_dir("") / "planner.sqlite3"
    c = sqlite3.connect(db)
    c.execute("create table if not exists t(x)")
    c.execute("insert into t values (1)")
    c.commit()
    (pu.data_dir("") / "telegram.session").write_text("secret")
    p = backup.make_backup(cfg, "test")
    names = zipfile.ZipFile(p).namelist()
    assert "data/Notes/n1.txt" in names and "data/planner.sqlite3" in names
    assert not any("telegram" in n for n in names)
    (notes / "n1.txt").write_text("изменено", encoding="utf-8")
    backup.stage_restore(p)
    assert backup.apply_pending_restore()
    assert (notes / "n1.txt").read_text(encoding="utf-8") == "заметка"
    c.close()
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.txt", "x")
    with pytest.raises(ValueError):
        backup.stage_restore(bad)


def test_diagnostics_quick(cfg):
    from jarvis import diagnostics
    checks = diagnostics.run(cfg, None, quick=True)
    ids = {c.id for c in checks}
    assert {"mic", "whisper", "ollama", "tts", "output", "internet"} <= ids
    assert isinstance(diagnostics.spoken_summary(checks), str)


def test_voicefx_and_pitch(cfg):
    from jarvis.voicefx import apply_fx
    rate = 22050
    x = (0.3 * np.sin(2 * np.pi * 200 * np.arange(rate) / rate)).astype(np.float32)
    y = apply_fx(x, rate, 0.35)
    assert y.dtype == np.float32 and np.isfinite(y).all() and np.abs(y).max() <= 1.0
    assert abs(len(y) - len(x)) < rate


def test_planner_parse():
    from jarvis.planner import parse_task
    now = dt.datetime(2026, 10, 17, 12, 0)
    t = parse_task("завтра в 9 напомни позвонить маме", now=now)
    assert t and t.due == dt.datetime(2026, 10, 18, 9, 0) and "позвонить маме" in t.text
    t = parse_task("через 20 минут проверить духовку", now=now)
    assert t and t.due == now + dt.timedelta(minutes=20)


def test_shutdown_timer_confirm(cfg):
    from jarvis.skills.base import SkillContext
    from jarvis.skills.system_skill import SystemSkill
    ctx = SkillContext(cfg=cfg, llm=None, say=lambda *a, **k: None, notify=lambda *a: None, project_root=Path("."))
    sk = SystemSkill(ctx)
    r = sk.match("выключи компьютер через 30 минут", "выключи компьютер через 30 минут")
    assert r and r.confirm and "30 минут" in r.confirm.prompt
