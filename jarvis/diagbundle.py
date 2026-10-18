"""«Джарвис, собери диагностику» (1.0.9): one zip on the Desktop with everything needed to debug remotely —
jarvis.log (+ rotations), Ollama's server.log files, `nvidia-smi`, Ollama /api/ps + /api/version, system info
and the local settings with secrets redacted. Nothing is sent anywhere: the user decides what to do with it."""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import platform
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__
from . import platform_utils as pu
from .config import LOCAL_SETTINGS, resolve_path

log = logging.getLogger(__name__)

SECRET_RE = re.compile(r"(token|hash|password|passwd|secret|api_?key|api_id|session|cookie|auth)", re.I)
MAX_FILE = 25_000_000


def desktop_dir() -> Path:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for c in (home / "Desktop", home / "OneDrive" / "Desktop", home / "OneDrive" / "Рабочий стол", home / "Рабочий стол"):
        if c.is_dir():
            return c
    return pu.data_dir("")


def ollama_logs() -> list[Path]:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Ollama"
    out = [p for p in sorted(base.glob("server*.log")) if p.is_file()]
    out += [p for p in sorted(base.glob("app*.log")) if p.is_file()]
    return out


def jarvis_logs(cfg: dict) -> list[Path]:
    f = (cfg.get("logging") or {}).get("file") or "logs/jarvis.log"
    p = resolve_path(f)
    return [x for x in [p] + [Path(f"{p}.{i}") for i in range(1, 4)] if x.is_file()]


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if SECRET_RE.search(str(k)) and v not in (None, "", [], {}) else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def _run(run: Callable, args: list[str]) -> str:
    try:
        kw: dict = {"capture_output": True, "text": True, "timeout": 15, "encoding": "utf-8", "errors": "replace"}
        if pu.IS_WIN:
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = run(args, **kw)
        return (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    except Exception as e:                                     # noqa: BLE001
        return f"{' '.join(args)}: {e}"


def collect(cfg: dict, assistant: Any = None, dest: Optional[Path] = None, run: Callable = subprocess.run,
            http: Any = None, now: Optional[_dt.datetime] = None) -> Path:
    now = now or _dt.datetime.now()
    dest = dest or desktop_dir()
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"Jarvis-диагностика-{now:%Y%m%d-%H%M}.zip"
    host = str((cfg.get("llm") or {}).get("host") or "http://localhost:11434").rstrip("/")
    if http is None:
        import requests
        http = requests
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in jarvis_logs(cfg):
            if p.stat().st_size <= MAX_FILE:
                z.write(p, f"jarvis/{p.name}")
        for p in ollama_logs():
            if p.stat().st_size <= MAX_FILE:
                z.write(p, f"ollama/{p.name}")
        z.writestr("nvidia-smi.txt", _run(run, ["nvidia-smi"]) + "\n\n" +
                   _run(run, ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used,memory.free,"
                              "utilization.gpu", "--format=csv"]))
        for api in ("ps", "version", "tags"):
            try:
                z.writestr(f"ollama/api-{api}.json", json.dumps(http.get(f"{host}/api/{api}", timeout=5).json(),
                                                                ensure_ascii=False, indent=1))
            except Exception as e:                             # noqa: BLE001
                z.writestr(f"ollama/api-{api}.json", json.dumps({"error": str(e)}, ensure_ascii=False))
        info = {"jarvis": __version__, "python": platform.python_version(), "os": platform.platform(),
                "cpu": platform.processor(), "cores": os.cpu_count(), "time": now.isoformat(timespec="seconds"),
                "ollama_env": {k: v for k, v in os.environ.items() if k.startswith(("OLLAMA_", "CUDA_"))}}
        try:
            import psutil
            vm = psutil.virtual_memory()
            info["ram_gb"] = {"total": round(vm.total / 2**30, 1), "free": round(vm.available / 2**30, 1)}
        except Exception:                                      # noqa: BLE001
            pass
        llm = getattr(assistant, "llm", None)
        if llm is not None:
            try:
                info["llm"] = {"model": llm.model, "placement": llm.placement, "on_cpu": llm.on_cpu,
                               "num_ctx": llm.num_ctx, "keep_alive": llm.keep_alive,
                               "where": llm.placement_report()[0]}
            except Exception as e:                             # noqa: BLE001
                info["llm"] = {"error": str(e)}
        z.writestr("system.json", json.dumps(info, ensure_ascii=False, indent=1))
        try:
            import yaml
            local = yaml.safe_load(Path(LOCAL_SETTINGS).read_text(encoding="utf-8")) or {}
            z.writestr("settings.local.yaml", yaml.safe_dump(redact(local), allow_unicode=True, sort_keys=False))
        except Exception as e:                                 # noqa: BLE001
            z.writestr("settings.local.yaml", f"# нет файла или ошибка: {e}\n")
    log.info("Диагностика собрана: %s", out)
    return out
