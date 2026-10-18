"""Strict wake-word mode: state machine, own-voice suppression, settings migration."""
import threading
import time
import types

import numpy as np
import pytest

from jarvis import config as C
from jarvis.wake import WakeWordDetector
from jarvis.wakeflow import LISTENING, SLEEPING, THINKING, WakeFlow

DET = WakeWordDetector.from_name("Джарвис", ["jarvis", "джарвиз", "жарвис", "джервис"])


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(phrases, cfg=None, awaiting=None, follow_allowed=lambda: True):
    """phrases: what each listen() hears: str (speech) or None (silence until timeout)."""
    log = types.SimpleNamespace(listens=[], said=[], done=[], states=[])
    queue = list(phrases)
    clock = Clock()

    def listen(timeout=None, silence_ms=None, max_s=None):
        log.listens.append({"timeout": timeout, "silence_ms": silence_ms, "max_s": max_s})
        if not queue:
            raise AssertionError("listened more than expected")
        return queue.pop(0)

    def execute(cmd):
        log.done.append(cmd)
        clock.t += 2

    f = WakeFlow(detect=DET.detect, listen=listen, transcribe=lambda a: a, say=log.said.append, execute=execute,
                 emit=log.states.append, awaiting=awaiting or (lambda: False),
                 cfg=dict({"wake_reply": "Слушаю"}, **(cfg or {})), follow_allowed=follow_allowed, clock=clock)
    return f, log, clock


def test_speech_without_name_is_ignored():
    f, log, _ = make(["открой ютуб", "какая погода", "выключи компьютер"])
    assert [f.step() for _ in range(3)] == ["ignored"] * 3
    assert log.done == [] and log.said == []
    assert log.listens[0]["timeout"] is None                      # asleep: waits for the name forever


def test_name_then_one_request_then_sleep():
    f, log, _ = make(["Джарвис", "открой ютуб", "закрой ютуб"])
    assert f.step() == "request"
    assert log.said == ["Слушаю"] and log.done == ["открой ютуб"]
    assert log.listens[1] == {"timeout": 6.0, "silence_ms": 1200, "max_s": 15.0}
    assert log.states == [LISTENING, THINKING, SLEEPING]
    assert f.step() == "ignored"                                  # no follow-up: needs the name again
    assert log.done == ["открой ютуб"]


def test_name_with_command_in_one_phrase_runs_directly():
    f, log, _ = make(["Джарвис, закрой ютуб"])
    assert f.step() == "direct"
    assert log.done == ["закрой ютуб"] and log.said == []         # no "Слушаю"


@pytest.mark.parametrize("phrase", ["джарвиз открой стим", "Jarvis открой стим", "эй жарвис открой стим",
                                    "Джервис, открой стим"])
def test_whisper_variants_of_the_name(phrase):
    f, log, _ = make([phrase])
    assert f.step() == "direct" and log.done == ["открой стим"]


def test_silence_after_slushayu_goes_back_to_sleep_quietly():
    f, log, _ = make(["Джарвис", None, "открой ютуб"])
    assert f.step() == "no_request"
    assert log.said == ["Слушаю"] and log.done == []              # nothing else said
    assert log.states[-1] == SLEEPING
    assert f.step() == "ignored"


def test_repeated_name_after_slushayu_is_stripped():
    f, log, _ = make(["Джарвис", "Джарвис, какая погода"])
    assert f.step() == "request" and log.done == ["какая погода"]


def test_only_name_again_after_slushayu_is_not_a_command():
    f, log, _ = make(["Джарвис", "Джарвис"])
    assert f.step() == "no_request" and log.done == []


def test_custom_timeouts_from_settings():
    f, log, _ = make(["Джарвис", None], cfg={"request_timeout_s": 4, "request_silence_ms": 900, "max_request_s": 10})
    f.step()
    assert log.listens[1] == {"timeout": 4.0, "silence_ms": 900, "max_s": 10.0}


def test_follow_up_window_only_when_enabled():
    f, log, clock = make(["Джарвис, открой ютуб", "сделай погромче", "а теперь потише"], cfg={"follow_up_seconds": 5})
    f.step()
    assert f.step() == "reply" and log.done[-1] == "сделай погромче"
    assert log.listens[1]["timeout"] == pytest.approx(5)
    clock.t += 10                                                 # window expired
    assert f.step() == "ignored"


def test_follow_up_window_closes_on_silence():
    f, log, clock = make(["Джарвис, открой ютуб", None, "потише"], cfg={"follow_up_seconds": 5})
    f.step()
    assert f.step() == "idle"
    assert f.step() == "ignored" and log.listens[2]["timeout"] is None


def test_follow_up_off_in_game_profile():
    f, log, _ = make(["Джарвис, открой ютуб", "потише"], cfg={"follow_up_seconds": 5}, follow_allowed=lambda: False)
    f.step()
    assert f.step() == "ignored"


def test_skill_question_is_answered_without_name():
    """'Отправить?' confirmations still work: the skill waits for the answer explicitly."""
    waiting = {"on": False}
    f, log, _ = make(["Джарвис, передай люмо привет", "да"], awaiting=lambda: waiting["on"])

    def execute(cmd):
        log.done.append(cmd)
        waiting["on"] = cmd != "да"
    f.execute = execute
    assert f.step() == "direct"
    assert f.step() == "reply" and log.done == ["передай люмо привет", "да"]


def test_state_returns_to_sleeping_even_if_execution_fails():
    f, log, _ = make(["Джарвис, сломайся"])

    def boom(cmd):
        raise RuntimeError("x")
    f.execute = boom
    with pytest.raises(RuntimeError):
        f.step()
    assert f.state == SLEEPING and log.states[-1] == SLEEPING


def test_default_config_is_strict():
    a = C.DEFAULTS["assistant"]
    assert a["follow_up_seconds"] == 0 and a["mode"] == "wake"
    assert a["request_timeout_s"] == 6 and a["request_silence_ms"] == 1200 and a["max_request_s"] == 15
    import yaml
    from pathlib import Path
    y = yaml.safe_load(Path(C.PROJECT_ROOT, "config.yaml").read_text(encoding="utf-8"))["assistant"]
    assert y["follow_up_seconds"] == 0 and y["request_timeout_s"] == 6


def test_migration_turns_old_pinned_follow_up_off_but_keeps_user_choice():
    d, ch = C.migrate_local({"config_version": 3, "assistant": {"follow_up_seconds": 6, "name": "Джарвис"}})
    assert ch and "follow_up_seconds" not in d["assistant"] and d["config_version"] == C.CONFIG_VERSION
    d, _ = C.migrate_local({"config_version": 3, "assistant": {"follow_up_seconds": 10}})
    assert d["assistant"]["follow_up_seconds"] == 10


# ---------------------------------------------------------------- own voice is never heard
def test_speaker_active_window():
    from jarvis.tts import Speaker
    sp = Speaker.__new__(Speaker)
    sp.engine = object()
    import queue
    sp._q = queue.Queue()
    sp._last_end = 0.0
    assert not sp.active(0.5)
    sp._q.put(("x", None, 1.0))
    assert sp.active(0.5)                                         # queued / playing
    sp._q.get()
    sp._q.task_done()
    sp._last_end = time.monotonic()
    assert sp.active(0.5) and not sp.active(0.0)                  # echo tail


def test_mic_hears_silence_while_jarvis_speaks():
    from jarvis.assistant import Assistant
    a = Assistant.__new__(Assistant)
    a.cfg = {"assistant": {"tts_tail_ms": 500}}
    a.mic_muted, a.stop_event = False, threading.Event()
    a.emit = lambda *x, **k: None                                 # type: ignore[method-assign]
    speaking = iter([True, True, False])
    a.speaker = types.SimpleNamespace(active=lambda tail: next(speaking))
    loud = np.full(480, 0.5, dtype=np.float32)
    mic = types.SimpleNamespace(frames=lambda stop: iter([loud, loud, loud]))
    out = list(a._frames(mic))
    assert len(out) == 3                                          # timeouts keep ticking
    assert not out[0].any() and not out[1].any() and out[2].any()


def test_own_voice_never_wakes_jarvis():
    """Integration: the flow fed through _frames + segment_utterance with Jarvis speaking "Джарвис"."""
    from jarvis.assistant import Assistant
    from jarvis.audio import segment_utterance

    class Vad:
        def is_speech(self, frame, rate):
            return bool(np.abs(frame).max() > 0.1)

    a = Assistant.__new__(Assistant)
    a.cfg = {"assistant": {"tts_tail_ms": 500}}
    a.mic_muted, a.stop_event = False, threading.Event()
    a.emit = lambda *x, **k: None                                 # type: ignore[method-assign]
    a.speaker = types.SimpleNamespace(active=lambda tail: True)   # Jarvis talks the whole time
    loud = np.full(480, 0.5, dtype=np.float32)
    mic = types.SimpleNamespace(frames=lambda stop: iter([loud] * 200))
    assert segment_utterance(a._frames(mic), Vad(), 16000) is None
