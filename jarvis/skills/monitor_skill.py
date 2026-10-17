"""PC temperature/load monitoring with spoken + toast warnings (cooldown)."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from typing import Optional

import requests

from .. import platform_utils as pu
from .core import pct, units
from .base import Skill, ToolResult

log = logging.getLogger(__name__)

TEMP_RE = re.compile(r"^(какая |скажи |какие )?(температур\w*)( у| в)?( компьютер\w*| компа| пк| процессор\w*| видеокарт\w*| железа| системы)?$|"
                     r"\bтемпература (процессора|видеокарты|компьютера|пк|железа)\b|\bне греется ли\b|\bкак (там )?(железо|комп)\b")


def gpu_stats() -> Optional[dict]:
    smi = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe" if pu.IS_WIN else None)
    if not smi:
        return None
    try:
        kw = {"creationflags": 0x08000000} if pu.IS_WIN else {}   # CREATE_NO_WINDOW
        r = subprocess.run([smi, "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, **kw)
        name, temp, util, mu, mt = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
        return {"name": name, "temp": float(temp), "load": float(util), "mem_used": float(mu), "mem_total": float(mt)}
    except Exception as e:
        log.debug("nvidia-smi: %s", e)
        return None


def cpu_temp_lhm(url: str) -> Optional[float]:
    """LibreHardwareMonitor web server (Options -> Remote Web Server -> Run), default http://localhost:8085/data.json"""
    try:
        data = requests.get(url, timeout=2).json()
    except Exception:
        return None
    best = None

    def walk(node, in_cpu=False):
        nonlocal best
        text = str(node.get("Text", ""))
        in_cpu = in_cpu or bool(re.search(r"cpu|ryzen|intel|core i", text, re.I))
        if in_cpu and "°C" in str(node.get("Value", "")) and re.search(r"package|tctl|tdie|core \(tctl|average|cpu", text, re.I):
            try:
                v = float(str(node["Value"]).split()[0].replace(",", "."))
                best = max(best or 0, v)
            except ValueError:
                pass
        for ch in node.get("Children", []) or []:
            walk(ch, in_cpu)
    walk(data)
    return best


def cpu_temp_wmi() -> Optional[float]:
    """MSAcpi_ThermalZoneTemperature — often unavailable or requires admin; best effort."""
    if not pu.IS_WIN:
        return None
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
                            "-ErrorAction Stop | Select -First 1).CurrentTemperature"],
                           capture_output=True, text=True, timeout=8, creationflags=0x08000000)
        v = float(r.stdout.strip())
        return round(v / 10 - 273.15, 1)
    except Exception:
        return None


def cpu_temp_psutil() -> Optional[float]:
    try:
        import psutil
        temps = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
        vals = [t.current for arr in temps.values() for t in arr if t.current]
        return max(vals) if vals else None
    except Exception:
        return None


class MonitorSkill(Skill):
    name = "monitor"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.cfg = ctx.cfg.get("monitor", {})
        self.last: dict = {}
        self._last_warn: dict = {}
        self._stop = threading.Event()
        threading.Thread(target=self._loop, name="jarvis-monitor", daemon=True).start()

    def read(self) -> dict:
        out = {}
        try:
            import psutil
            out["cpu_load"] = psutil.cpu_percent(interval=0.3)
            out["ram"] = psutil.virtual_memory().percent
        except Exception:
            pass
        g = gpu_stats()
        if g:
            out.update({"gpu_name": g["name"], "gpu_temp": g["temp"], "gpu_load": g["load"]})
        t = cpu_temp_lhm(self.cfg.get("lhm_url", "http://localhost:8085/data.json")) or cpu_temp_psutil()
        if t is None and self.cfg.get("wmi_fallback", True):
            t = cpu_temp_wmi()
        if t:
            out["cpu_temp"] = t
        self.last = out
        emit = self.ctx.extra.get("assistant")
        if emit:
            emit.emit("pcstats", **out)
        return out

    def _warn(self, key: str, text: str) -> None:
        cooldown = float(self.cfg.get("cooldown_min", 10)) * 60
        if time.time() - self._last_warn.get(key, 0) < cooldown:
            return
        self._last_warn[key] = time.time()
        self.ctx.notify("Джарвис: перегрев", text)
        self.ctx.say(text, "critical")

    def _loop(self):
        interval = float(self.cfg.get("interval_s", 30))
        while not self._stop.wait(interval):
            try:
                s = self.read()
                if s.get("gpu_temp", 0) >= float(self.cfg.get("gpu_temp_warn", 83)):
                    self._warn("gpu", f"Внимание, видеокарта греется: {s['gpu_temp']:.0f} градусов.")
                if s.get("cpu_temp", 0) >= float(self.cfg.get("cpu_temp_warn", 90)):
                    self._warn("cpu", f"Внимание, процессор перегревается: {s['cpu_temp']:.0f} градусов.")
            except Exception as e:
                log.debug("monitor: %s", e)

    def spoken(self) -> str:
        s = self.read()
        parts = []
        if "cpu_temp" in s:
            parts.append(f"процессор {s['cpu_temp']:.0f} градусов")
        if "cpu_load" in s:
            parts.append(f"загрузка процессора {pct(s['cpu_load'])}")
        if "gpu_temp" in s:
            parts.append(f"видеокарта {units(s['gpu_temp'], 'градус', 'градуса', 'градусов')}, загрузка {pct(s['gpu_load'])}")
        if "ram" in s:
            parts.append(f"память занята на {pct(s['ram'])}")
        if not parts:
            return "Не могу прочитать датчики."
        txt = ", ".join(parts)
        return txt[0].upper() + txt[1:] + "."

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if TEMP_RE.search(t) and not re.search(r"улиц|погод|комнат|датчик|дом", t):
            return ToolResult(self.spoken())
        return None
