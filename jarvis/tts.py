"""Text-to-speech: Piper (offline neural voice) with pyttsx3 (Windows SAPI) fallback."""
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from .audio import Player, write_wav
from .config import resolve_path
from .llm import clean_for_speech

log = logging.getLogger(__name__)


class PiperTTS:
    name = "piper"

    def __init__(self, cfg: dict):
        from piper import PiperVoice  # piper-tts >= 1.3

        voice = cfg.get("piper_voice", "ru_RU-denis-medium")
        model_path = Path(voice)
        if model_path.suffix != ".onnx":
            model_path = resolve_path(cfg.get("voices_dir", "voices")) / f"{voice}.onnx"
        if not model_path.exists():
            raise FileNotFoundError(
                f"Голос Piper не найден: {model_path}. Запусти: python scripts/download_models.py --voice {voice}")
        self.voice = PiperVoice.load(str(model_path))
        self.sample_rate = int(self.voice.config.sample_rate)
        self.length_scale = float(cfg.get("length_scale", 1.0))
        self.volume = float(cfg.get("volume", 1.0))
        self.pitch = float(cfg.get("pitch_semitones", 0.0))
        fx = cfg.get("fx") or {}
        self.fx_enabled = bool(fx.get("enabled", False))
        self.fx_intensity = float(fx.get("intensity", 0.35))
        self.cfg = cfg
        self._others: dict[str, "PiperTTS"] = {}
        log.info("Piper голос загружен: %s (%d Гц)", model_path.name, self.sample_rate)

    def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[np.ndarray, int]:
        if voice and voice != self.cfg.get("piper_voice"):
            return self.other(voice).synthesize(text)
        from piper import SynthesisConfig

        factor = 2.0 ** (max(-6.0, min(6.0, self.pitch)) / 12.0)
        syn = SynthesisConfig(length_scale=self.length_scale * factor, volume=self.volume)
        chunks = [c.audio_float_array for c in self.voice.synthesize(text, syn_config=syn)]
        audio = np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, np.float32)
        if abs(factor - 1.0) > 1e-3 and audio.size:
            # pitch shift by resampling; duration was pre-compensated via length_scale
            n = max(1, int(len(audio) / factor))
            audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
        if self.fx_enabled and audio.size:
            try:
                from .voicefx import apply_fx
                audio = apply_fx(audio, self.sample_rate, self.fx_intensity)
            except Exception as e:  # styling must never break speech
                log.debug("voicefx: %s", e)
        return audio, self.sample_rate

    def other(self, voice: str) -> "PiperTTS":
        """Extra voice (e.g. en_US-ryan-medium for the translator), downloaded on demand."""
        if voice not in self._others:
            path = resolve_path(self.cfg.get("voices_dir", "voices")) / f"{voice}.onnx"
            if not path.exists():
                ensure_voice(voice, path.parent)
            cfg = dict(self.cfg, piper_voice=voice, fx={"enabled": False}, length_scale=1.0)
            self._others[voice] = PiperTTS(cfg)
        return self._others[voice]


class Pyttsx3TTS:
    """Fallback. On Windows uses SAPI5 voices (install a Russian voice in Windows settings).

    SAPI is COM based, so the engine is created lazily inside the thread that speaks.
    """

    name = "pyttsx3"

    def __init__(self, cfg: dict):
        import pyttsx3  # noqa: F401  (fail early if not installed)

        self.cfg = cfg
        self.engine = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self.engine is not None:
            return self.engine
        import sys
        if sys.platform == "win32":
            try:
                import pythoncom  # from pywin32, installed with pyttsx3 on Windows
                pythoncom.CoInitialize()
            except Exception:
                pass
        import pyttsx3

        engine = pyttsx3.init()
        engine.setProperty("rate", int(self.cfg.get("pyttsx3_rate", 180)))
        wanted = str(self.cfg.get("pyttsx3_voice") or "").lower()
        chosen = None
        for v in engine.getProperty("voices"):
            desc = f"{v.id} {v.name} {getattr(v, 'languages', '')}".lower()
            if wanted and wanted in desc:
                chosen = v.id
                break
            if not wanted and chosen is None and any(k in desc for k in ("ru-ru", "russian", "irina", "pavel", "ru_ru")):
                chosen = v.id
        if chosen:
            engine.setProperty("voice", chosen)
        else:
            log.warning("Русский голос SAPI не найден - речь может быть на английском.")
        self.engine = engine
        return engine

    def say(self, text: str) -> None:
        with self._lock:
            engine = self._ensure()
            engine.say(text)
            engine.runAndWait()

    def stop(self) -> None:
        try:
            if self.engine is not None:
                self.engine.stop()
        except Exception:
            pass


def ensure_voice(voice: str, folder: Path) -> Path:
    """Download a Piper voice from rhasspy/piper-voices if missing."""
    import requests
    lang_region, name, quality = voice.split("-", 2)
    base = (f"https://huggingface.co/rhasspy/piper-voices/resolve/main/{lang_region.split('_')[0]}/"
            f"{lang_region}/{name}/{quality}/{voice}")
    folder.mkdir(parents=True, exist_ok=True)
    for ext in (".onnx.json", ".onnx"):
        dest = folder / f"{voice}{ext}"
        if dest.exists():
            continue
        log.info("Скачиваю голос %s%s ...", voice, ext)
        with requests.get(base + ext, stream=True, timeout=60) as r:
            r.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
            tmp.replace(dest)
    return folder / f"{voice}.onnx"


def create_engine(cfg: dict):
    engine = str(cfg.get("engine", "piper")).lower()
    if engine == "none":
        return None
    if engine == "piper":
        try:
            return PiperTTS(cfg)
        except Exception as e:
            log.warning("Piper недоступен (%s). Пробую pyttsx3.", e)
    try:
        return Pyttsx3TTS(cfg)
    except Exception as e:
        log.error("Не удалось инициализировать ни один TTS (%s). Озвучка выключена.", e)
        return None


def speech_envelope(audio: np.ndarray, rate: int, frame_s: float = 0.02) -> tuple[list, list]:
    """Lip-sync data: per-frame mouth openness (RMS, 0..1) and width (spectral centroid, 0..1;
    high = wide 'и/е', low = round 'о/у')."""
    n = max(1, int(rate * frame_s))
    frames = len(audio) // n
    if frames == 0:
        return [], []
    a = audio[: frames * n].reshape(frames, n).astype(np.float32)
    rms = np.sqrt(np.mean(a * a, axis=1))
    peak = float(np.percentile(rms, 95)) or 1.0
    openness = np.clip(rms / peak, 0, 1) ** 0.8
    spec = np.abs(np.fft.rfft(a * np.hanning(n), axis=1))
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    cent = (spec * freqs).sum(axis=1) / (spec.sum(axis=1) + 1e-9)
    width = np.clip((cent - 500.0) / 2500.0, 0, 1)
    return [round(float(x), 3) for x in openness], [round(float(x), 3) for x in width]


class Speaker:
    """Background worker: sentences are queued, synthesized and played in order,
    so synthesis of the next sentence overlaps with LLM streaming.

    on_event(name, **data) receives 'speech_start' (with lip-sync envelope) and 'speech_end'."""

    def __init__(self, engine, output_device=None, on_event=None):
        self.on_event = on_event or (lambda *a, **k: None)
        self.engine = engine
        self.player = Player(output_device) if isinstance(engine, PiperTTS) else None
        self._q: "queue.Queue" = queue.Queue()
        self.volume = 1.0              # profile volume (night mode = quiet)
        self._interrupt = threading.Event()
        self._last_end = 0.0           # monotonic time the last sentence finished playing
        self._thread = threading.Thread(target=self._run, name="jarvis-tts", daemon=True)
        self._thread.start()

    @property
    def enabled(self) -> bool:
        return self.engine is not None

    def say(self, text: str, voice: Optional[str] = None, volume: Optional[float] = None) -> None:
        text = clean_for_speech(text)
        if not text or not self.enabled:
            return
        self._q.put((text, voice, self.volume if volume is None else volume))

    def active(self, tail_s: float = 0.0) -> bool:
        """Speaking now, queued, or finished less than tail_s ago (room echo / output latency).
        The microphone ignores this time so Jarvis never hears (and wakes on) its own voice."""
        if not self.enabled:
            return False
        return self._q.unfinished_tasks > 0 or time.monotonic() - self._last_end < tail_s

    def wait(self, timeout: Optional[float] = None) -> None:
        if self.enabled:
            self._q.join()

    def interrupt(self) -> None:
        self._interrupt.set()
        while not self._q.empty():
            try:
                self._q.get_nowait()
                self._q.task_done()
            except queue.Empty:
                break
        if self.player:
            self.player.stop()
        elif self.engine is not None and hasattr(self.engine, "stop"):
            self.engine.stop()

    def close(self) -> None:
        self.interrupt()
        self._q.put(None)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            text = item[0] if item else None
            try:
                if item is None:
                    return
                _, voice, vol = item
                self._interrupt.clear()
                if isinstance(self.engine, PiperTTS):
                    audio, rate = self.engine.synthesize(text, voice)
                    if vol != 1.0:
                        audio = audio * float(vol)
                    if not self._interrupt.is_set():
                        env, wid = speech_envelope(audio, rate)
                        self.on_event("speech_start", text=text, env=env, wid=wid, dt=0.02,
                                      duration=len(audio) / rate)
                        self.player.play(audio, rate)
                else:
                    # SAPI: no audio buffer - estimate ~14 chars/s with a synthetic envelope
                    dur = max(0.6, len(text) / 14.0)
                    k = int(dur / 0.02)
                    env = [round(0.35 + 0.35 * abs(np.sin(i * 0.9)), 3) for i in range(k)]
                    self.on_event("speech_start", text=text, env=env, wid=[0.5] * k, dt=0.02, duration=dur)
                    self.engine.say(text)
            except Exception as e:
                log.error("Ошибка TTS: %s", e)
            finally:
                self._last_end = time.monotonic()
                if text is not None and self._q.unfinished_tasks <= 1:
                    self.on_event("speech_end")
                self._q.task_done()


def synth_to_wav(cfg: dict, text: str, out_path: str | Path) -> Path:
    """Synthesize text into a WAV file with Piper (used for testing / --say)."""
    tts = PiperTTS(cfg)
    audio, rate = tts.synthesize(clean_for_speech(text))
    write_wav(out_path, audio, rate)
    return Path(out_path)
