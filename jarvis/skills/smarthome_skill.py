"""Smart home voice commands."""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..smarthome import SmartHome, spoken_reading
from .base import Skill, ToolResult, fn, s

log = logging.getLogger(__name__)

SWITCH_RE = re.compile(r"^(включи|выключи|отключи|вруби|выруби|зажги|погаси)\s+(.+)$")
SENSOR_RE = re.compile(r"\b(какая|какой|сколько|скажи|что показывает)\b.*\b(температур\w*|влажност\w*|давлени\w*|датчик\w*|градус\w*)\b(.*)$")


class SmartHomeSkill(Skill):
    name = "smarthome"

    def __init__(self, ctx):
        super().__init__(ctx)
        path = ctx.project_root / ctx.cfg.get("smarthome", {}).get("devices_file", "devices.yaml")
        self.home = SmartHome(path, ctx.cfg.get("smarthome", {}))

    def switch(self, target: str, on: bool) -> Optional[ToolResult]:
        d = self.home.find(target)
        if not d:
            return None
        try:
            self.home.set_state(d, on)
        except Exception as e:
            log.error("smarthome %s: %s", d.id, e)
            return ToolResult(f"Не достучался до устройства {d.name}.")
        return ToolResult(f"{d.name}: {'включил' if on else 'выключил'}.")

    def sensor(self, target: str) -> Optional[ToolResult]:
        d = self.home.find(target, want_sensor=True) or (
            next((x for x in self.home.devices if x.type == "sensor"), None) if len([x for x in self.home.devices if x.type == "sensor"]) == 1 else None)
        if not d:
            return None
        try:
            return ToolResult(spoken_reading(d, self.home.read(d)))
        except Exception as e:
            log.error("sensor %s: %s", d.id, e)
            return ToolResult(f"Датчик {d.name} не отвечает.")

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if not self.home.devices:
            return None
        m = SWITCH_RE.match(t)
        if m:
            return self.switch(m.group(2), m.group(1) in ("включи", "вруби", "зажги"))
        m = SENSOR_RE.search(t)
        if m and not re.search(r"на улице|погод|видеокарт|процессор", t):
            return self.sensor(t)
        return None

    def tools(self):
        if not self.home.devices:
            return []
        names = ", ".join(d.name for d in self.home.devices)
        return [fn("smart_device", f"Управление умным домом. Устройства: {names}.",
                   {"device": s("название устройства"), "action": s("действие", ["on", "off", "read"])},
                   ["device", "action"])]

    def call(self, name, args):
        if name == "smart_device":
            dev, act = str(args.get("device", "")), args.get("action", "read")
            r = self.sensor(dev) if act == "read" else self.switch(dev, act == "on")
            return r or ToolResult(f"Не нашёл устройство {dev}.")
        return None
