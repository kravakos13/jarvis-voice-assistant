"""Switch the Windows default audio output/input by voice (pycaw IPolicyConfig) + Jarvis' own playback."""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..launcher import phonetic, score
from .base import Skill, ToolResult

log = logging.getLogger(__name__)

SWITCH_RE = re.compile(r"^(?:переключи|переключись|выведи|включи|сделай)\s+(?:звук\s+)?(?:на|в)\s+(.+)$")
MIC_RE = re.compile(r"^(?:переключи|поменяй)\s+микрофон\s+на\s+(.+)$")
LIST_RE = re.compile(r"\bкакие (есть )?(аудио)?устройства\b|\bсписок (аудио)?устройств\b|\bкакие (есть )?(колонки|наушники)\b")


def list_endpoints(capture: bool = False) -> list[tuple[str, str]]:
    from pycaw.constants import DEVICE_STATE, EDataFlow
    from pycaw.pycaw import AudioUtilities
    flow = EDataFlow.eCapture.value if capture else EDataFlow.eRender.value
    return [(d.id, d.FriendlyName or "") for d in AudioUtilities.GetAllDevices(flow, DEVICE_STATE.ACTIVE.value)]


def set_default(dev_id: str) -> None:
    from pycaw.constants import ERole
    from pycaw.pycaw import AudioUtilities
    AudioUtilities.SetDefaultDevice(dev_id, [ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])


class AudioDevicesSkill(Skill):
    name = "audio_devices"

    def __init__(self, ctx):
        super().__init__(ctx)
        import pycaw  # noqa: F401  Windows only -> skill disabled elsewhere
        self.aliases = {k.lower(): v for k, v in (ctx.cfg.get("audio_devices", {}).get("aliases") or {}).items()}

    def _find(self, spoken: str, capture: bool):
        spoken = spoken.strip().lower()
        target = self.aliases.get(spoken, spoken)
        devs = list_endpoints(capture)
        best, best_s = None, 0.0
        for dev_id, name in devs:
            sc = score(phonetic(target), phonetic(name))
            if target.lower() in name.lower():
                sc = max(sc, 0.95)
            if sc > best_s:
                best, best_s = (dev_id, name), sc
        return best if best_s >= 0.55 else None, devs

    def _switch(self, spoken: str, capture: bool) -> ToolResult:
        dev, devs = self._find(spoken, capture)
        if not dev:
            names = ", ".join(n for _, n in devs[:6])
            return ToolResult(f"Не нашёл «{spoken}». Есть: {names}. Можно задать псевдонимы в audio_devices.aliases.")
        try:
            set_default(dev[0])
        except Exception as e:
            log.error("SetDefaultDevice: %s", e)
            return ToolResult("Windows не дала переключить устройство.")
        if not capture:
            a = self.ctx.extra.get("assistant")
            if a and a.speaker and a.speaker.player:
                a.speaker.player.device = None      # follow the new Windows default
                try:
                    import sounddevice as sd
                    sd._terminate(); sd._initialize()   # refresh PortAudio device list
                except Exception:
                    pass
        return ToolResult(f"Переключил {'микрофон' if capture else 'звук'} на {dev[1]}.")

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if LIST_RE.search(t):
            outs = ", ".join(n for _, n in list_endpoints(False))
            ins = ", ".join(n for _, n in list_endpoints(True))
            return ToolResult(f"Выходы: {outs}. Микрофоны: {ins}.")
        m = MIC_RE.match(t)
        if m:
            return self._switch(m.group(1), True)
        m = SWITCH_RE.match(t)
        if m and (re.search(r"наушник|колонк|динамик|монитор|телевизор|гарнитур|блютуз|bluetooth|hdmi|realtek|usb", t)
                  or m.group(1).strip().lower() in self.aliases):
            return self._switch(m.group(1), False)
        return None
