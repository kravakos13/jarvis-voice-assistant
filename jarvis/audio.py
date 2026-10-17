"""Microphone capture with VAD (end-of-utterance detection) and playback."""
from __future__ import annotations

import collections
import logging
import queue
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

import numpy as np

log = logging.getLogger(__name__)

try:  # sounddevice needs PortAudio; on Windows it ships inside the wheel
    import sounddevice as sd
except Exception as e:  # pragma: no cover - depends on system
    sd = None
    _SD_ERROR = e
else:
    _SD_ERROR = None


def require_sd():
    if sd is None:
        raise RuntimeError(f"sounddevice/PortAudio недоступен: {_SD_ERROR}")
    return sd


# --------------------------------------------------------------------------- devices
def list_devices() -> str:
    s = require_sd()
    lines = []
    hostapis = s.query_hostapis()
    for idx, dev in enumerate(s.query_devices()):
        api = hostapis[dev["hostapi"]]["name"]
        kinds = []
        if dev["max_input_channels"] > 0:
            kinds.append(f"in:{dev['max_input_channels']}")
        if dev["max_output_channels"] > 0:
            kinds.append(f"out:{dev['max_output_channels']}")
        lines.append(f"[{idx:2d}] {dev['name']}  ({api}, {', '.join(kinds)}, {int(dev['default_samplerate'])} Hz)")
    try:
        din, dout = s.default.device
        lines.append(f"\nПо умолчанию: вход={din}, выход={dout}")
    except Exception:
        pass
    return "\n".join(lines)


def resolve_device(spec, kind: str):
    """spec: None | int | str (substring of device name). kind: 'input'|'output'."""
    if spec is None or spec == "":
        return None
    if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().isdigit()):
        return int(spec)
    s = require_sd()
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    for idx, dev in enumerate(s.query_devices()):
        if dev[key] > 0 and str(spec).lower() in dev["name"].lower():
            return idx
    log.warning("Устройство '%s' (%s) не найдено, используется устройство по умолчанию", spec, kind)
    return None


# --------------------------------------------------------------------------- helpers
def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or audio.size == 0:
        return audio.astype(np.float32, copy=False)
    n_dst = int(round(audio.size * dst_rate / src_rate))
    x_src = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
    x_dst = np.linspace(0.0, 1.0, num=n_dst, endpoint=False)
    return np.interp(x_dst, x_src, audio).astype(np.float32)


def float_to_pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def read_wav(path: str | Path, target_rate: int = 16000) -> np.ndarray:
    """Read a PCM WAV file to mono float32 at target_rate."""
    with wave.open(str(path), "rb") as wf:
        rate, ch, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {width}")
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return resample(data, rate, target_rate)


def write_wav(path: str | Path, audio: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(float_to_pcm16(audio))


# --------------------------------------------------------------------------- VAD
class EnergyVAD:
    """Very simple RMS based VAD with adaptive noise floor."""

    def __init__(self, threshold: float = 0.015):
        self.base_threshold = threshold
        self.noise = None

    def is_speech(self, frame: np.ndarray, rate: int) -> bool:
        rms = float(np.sqrt(np.mean(np.square(frame)))) if frame.size else 0.0
        if self.noise is None:
            self.noise = rms
        threshold = max(self.base_threshold, self.noise * 3.0)
        speech = rms > threshold
        if not speech:  # track background noise slowly
            self.noise = 0.95 * self.noise + 0.05 * rms
        return speech


class WebRtcVAD:
    def __init__(self, aggressiveness: int = 2):
        import webrtcvad  # provided by webrtcvad-wheels
        self.vad = webrtcvad.Vad(int(aggressiveness))

    def is_speech(self, frame: np.ndarray, rate: int) -> bool:
        return self.vad.is_speech(float_to_pcm16(frame), rate)


def make_vad(cfg: dict):
    kind = str(cfg.get("vad", "webrtc")).lower()
    if kind == "webrtc":
        try:
            return WebRtcVAD(cfg.get("vad_aggressiveness", 2))
        except Exception as e:
            log.warning("webrtcvad недоступен (%s), переключаюсь на energy VAD", e)
    return EnergyVAD(cfg.get("energy_threshold", 0.015))


def segment_utterance(
    frames: Iterable[np.ndarray],
    vad,
    rate: int,
    frame_ms: int = 30,
    silence_ms: int = 800,
    min_speech_ms: int = 250,
    max_utterance_s: float = 15,
    pre_roll_ms: int = 300,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Optional[np.ndarray]:
    """Consume frames until one complete utterance is found.

    Returns float32 audio of the utterance or None if the frame source ended /
    should_stop() returned True before any speech was found.
    Pure logic (no audio device) so it can be unit tested.
    """
    pre = collections.deque(maxlen=max(1, pre_roll_ms // frame_ms))
    voiced: list[np.ndarray] = []
    triggered = False
    speech_frames = 0
    silence_frames = 0
    need_silence = max(1, silence_ms // frame_ms)
    need_speech = max(1, min_speech_ms // frame_ms)
    max_frames = int(max_utterance_s * 1000 / frame_ms)
    start_run = 0  # consecutive speech frames before trigger

    for frame in frames:
        # should_stop is only honoured while waiting for speech, never mid-phrase
        if not triggered and should_stop and should_stop():
            break
        speech = vad.is_speech(frame, rate)
        if not triggered:
            pre.append(frame)
            start_run = start_run + 1 if speech else 0
            if start_run >= 3:  # ~90 ms of speech starts the phrase
                triggered = True
                voiced.extend(pre)
                pre.clear()
                speech_frames = start_run
                silence_frames = 0
            continue
        voiced.append(frame)
        if speech:
            speech_frames += 1
            silence_frames = 0
        else:
            silence_frames += 1
        if silence_frames >= need_silence or len(voiced) >= max_frames:
            if speech_frames >= need_speech:
                return np.concatenate(voiced).astype(np.float32)
            # too short (click / cough) - reset and keep listening
            triggered, voiced, speech_frames, silence_frames, start_run = False, [], 0, 0, 0
    if triggered and speech_frames >= need_speech:
        return np.concatenate(voiced).astype(np.float32)
    return None


def frames_from_array(audio: np.ndarray, rate: int, frame_ms: int = 30, pad_silence_ms: int = 1500) -> Iterator[np.ndarray]:
    n = int(rate * frame_ms / 1000)
    audio = np.concatenate([audio.astype(np.float32), np.zeros(int(rate * pad_silence_ms / 1000), np.float32)])
    for i in range(0, len(audio) - n + 1, n):
        yield audio[i:i + n]


# --------------------------------------------------------------------------- microphone
class Microphone:
    """Opens an input stream and yields fixed-size float32 frames at `rate`.

    If the device does not support 16 kHz (some WASAPI devices), it records at
    the device default rate and resamples.
    """

    def __init__(self, device=None, rate: int = 16000, frame_ms: int = 30):
        self.device = resolve_device(device, "input")
        self.rate = rate
        self.frame_ms = frame_ms
        self.frame_len = int(rate * frame_ms / 1000)
        self._q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=1000)
        self._stream = None
        self._dev_rate = rate
        self._buf = np.zeros(0, np.float32)

    def _callback(self, indata, frames, t, status):  # runs in PortAudio thread
        if status:
            log.debug("audio status: %s", status)
        try:
            self._q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def start(self):
        s = require_sd()
        try:
            self._dev_rate = self.rate
            self._stream = s.InputStream(device=self.device, channels=1, samplerate=self.rate,
                                         dtype="float32", blocksize=self.frame_len, callback=self._callback)
        except Exception as e:
            info = s.query_devices(self.device, "input")
            self._dev_rate = int(info["default_samplerate"])
            log.info("Микрофон не поддерживает %d Гц (%s), пишу в %d Гц с ресемплингом", self.rate, e, self._dev_rate)
            self._stream = s.InputStream(device=self.device, channels=1, samplerate=self._dev_rate,
                                         dtype="float32", callback=self._callback)
        self._stream.start()
        return self

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def clear(self):
        """Drop buffered audio (e.g. our own TTS output)."""
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._buf = np.zeros(0, np.float32)

    def frames(self, stop_event: Optional[threading.Event] = None) -> Iterator[np.ndarray]:
        while not (stop_event and stop_event.is_set()):
            try:
                chunk = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._dev_rate != self.rate:
                chunk = resample(chunk, self._dev_rate, self.rate)
            self._buf = np.concatenate([self._buf, chunk])
            while self._buf.size >= self.frame_len:
                frame, self._buf = self._buf[: self.frame_len], self._buf[self.frame_len:]
                yield frame

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


# --------------------------------------------------------------------------- playback
class Player:
    def __init__(self, device=None):
        self.device = resolve_device(device, "output") if sd is not None else None
        self._lock = threading.Lock()
        self._stopped = threading.Event()

    def play(self, audio: np.ndarray, rate: int) -> None:
        s = require_sd()
        if audio.size == 0:
            return
        with self._lock:
            self._stopped.clear()
            try:
                s.play(audio.astype(np.float32), samplerate=rate, device=self.device)
                duration = audio.size / rate
                t0 = time.monotonic()
                while time.monotonic() - t0 < duration + 0.3:
                    if self._stopped.is_set():
                        s.stop()
                        return
                    time.sleep(0.05)
                s.wait()
            except Exception as e:
                log.error("Ошибка воспроизведения: %s", e)

    def stop(self) -> None:
        self._stopped.set()
        if sd is not None:
            try:
                sd.stop()
            except Exception:
                pass
