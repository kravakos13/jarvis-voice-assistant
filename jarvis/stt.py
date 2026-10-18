"""Speech-to-text with faster-whisper (offline after the model is downloaded)."""
from __future__ import annotations

import logging
from typing import Optional
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .config import resolve_path

log = logging.getLogger(__name__)


def _add_windows_cuda_dlls() -> None:
    """On Windows, make DLLs from pip packages nvidia-cublas-cu12 / nvidia-cudnn-cu12 visible."""
    if sys.platform != "win32":
        return
    for base in map(Path, sys.path):
        nv = base / "nvidia"
        if not nv.is_dir():
            continue
        for bin_dir in nv.glob("*/bin"):
            try:
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            except Exception:
                pass


def pick_device(device: str = "auto", compute_type: str = "auto") -> tuple[str, str]:
    device = (device or "auto").lower()
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    if not compute_type or compute_type == "auto":
        # int8_float16: ~2x less VRAM than float16 and faster on GTX 16xx (no tensor cores), same accuracy
        compute_type = "int8_float16" if device == "cuda" else "int8"
    return device, compute_type



def _whisper(cls, model: str, **kw):
    """Load from the local cache first (fully offline, no Hugging Face requests); download only if missing."""
    try:
        return cls(model, local_files_only=True, **kw)
    except Exception as e:
        if "cuda" in str(e).lower() or "cublas" in str(e).lower() or "cudnn" in str(e).lower():
            raise
        log.info("Модель Whisper '%s' не найдена локально — скачиваю (один раз).", model)
        return cls(model, **kw)

class SpeechToText:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._lock = threading.RLock()
        self.relocated = False
        self._orig: Optional[tuple[str, str]] = None
        self.language = cfg.get("language", "ru")
        self.beam_size = int(cfg.get("beam_size", 1))
        self.initial_prompt = cfg.get("initial_prompt") or None
        _add_windows_cuda_dlls()
        from faster_whisper import WhisperModel

        device, compute = pick_device(cfg.get("device", "auto"), cfg.get("compute_type", "auto"))
        model = cfg.get("model", "small")
        root = cfg.get("download_root")
        root = str(resolve_path(root)) if root else None
        t0 = time.time()
        try:
            self.model = _whisper(WhisperModel, model, device=device, compute_type=compute, download_root=root,
                                  cpu_threads=int(cfg.get("cpu_threads", 0) or 0))
            if device == "cuda":  # catch missing cuBLAS/cuDNN early
                self.model.transcribe(np.zeros(16000, np.float32), language=self.language)
        except Exception as e:
            if device != "cuda":
                raise
            log.warning("CUDA недоступна для Whisper (%s). Переключаюсь на CPU/int8.", e)
            device, compute = "cpu", "int8"
            self.model = _whisper(WhisperModel, model, device=device, compute_type=compute, download_root=root,
                                  cpu_threads=int(cfg.get("cpu_threads", 0) or 0))
        self.device, self.compute_type = device, compute
        log.info("Whisper '%s' загружен (%s/%s) за %.1f с", model, device, compute, time.time() - t0)

    def _load(self, device: str, compute: str):
        from faster_whisper import WhisperModel
        root = self.cfg.get("download_root")
        root = str(resolve_path(root)) if root else None
        return _whisper(WhisperModel, self.cfg.get("model", "small"), device=device, compute_type=compute, download_root=root,
                            cpu_threads=int(self.cfg.get("cpu_threads", 0) or 0))

    def relocate(self, target) -> None:
        """'cpu' — move to CPU/int8; None — unload (lazy reload on CPU); 'restore' — back to the original device."""
        with self._lock:
            try:
                if target == "restore":
                    if self._orig:
                        dev, comp = self._orig
                        self.model = None
                        self.model = self._load(dev, comp)
                        self.device, self.compute_type = dev, comp
                    self.relocated, self._orig = False, None
                    return
                if self._orig is None:
                    self._orig = (self.device, self.compute_type)
                self.model = None          # frees VRAM
                import gc
                gc.collect()
                if target == "cpu":
                    self.model = self._load("cpu", "int8")
                self.device, self.compute_type = "cpu", "int8"
                self.relocated = True
                log.info("Whisper: %s", "CPU" if target == "cpu" else "выгружен")
            except Exception as e:
                log.error("Whisper relocate: %s", e)

    def transcribe(self, audio, language: str | None = "default") -> str:
        with self._lock:
            if self.model is None:          # unloaded in game mode -> reload on demand (CPU)
                self.model = self._load("cpu", "int8")
            return self._transcribe(audio, language)

    def _transcribe(self, audio, language: str | None = "default") -> str:
        """audio: float32 numpy array at 16 kHz, or a path to an audio file."""
        if isinstance(audio, np.ndarray):
            audio = audio.astype(np.float32, copy=False)
            if audio.size < 1600:  # < 0.1 s
                return ""
        elif isinstance(audio, Path):
            audio = str(audio)
        t0 = time.time()
        segments, _info = self.model.transcribe(
            audio,
            language=self.language if language == "default" else language,   # None = auto-detect
            beam_size=self.beam_size,
            initial_prompt=self.initial_prompt if language in ("default", "ru") else None,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
        )
        text = " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.8).strip()
        log.debug("STT %.2f с: %r", time.time() - t0, text)
        return _drop_hallucinations(text)


# Typical Whisper hallucinations on silence/noise in Russian
_HALLUCINATIONS = (
    "продолжение следует",
    "субтитры сделал",
    "субтитры создавал",
    "редактор субтитров",
    "спасибо за просмотр",
    "подписывайтесь на канал",
    "dimatorzok",
)


def _drop_hallucinations(text: str) -> str:
    low = text.lower()
    if any(h in low for h in _HALLUCINATIONS):
        return ""
    return text
