"""'AI butler' voice styling for TTS output — pure numpy, no extra dependencies.

Chain: high-pass (rumble/boxiness cut) -> gentle compressor -> light presence lift ->
faint metallic chorus layer -> short, small 'room' reverb -> soft limiter.
`intensity` 0..1 scales the effect (0.35 = subtle and premium, 1.0 = clearly 'synthetic').
It does NOT imitate any real person's voice — it only colours the existing Piper voice.
"""
from __future__ import annotations

import numpy as np


def _fft_filter(x: np.ndarray, sr: int, hp_hz: float, presence_db: float) -> np.ndarray:
    n = int(2 ** np.ceil(np.log2(len(x) + 1)))
    X = np.fft.rfft(x, n)
    f = np.fft.rfftfreq(n, 1.0 / sr)
    # 2nd-order-ish high-pass magnitude response
    hp = 1.0 / np.sqrt(1.0 + (hp_hz / np.maximum(f, 1.0)) ** 4)
    # broad presence bell around 3.2 kHz
    bell = 10 ** (presence_db / 20.0 * np.exp(-0.5 * ((f - 3200.0) / 1400.0) ** 2))
    return np.fft.irfft(X * hp * bell, n)[: len(x)]


def _compress(x: np.ndarray, sr: int, threshold_db: float = -20.0, ratio: float = 2.5,
              attack_ms: float = 8.0, release_ms: float = 120.0) -> np.ndarray:
    # envelope follower on 2 ms blocks (vectorised enough for speech lengths)
    hop = max(1, int(sr * 0.002))
    nb = int(np.ceil(len(x) / hop))
    pad = np.pad(np.abs(x), (0, nb * hop - len(x)))
    peak = pad.reshape(nb, hop).max(axis=1)
    a_att = np.exp(-hop / (sr * attack_ms / 1000.0))
    a_rel = np.exp(-hop / (sr * release_ms / 1000.0))
    env = np.empty(nb)
    e = 0.0
    for i, p in enumerate(peak):
        a = a_att if p > e else a_rel
        e = a * e + (1 - a) * p
        env[i] = e
    lvl = 20 * np.log10(np.maximum(env, 1e-6))
    over = np.maximum(lvl - threshold_db, 0.0)
    gain_db = -over * (1 - 1 / ratio)
    gain = 10 ** (gain_db / 20.0)
    g = np.repeat(gain, hop)[: len(x)]
    out = x * g
    return out * (10 ** ((-threshold_db * (1 - 1 / ratio)) * 0.35 / 20.0))   # make-up gain


def _chorus(x: np.ndarray, sr: int, depth_ms: float = 2.5, base_ms: float = 11.0, rate_hz: float = 0.8) -> np.ndarray:
    n = np.arange(len(x))
    out = np.zeros_like(x)
    for k, ph in enumerate((0.0, 2.1)):
        d = (base_ms + k * 4 + depth_ms * np.sin(2 * np.pi * rate_hz * n / sr + ph)) * sr / 1000.0
        out += np.interp(n - d, n, x, left=0.0)
    out *= 0.5
    # ring-mod sparkle at very low level = 'metallic' edge
    ring = x * np.sin(2 * np.pi * 95.0 * n / sr)
    return out + 0.25 * ring


def _reverb(x: np.ndarray, sr: int, decay_s: float = 0.35, predelay_ms: float = 12.0) -> np.ndarray:
    rng = np.random.default_rng(42)
    L = int(sr * decay_s)
    t = np.arange(L) / sr
    ir = rng.standard_normal(L) * np.exp(-t * 6.9 / decay_s)
    ir[: int(sr * predelay_ms / 1000)] = 0
    # darker tail
    ir = np.convolve(ir, np.ones(6) / 6, mode="same")
    ir /= np.sqrt(np.sum(ir ** 2)) + 1e-9
    n = int(2 ** np.ceil(np.log2(len(x) + L)))
    y = np.fft.irfft(np.fft.rfft(x, n) * np.fft.rfft(ir, n), n)[: len(x) + L]
    return y


def apply_fx(audio: np.ndarray, sr: int, intensity: float = 0.35) -> np.ndarray:
    if audio.size == 0 or intensity <= 0:
        return audio
    k = float(np.clip(intensity, 0.0, 1.0))
    x = audio.astype(np.float64)
    peak_in = np.max(np.abs(x)) or 1.0
    x = _fft_filter(x, sr, hp_hz=110.0 + 60 * k, presence_db=2.5 * k)
    x = _compress(x, sr, threshold_db=-18.0, ratio=1.8 + 1.5 * k)
    wet_ch = _chorus(x, sr)
    x = x + wet_ch * (0.12 * k)
    rev = _reverb(x, sr)
    y = np.zeros(len(rev))
    y[: len(x)] = x
    y = y + rev * (0.10 + 0.12 * k)
    # trim tail to last audible sample, soft limiter, match loudness
    tail = np.where(np.abs(y) > 1e-3 * np.max(np.abs(y)))[0]
    if tail.size:
        y = y[: tail[-1] + 1]
    y = np.tanh(y / (np.max(np.abs(y)) or 1.0) * 1.2) / np.tanh(1.2)
    return (y * min(0.97, peak_in * 1.05)).astype(np.float32)
