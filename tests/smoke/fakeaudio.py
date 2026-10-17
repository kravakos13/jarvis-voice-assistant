"""Mocked audio for smoke tests: microphone plays WAV files (with silence around), player just sleeps."""
import queue, threading, time, wave
import numpy as np
import jarvis.audio as A

FILES = []          # set by the caller
PLAYED = []
START_DELAY = 10
GAP_CHUNKS = 9


def _load(path):
    with wave.open(path) as w:
        rate = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    return A.resample(a, rate, 16000) if rate != 16000 else a


class FakeMic(A.Microphone):
    def start(self):
        self._dev_rate = self.rate
        def feed():
            time.sleep(START_DELAY)
            sil = np.zeros(int(16000 * 1.5), np.float32)
            noise = lambda n: (np.random.randn(n) * 0.0005).astype(np.float32)
            seq = [noise(len(sil))]
            for f in FILES:
                seq += [_load(f)] + [noise(len(sil))] * GAP_CHUNKS
            for chunk in seq:
                for i in range(0, len(chunk), self.frame_len):
                    if self._stream is None:
                        return
                    self._q.put(chunk[i:i + self.frame_len])
                    time.sleep(self.frame_ms / 1000)
            while self._stream is not None:
                self._q.put(noise(self.frame_len)); time.sleep(self.frame_ms / 1000)
        self._stream = True
        threading.Thread(target=feed, daemon=True).start()
        return self

    def stop(self):
        self._stream = None


class FakePlayer(A.Player):
    def __init__(self, device=None):
        self.device = None; self._lock = threading.Lock(); self._stopped = threading.Event()

    def play(self, audio, rate, *a, **k):
        PLAYED.append(len(audio) / rate)
        time.sleep(min(len(audio) / rate, 4))

    def stop(self):
        self._stopped.set()


def install():
    A.Microphone = FakeMic
    A.Player = FakePlayer
    import sys
    for name, mod in list(sys.modules.items()):
        if name.startswith("jarvis") and mod is not None:
            if getattr(mod, "Microphone", None) is not None and mod is not A:
                mod.Microphone = FakeMic
            if getattr(mod, "Player", None) is not None and mod is not A:
                mod.Player = FakePlayer
