"""System load control (1.0.8, «Нагрузка»): Jarvis must never make Windows unresponsive.

* BELOW_NORMAL process priority (Jarvis and the Ollama server it starts);
* limited threads for Whisper / ONNX / OpenMP and Ollama (num_thread);
* free RAM / VRAM checks before the LLM is loaded: the model gets num_gpu layers that fit with ~1 GB VRAM headroom,
  and it is NOT silently spilled to CPU+RAM when free RAM is low;
* one resource line in jarvis.log every 5 minutes.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import Any, Optional

log = logging.getLogger("jarvis.res")
IS_WIN = sys.platform == "win32"

# presets: Settings → «Нагрузка». Explicit values in the config sections always win over the preset.
PRESETS: dict[str, dict[str, Any]] = {
    # default: qwen2.5:7b (full personality), 4 threads, model unloaded after 5 min idle, lower priority
    "balanced": {"threads": 4, "keep_alive": "5m", "num_ctx": 4096, "face_fps": 30, "vram_headroom_gb": 1.0,
                 "min_free_ram_gb": 6.0, "tools": "core", "priority": "below_normal", "model": None},
    # opt-in: the small model (~2 GB): fits next to games/Chrome, noticeably simpler answers
    "light": {"threads": 4, "keep_alive": "5m", "num_ctx": 4096, "face_fps": 24, "vram_headroom_gb": 1.0,
              "min_free_ram_gb": 6.0, "tools": "core", "priority": "below_normal", "model": "qwen2.5:3b"},
    # 1.0.7-like: all threads, model kept 30 min, every tool offered to the model, normal priority
    "max": {"threads": 0, "keep_alive": "30m", "num_ctx": 6144, "face_fps": 30, "vram_headroom_gb": 0.6,
            "min_free_ram_gb": 4.0, "tools": "all", "priority": "normal", "model": None},
}
PRESET_NAMES = {"balanced": "Обычная (по умолчанию)", "light": "Лёгкая — модель qwen2.5:3b",
                "max": "Максимальная скорость"}
LIGHT_MODEL = "qwen2.5:3b"


def preset(cfg: dict) -> dict:
    name = str(((cfg or {}).get("load") or {}).get("preset") or "balanced")
    return dict(PRESETS.get(name, PRESETS["balanced"]), name=name if name in PRESETS else "balanced")


def thread_env(n: int) -> dict:
    """Thread caps for OpenMP/MKL/ONNX (set BEFORE numpy / ctranslate2 / onnxruntime are imported)."""
    if n <= 0:
        return {}
    v = str(n)
    return {"OMP_NUM_THREADS": v, "MKL_NUM_THREADS": v, "OPENBLAS_NUM_THREADS": v, "NUMEXPR_NUM_THREADS": v,
            "ORT_NUM_THREADS": v}


def apply_env(cfg: dict) -> None:
    for k, v in thread_env(int(preset(cfg)["threads"])).items():
        os.environ.setdefault(k, v)


def _kernel32():
    """kernel32 with typed prototypes: the pseudo-handles (-1 / -2) are 64-bit HANDLEs; with ctypes' default int
    conversion they were truncated to 32 bits and SetPriorityClass failed (seen under Wine, possible on x64)."""
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k.GetCurrentProcess.restype = k.GetCurrentThread.restype = w.HANDLE
    k.SetPriorityClass.argtypes, k.SetPriorityClass.restype = [w.HANDLE, w.DWORD], w.BOOL
    k.GetPriorityClass.argtypes, k.GetPriorityClass.restype = [w.HANDLE], w.DWORD
    k.SetThreadPriority.argtypes, k.SetThreadPriority.restype = [w.HANDLE, ctypes.c_int], w.BOOL
    return k


def lower_priority(cfg: Optional[dict] = None) -> bool:
    if cfg is not None and preset(cfg)["priority"] == "normal":
        return False
    try:
        if IS_WIN:
            k = _kernel32()
            return bool(k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000))   # BELOW_NORMAL_PRIORITY_CLASS
        os.nice(5)
        return True
    except Exception as e:                                # noqa: BLE001
        log.debug("priority: %s", e)
        return False


def lowest_thread_priority() -> None:
    """For background indexing threads (call inside the thread)."""
    if not IS_WIN:
        return
    try:
        k = _kernel32()
        k.SetThreadPriority(k.GetCurrentThread(), -2)       # THREAD_PRIORITY_LOWEST
    except Exception as e:                                # noqa: BLE001
        log.debug("thread priority: %s", e)


# ------------------------------------------------------------------ memory
def free_ram_gb() -> Optional[float]:
    try:
        import psutil
        return round(psutil.virtual_memory().available / 2**30, 2)
    except Exception:                                     # noqa: BLE001
        pass
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return round(int(line.split()[1]) / 2**20, 2)
    except Exception:                                     # noqa: BLE001
        pass
    return None


_gpu_cache: tuple[float, Optional[tuple[float, float]]] = (0.0, None)


def vram_gb(run=subprocess.run, now: Optional[float] = None) -> Optional[tuple[float, float]]:
    """(total, free) GB of the first NVIDIA GPU via nvidia-smi (cached 20 s); None without one."""
    global _gpu_cache
    now = time.monotonic() if now is None else now
    if now - _gpu_cache[0] < 20 and _gpu_cache[0]:
        return _gpu_cache[1]
    val = None
    try:
        kw: dict = {"capture_output": True, "text": True, "timeout": 5}
        if IS_WIN:
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = run(["nvidia-smi", "--query-gpu=memory.total,memory.free", "--format=csv,noheader,nounits"], **kw)
        if out.returncode == 0 and out.stdout.strip():
            tot, free = (float(x) for x in out.stdout.strip().splitlines()[0].split(","))
            val = (round(tot / 1024, 2), round(free / 1024, 2))
    except Exception as e:                                # noqa: BLE001
        log.debug("nvidia-smi: %s", e)
    _gpu_cache = (now, val)
    return val


def kv_gb(num_ctx: int, layers: int = 28, kv_heads: int = 4, head_dim: int = 128, bytes_per: int = 2) -> float:
    """KV cache size (qwen2.5-7b defaults: 28 layers, 4 KV heads × 128, f16)."""
    return num_ctx * layers * kv_heads * head_dim * 2 * bytes_per / 2**30


def fit_layers(model_gb: float, layers: int, num_ctx: int, free_vram: Optional[float], headroom: float,
               already_loaded_gb: float = 0.0) -> Optional[int]:
    """How many layers to put on the GPU. None = all of them (fits, let Ollama decide); 0 = none (CPU)."""
    if free_vram is None:
        return None
    free = free_vram + already_loaded_gb - headroom
    need = model_gb + kv_gb(num_ctx, layers) + 0.45      # + compute buffers
    if free >= need:
        return None
    per = model_gb / (layers + 1)
    n = int((free - kv_gb(num_ctx, layers) - 0.45) / per)
    return max(0, min(n, layers))


def spill_gb(model_gb: float, layers: int, num_gpu: Optional[int]) -> float:
    if num_gpu is None:
        return 0.0
    return round(model_gb * (1 - num_gpu / (layers + 1)), 2)


# ------------------------------------------------------------------ periodic log line
class Monitor:
    def __init__(self, llm: Any = None, period_s: float = 300.0):
        self.llm, self.period = llm, period_s
        self._last_cpu: Optional[tuple[float, float]] = None

    def own_cpu_percent(self) -> Optional[float]:
        try:
            t = sum(os.times()[:2])
            now = time.monotonic()
            prev, self._last_cpu = self._last_cpu, (now, t)
            if prev is None or now - prev[0] <= 0:
                return None
            return round(100.0 * (t - prev[1]) / (now - prev[0]) / (os.cpu_count() or 1), 1)
        except Exception:                                 # noqa: BLE001
            return None

    @staticmethod
    def own_rss_mb() -> Optional[float]:
        try:
            import psutil
            return round(psutil.Process().memory_info().rss / 2**20)
        except Exception:                                 # noqa: BLE001
            return None

    def line(self) -> str:
        parts = []
        cpu = self.own_cpu_percent()
        if cpu is not None:
            parts.append(f"CPU Джарвиса {cpu:.1f}%")
        rss = self.own_rss_mb()
        if rss is not None:
            parts.append(f"RAM Джарвиса {rss:.0f} МБ")
        fr = free_ram_gb()
        if fr is not None:
            parts.append(f"свободно RAM {fr:.1f} ГБ")
        vr = vram_gb()
        if vr is not None:
            parts.append(f"видеопамять свободно {vr[1]:.1f} из {vr[0]:.1f} ГБ")
        info = None
        try:
            info = self.llm.placement_info() if self.llm is not None else None
        except Exception:                                 # noqa: BLE001
            info = None
        if info:
            parts.append(f"ИИ {info['model']}: {int(info['gpu_share'] * 100)}% на видеокарте")
        else:
            parts.append("ИИ не загружен")
        return "Ресурсы: " + ", ".join(parts)

    def run(self, stop) -> None:
        self.own_cpu_percent()
        while not stop.wait(self.period):
            try:
                ln = self.line()
                log.info(ln)
                info = self.llm.placement_info() if self.llm is not None else None
                if info and info.get("gpu_share", 1) < 0.99:
                    log.warning("ИИ частично на процессоре (%d%% на видеокарте) — медленно и грузит систему",
                                int(info["gpu_share"] * 100))
            except Exception as e:                        # noqa: BLE001
                log.debug("monitor: %s", e)
