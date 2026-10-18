"""NVIDIA CUDA libraries for GPU speech recognition (faster-whisper / CTranslate2) on Windows.

Verified against the CTranslate2 4.8.3 win_amd64 wheel (CUDA 12.4 build):
  * ctranslate2.dll imports cublas64_12.dll, which itself needs cublasLt64_12.dll;
  * the wheel bundles only the cuDNN 9.10.2.21 *dispatcher* (cudnn64_9.dll); at runtime it loads the
    cudnn_*64_9.dll sub-libraries, which must be the SAME cuDNN version.
None of these come with the NVIDIA driver, so without them GPU Whisper fails with
"Library cublas64_12.dll is not found or cannot be loaded". The pinned NVIDIA pip wheels below put
the DLLs into site-packages/nvidia/<lib>/bin; add_dll_dirs() makes them visible before ctranslate2 loads.
They are not installed automatically: the default is the CPU, and GPU mode falls back to it.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, Mapping, Optional

log = logging.getLogger(__name__)

# Matching NVIDIA pip wheels (NOT downloaded automatically; GPU Whisper is opt-in, ~1.1 GB):
#   python\python.exe -m pip install nvidia-cublas-cu12==12.4.5.8 nvidia-cudnn-cu12==9.10.2.21
MANUAL_PACKAGES = ["nvidia-cublas-cu12==12.4.5.8", "nvidia-cudnn-cu12==9.10.2.21"]
REQUIRED_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll",
                 "cudnn_ops64_9.dll", "cudnn_cnn64_9.dll", "cudnn_graph64_9.dll")
GPU_LIB_WORDS = ("cuda", "cublas", "cudnn", "cannot be loaded", "is not found", "nvcuda", "dll")

_added: dict[str, object] = {}      # keep the add_dll_directory handles alive


def nvidia_bin_dirs(paths: Optional[Iterable[str]] = None) -> list[Path]:
    out: list[Path] = []
    for base in map(Path, paths if paths is not None else sys.path):
        nv = base / "nvidia"
        if nv.is_dir():
            out += sorted(p for p in nv.glob("*/bin") if p.is_dir())
    return out


def toolkit_bin_dirs(env: Optional[dict] = None) -> list[Path]:
    """CUDA Toolkit 12.x installed system-wide (CUDA_PATH / CUDA_PATH_V12_*)."""
    e: Mapping[str, str] = os.environ if env is None else env
    out = []
    for k, v in e.items():
        if (k == "CUDA_PATH" or k.startswith("CUDA_PATH_V12")) and v:
            b = Path(v) / "bin"
            if b.is_dir() and b not in out:
                out.append(b)
    return out


def search_dirs(paths: Optional[Iterable[str]] = None, env: Optional[dict] = None) -> list[Path]:
    e: Mapping[str, str] = os.environ if env is None else env
    dirs = nvidia_bin_dirs(paths) + toolkit_bin_dirs(dict(e))
    for p in (e.get("PATH") or "").split(os.pathsep):
        if p and Path(p) not in dirs:
            dirs.append(Path(p))
    return dirs


def missing_dlls(dirs: Optional[Iterable[Path]] = None) -> list[str]:
    dirs = list(search_dirs() if dirs is None else dirs)
    have: set[str] = set()
    for d in dirs:
        try:
            have.update(f.name.lower() for f in d.iterdir() if f.suffix.lower() == ".dll")
        except OSError:
            pass
    return [n for n in REQUIRED_DLLS if n.lower() not in have]


def add_dll_dirs(paths: Optional[Iterable[str]] = None, env: Optional[dict] = None) -> list[str]:
    """Windows: make the NVIDIA pip / CUDA Toolkit DLLs loadable. Call BEFORE importing ctranslate2."""
    if sys.platform != "win32":
        return []
    added = []
    for d in nvidia_bin_dirs(paths) + toolkit_bin_dirs(env):
        key = str(d)
        if key in _added:
            continue
        try:
            _added[key] = os.add_dll_directory(key)      # type: ignore[attr-defined]
            os.environ["PATH"] = key + os.pathsep + os.environ.get("PATH", "")
            added.append(key)
        except (OSError, AttributeError) as e:
            log.debug("add_dll_directory %s: %s", key, e)
    if added:
        log.info("Библиотеки CUDA: %s", "; ".join(added))
    return added


def nvidia_gpu_present(system_root: Optional[str] = None) -> bool:
    """An NVIDIA driver is installed (nvcuda.dll / nvidia-smi). Cheap: no CUDA initialisation."""
    if sys.platform == "win32" or system_root:
        root = Path(system_root or os.environ.get("SystemRoot", r"C:\Windows"))
        if (root / "System32" / "nvcuda.dll").is_file():
            return True
        pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        if (pf / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe").is_file():
            return True
    return shutil.which("nvidia-smi") is not None


def is_gpu_lib_error(e: BaseException) -> bool:
    s = f"{type(e).__name__} {e}".lower()
    return any(w in s for w in GPU_LIB_WORDS)


def short_error(e: BaseException) -> str:
    s = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return s[:160]
