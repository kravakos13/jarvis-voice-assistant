"""1.0.9: cancel thinking by voice («стоп / отмена / хватит / забей / не надо») while the command is processed,
plus the stop hotkey / tray item (cancel_current). Real brain loop, fake mic / STT / LLM / speaker."""
from __future__ import annotations

import queue
import threading
import time
import types

import numpy as np
import pytest

from jarvis.assistant import Assistant
from jarvis.audio import EnergyVAD
from jarvis.noname import is_cancel

RATE, FL = 16000, 480


class Mic:
    rate, frame_len, frame_ms = RATE, FL, 30

    def __init__(self):
        self.q: "queue.Queue" = queue.Queue()

    def speak(self):
        for f in [np.full(FL, 0.3, np.float32)] * 15 + [np.zeros(FL, np.float32)] * 20:
            self.q.put(f)

    def clear(self):
        pass

    def frames(self, stop_event=None):
        while not stop_event.is_set():
            try:
                yield self.q.get_nowait()
            except queue.Empty:
                time.sleep(0.002)
                yield np.zeros(FL, np.float32)


class Speaker:
    def __init__(self):
        self.said, self.hushed, self.interrupted = [], False, 0
        self.talking = False

    def say(self, text, voice=None, volume=None):
        if not self.hushed:
            self.said.append(text)

    def hush(self):
        self.hushed = True
        self.interrupt()

    def unhush(self):
        self.hushed = False

    def interrupt(self):
        self.interrupted += 1
        self.talking = False

    def active(self, tail_s=0.0):
        return self.talking

    def wait(self, timeout=None):
        pass


class LLM:
    def __init__(self):
        self.cancels = 0
        self.flag = threading.Event()

    def cancel(self):
        self.cancels += 1
        self.flag.set()


def make(stt_results, think_s=6.0, clear_flag_once=False):
    a = Assistant.__new__(Assistant)
    a.cfg = {"assistant": {"tts_tail_ms": 0, "ack_after_s": 0, "voice_cancel": True},
             "audio": {"silence_ms": 300, "min_speech_ms": 150, "frame_ms": 30, "pre_roll_ms": 90}}
    a.stop_event, a._idle = threading.Event(), threading.Event()
    a._idle.set()
    a._commands = queue.Queue()
    a.mic_muted, a.speaker, a.llm = False, Speaker(), LLM()
    a.emit = lambda *x, **k: None
    a.timings = types.SimpleNamespace(finish=lambda tr: None)
    a.router = types.SimpleNamespace(after_turn=lambda: None, ctx=types.SimpleNamespace(extra={}))
    a._voice_trace = lambda c: None
    a._trace, a.STUCK_S = None, 60.0
    res = list(stt_results)
    a.stt = types.SimpleNamespace(last_conf=None)

    def transcribe(audio, language=None):
        text, conf = res.pop(0) if res else ("", None)
        a.stt.last_conf = conf
        return text
    a._transcribe = transcribe
    out = {"finished": None}

    def respond(text):
        t0 = time.monotonic()
        if clear_flag_once:                      # like llm.stream(): clears the cancel flag when it starts
            time.sleep(0.3)
            a.llm.flag.clear()
        while time.monotonic() - t0 < think_s and not a.llm.flag.is_set():
            time.sleep(0.01)
        a.speaker.say("Поздняя фраза ответа.")     # a sentence that the stream still produced
        out["finished"] = time.monotonic() - t0
        return None
    a.respond = respond
    th = threading.Thread(target=a._brain_loop, daemon=True)
    th.start()
    return a, out


def run(a, mic, utter_at=(0.3,)):
    def talk():
        for t in utter_at:
            time.sleep(t)
            mic.speak()
    threading.Thread(target=talk, daemon=True).start()
    t0 = time.monotonic()
    a._execute_voice(mic, "расскажи длинную историю", EnergyVAD(0.05))
    return time.monotonic() - t0


@pytest.mark.parametrize("phrase", ["стоп", "Стоп.", "отмена", "отмени", "хватит", "забей", "не надо",
                                    "Джарвис, стоп!", "джарвис хватит"])
def test_voice_cancel_while_thinking(phrase):
    a, out = make([(phrase, (-0.2, 0.05))])
    mic = Mic()
    dt = run(a, mic)
    a.wait_idle(3)
    assert dt < 4 and out["finished"] is not None and out["finished"] < 3
    assert a.speaker.said == ["Ок, отменил."] and not a.speaker.hushed      # late sentence dropped
    assert a._cancel_req is None and a._idle.is_set()
    a.stop_event.set()


@pytest.mark.parametrize("phrase,conf", [("включи музыку", (-0.2, 0.05)), ("стоп", (-1.4, 0.05)),
                                         ("стоп", (-0.2, 0.8)), ("стоп машина", (-0.1, 0.0)),
                                         ("надо подумать", (-0.1, 0.0))])
def test_other_speech_or_unsure_stop_is_ignored(phrase, conf):
    a, out = make([(phrase, conf)], think_s=1.2)
    dt = run(a, Mic())
    assert out["finished"] >= 1.15 and a.llm.cancels == 0
    assert a.speaker.said == ["Поздняя фраза ответа."] and dt < 4
    a.stop_event.set()


def test_cancel_reasserted_when_stream_clears_flag():
    a, out = make([], think_s=5.0, clear_flag_once=True)
    a.submit("расскажи", "voice")
    time.sleep(0.1)
    assert a.cancel_current(reply="", source="клавиша")
    a.wait_idle(4)
    assert out["finished"] < 2.5 and a.llm.cancels >= 2 and a.speaker.said == []
    a.stop_event.set()


def test_hotkey_while_speaking_and_idle():
    a, _ = make([])
    assert a.cancel_current(reply="", source="клавиша") is False          # nothing to stop
    a.speaker.talking = True
    assert a.cancel_current(reply="", source="клавиша") is True
    assert a.speaker.interrupted == 1 and not a.speaker.hushed and a._cancel_req is None
    a.speaker.say("следующий ответ")
    assert a.speaker.said == ["следующий ответ"]                           # never stuck hushed
    a.stop_event.set()


def test_cancel_listener_does_not_move_close_snapshot():
    calls = []
    a, _ = make([("стоп", (-0.2, 0.05))])
    a.router.ctx.extra["wm"] = types.SimpleNamespace(mark_start=lambda: calls.append("start"),
                                                     commit_utterance=lambda: calls.append("commit"))
    run(a, Mic())
    assert calls == []
    a.stop_event.set()


def test_is_cancel_table():
    assert is_cancel("Стоп.", None) and is_cancel("не надо", (-0.5, 0.2))
    for t in ("стоп музыку", "стопка", "останови видео", "отмени таймер", "хватит играть", ""):
        assert not is_cancel(t, (-0.1, 0.0))


def test_stop_outside_thinking_keeps_media_meaning():
    from jarvis import noname
    assert noname.classify("стоп") == "media"
