"""1.0.7: the voice loop can never fall asleep for good (exceptions, phantom text, dead mic, stuck TTS)."""
import queue
import threading
import time
import types

import numpy as np

from jarvis import audio as A
from jarvis.assistant import Assistant
from jarvis.wake import WakeWordDetector

RATE, FL = 16000, 480


def utterance():
    return [np.full(FL, 0.3, np.float32)] * 20 + [np.zeros(FL, np.float32)] * 40


class FakeMic:
    """Each open gets the next script; a script item 'crash' raises like a vanished device."""
    rate, frame_len, frame_ms = RATE, FL, 30
    opened = 0

    def __init__(self, scripts, default="silence"):
        self.scripts, self.default = scripts, default

    def __enter__(self):
        FakeMic.opened += 1
        return self

    def __exit__(self, *e):
        return False

    start = stop = pause = clear = lambda self: None

    def frames(self, stop_event=None):
        while not stop_event.is_set():
            item = self.scripts.get() if not self.scripts.empty() else self.default
            if item == "crash":
                raise OSError("PortAudio: device unavailable")
            for f in (utterance() if item == "speech" else [np.zeros(FL, np.float32)] * 10):
                yield f
            time.sleep(0.005)


def make(texts, scripts, default="silence"):
    a = Assistant.__new__(Assistant)
    a.cfg = {"assistant": {"tts_tail_ms": 0}, "audio": {"vad": "energy", "energy_threshold": 0.05,
                                                        "silence_ms": 300, "min_speech_ms": 150}}
    a.stop_event, a._idle, a._ptt_active = threading.Event(), threading.Event(), threading.Event()
    a._idle.set()
    a.mic_muted, a.mode, a.profile, a.speaker, a.name = False, "wake", "work", None, "Джарвис"
    a._said, a.on_event = [], lambda *x, **k: None
    a.wake = WakeWordDetector.from_name("Джарвис", [])
    a.router = types.SimpleNamespace(stt_language=lambda: "ru", awaiting_input=False, awaiting_timeout=None)
    a.greet = lambda: None
    a.STUCK_S, a.DEAF_S = 60.0, 15.0
    states, done = [], []
    a.emit = lambda ev, **d: (states.append(d.get("state")) if ev == "state" else None,
                              Assistant.emit.__get__(a)(ev, **d) if ev == "state" else None)
    a._execute_voice = lambda mic, cmd, *rest: done.append(cmd)
    it = iter(texts)

    def tr(audio, language=None):
        x = next(it, "")
        if isinstance(x, Exception):
            raise x
        return x
    a.stt = types.SimpleNamespace(transcribe=tr)
    a._new_mic = lambda: FakeMic(scripts, default)
    return a, states, done


def test_stress_exceptions_phantoms_and_dead_mic_return_to_wake_waiting():
    scripts = queue.Queue()
    for x in ["speech", "speech", "crash", "speech", "speech", "speech", "speech"]:
        scripts.put(x)
    texts = [RuntimeError("cuBLAS boom"), "привет", "Джарвис, привет", MemoryError("oom"),
             "Продолжение следует...", "Джарвис, открой блокнот"]
    a, states, done = make(texts, scripts, default="speech")
    t = threading.Thread(target=a.run_voice, daemon=True)
    t.start()
    deadline = time.time() + 15
    while time.time() < deadline and "открой блокнот" not in done:
        time.sleep(0.05)
    assert "открой блокнот" in done                      # still answers after all of that
    assert done[0] == "привет"                          # «Джарвис, привет» was real (had the name); bare one ignored
    assert t.is_alive() and a._voice_alive
    time.sleep(0.2)
    assert states[-1] == "sleeping"
    a.stop_event.set()
    t.join(3)
    assert not a._voice_alive


def test_restart_listening_request_reopens_session():
    scripts = queue.Queue()
    a, states, done = make([], scripts)
    FakeMic.opened = 0
    t = threading.Thread(target=a.run_voice, daemon=True)
    t.start()
    time.sleep(0.3)
    assert a.restart_listening() is True
    time.sleep(0.5)
    assert FakeMic.opened >= 2
    a.stop_event.set()
    t.join(3)


class StuckSpeaker:
    def __init__(self):
        self.interrupted = 0

    def stuck(self):
        return self.interrupted == 0

    def interrupt(self):
        self.interrupted += 1

    def active(self, tail=0):
        return False


def test_watchdog_resets_stuck_tts_state_and_deaf_mic():
    a, _, _ = make([], queue.Queue())
    a._voice_alive, a._relisten, a._hb = True, threading.Event(), 100.0
    a.speaker = StuckSpeaker()
    assert a._watch_once(101.0) == "tts" and a.speaker.interrupted == 1
    a._vstate = ("speaking", 100.0)
    assert a._watch_once(100.0 + 61) == "stuck" and a._relisten.is_set()
    a._relisten.clear()
    a._vstate = ("thinking", 100.0)                     # 1.0.8: an LLM turn (90 s) + one retry is not «stuck»
    assert a._watch_once(100.0 + 61) is None
    assert a._watch_once(100.0 + a._stuck_limit("thinking") + 1) == "stuck" and a._relisten.is_set()
    a._relisten.clear()
    a._vstate, a._hb = ("sleeping", 200.0), 200.0
    assert a._watch_once(210.0) is None
    assert a._watch_once(216.0) == "deaf" and a._relisten.is_set()
    a.mic_muted = True                                  # privacy mute is respected
    assert a._watch_once(999.0) is None


def test_tts_queue_never_stays_busy_when_callback_raises():
    from jarvis import tts as T
    sp = T.Speaker.__new__(T.Speaker)
    sp._q, sp._interrupt, sp._last_end, sp._started = queue.Queue(), threading.Event(), 0.0, 0.0
    sp.engine = types.SimpleNamespace(say=lambda text: None)
    sp.player, sp.volume = None, 1.0

    def boom(ev, **k):
        raise RuntimeError("GUI gone")
    sp.on_event = boom
    th = threading.Thread(target=sp._run, daemon=True)
    th.start()
    sp._q.put(("привет", None, 1.0))
    sp._q.put(None)
    th.join(3)
    assert sp._q.unfinished_tasks == 0 and not sp.active(0)
    sp._q.put(("x", None, 1.0))                         # a hung sentence for > 60 s stops counting as speech
    sp._started = time.monotonic() - 61
    assert sp.stuck() and not sp.active(0)


class DyingStream:
    active = False

    def stop(self):
        pass

    def close(self):
        pass


def test_microphone_reopens_a_dead_stream_and_keeps_yielding(monkeypatch):
    mic = A.Microphone.__new__(A.Microphone)
    mic._q, mic.rate, mic._dev_rate, mic.frame_ms, mic.frame_len = queue.Queue(), RATE, RATE, 30, FL
    mic._buf, mic._stream, mic.wanted, mic.reopened, mic._device_spec, mic.device = \
        np.zeros(0, np.float32), DyingStream(), True, 0, None, None
    mic.STALL_S = 0.3
    opened = []
    monkeypatch.setattr(A.Microphone, "start", lambda self: (opened.append(1), setattr(self, "_stream", DyingStream()),
                                                            self._q.put(np.ones(FL, np.float32)))[-1])
    stop = threading.Event()
    got = []
    for f in mic.frames(stop):
        got.append(f)
        if f.max() == 1.0:
            break
    assert opened and mic.reopened == 1 and len(got) > 1    # silence while stalled, then real audio again
    mic.wanted = False                                       # privacy pause: never re-opened
    n = 0
    for _f in mic.frames(stop):
        n += 1
        if n > 20:
            break
    assert mic.reopened == 1
