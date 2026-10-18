"""1.0.7: media/app control verbs on the fast path (no LLM)."""
from pathlib import Path

import pytest

import jarvis.platform_utils as pu
from jarvis import uia
from jarvis.config import load_config
from jarvis.router import Router
from jarvis.skills.base import SkillContext


@pytest.fixture
def router(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cfg = load_config(use_local=False)
    cfg["alerts"]["enabled"] = False
    keys = []
    monkeypatch.setattr(pu, "press_media_key", lambda a, times=1: keys.append(a) or True)
    r = Router(SkillContext(cfg=cfg, llm=None, say=lambda *a, **k: None, notify=lambda *a: None,
                            project_root=Path(".")))
    med = next(s for s in r.skills if s.name == "media")
    med.media.play_music = lambda q: f"MUSIC {q}"
    med.media.youtube_search = lambda q, play_first=False: f"YT {q}"
    med.media.picker.open = lambda p: f"PICK {p.kind} {p.n}"
    r.keys = keys
    return r


def run(r, phrase):
    r.keys.clear()
    res = r.handle(phrase)
    r.pending_confirm = r.pending_expect = None
    return r.last_skill if res else None, (res.reply if res else ""), list(r.keys)


@pytest.mark.parametrize("phrase,key", [
    ("пауза", "play_pause"), ("поставь на паузу", "play_pause"), ("на паузу", "play_pause"),
    ("продолжи", "play_pause"), ("продолжай", "play_pause"), ("воспроизведи", "play_pause"),
    ("сними с паузы", "play_pause"), ("следующее", "next"), ("дальше", "next"), ("предыдущее", "previous"),
    ("громче", "volume_up"), ("тише", "volume_down"), ("сделай потише", "volume_down"),
    ("вруби погромче", "volume_up"), ("без звука", "mute"), ("выключи звук", "mute"),
])
def test_playback_keys_without_llm(router, phrase, key):
    skill, _, keys = run(router, phrase)
    assert skill in ("media", "system") and keys and keys[-1] == key


@pytest.mark.parametrize("playing,expect", [(True, ("core", ["play_pause"])), (False, ("core", []))])
def test_bare_stop_pauses_media_when_playing_else_exits(router, monkeypatch, playing, expect):
    monkeypatch.setattr(uia, "media_playing", lambda: playing)
    skill, reply, keys = run(router, "стоп")
    assert (skill, keys) == expect
    assert ("паузу" in reply) if playing else ("отключаюсь" in reply)


def test_bare_vykluchi_is_media_only_when_playing(router, monkeypatch):
    monkeypatch.setattr(uia, "media_playing", lambda: True)
    assert run(router, "выключи")[2] == ["play_pause"]
    monkeypatch.setattr(uia, "media_playing", lambda: False)
    assert run(router, "выключи")[2] == []                    # nothing plays: not a toggle that would START music


@pytest.mark.parametrize("phrase", ["выключи музыку", "выключи видео", "выруби эту песню", "останови видео",
                                    "поставь на паузу видео"])
def test_named_media_stop(router, monkeypatch, phrase):
    monkeypatch.setattr(uia, "media_playing", lambda: True)
    skill, reply, keys = run(router, phrase)
    assert skill == "media" and keys == ["play_pause"] and "паузу" in reply
    monkeypatch.setattr(uia, "media_playing", lambda: False)
    skill, reply, keys = run(router, phrase)
    assert keys == []                                  # never a toggle that would START playback
    if phrase.startswith(("выключи", "выруби")):
        assert skill == "windows"                      # idle: 1.0.4 behaviour, close the YT Music app (verified)
    else:
        assert reply == "Сейчас ничего не играет."


def test_smart_home_only_with_device_words(router, monkeypatch):
    monkeypatch.setattr(uia, "media_playing", lambda: True)
    assert run(router, "выключи свет")[0] == "smarthome"
    assert run(router, "выключи реле 2")[0] == "smarthome"
    assert run(router, "включи второе")[0] == "media"
    assert run(router, "выключи")[0] != "smarthome"


def test_open_close_play(router):
    med = next(s for s in router.skills if s.name == "media")
    played = []
    med.media.play_music = played.append
    res = router.handle("включи котиков")
    res.then()
    assert router.last_skill == "media" and played == ["котиков"]
    assert run(router, "открой ютуб")[0] == "media"
    assert run(router, "закрой ютуб")[0] == "windows"          # verified close with the existing guard
    assert run(router, "выключи ютуб")[0] == "windows"
    res = router.handle("включи второе видео")
    assert router.last_skill == "media" and res.then() == "PICK index 2"
