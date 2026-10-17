"""Smart home / ESP devices described in devices.yaml.

Protocols: mqtt (paho-mqtt), http (custom URLs), esphome (REST API),
tasmota (HTTP commands), homeassistant (REST API with long-lived token).
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

from .launcher import phonetic, score

log = logging.getLogger(__name__)


@dataclass
class Device:
    id: str
    name: str
    type: str                       # switch | light | sensor
    protocol: str
    aliases: list = field(default_factory=list)
    params: dict = field(default_factory=dict)


def _dig(data: Any, path: str) -> Any:
    """'StatusSNS.DHT11.Temperature' -> nested value."""
    for part in [p for p in str(path).split(".") if p]:
        if isinstance(data, list):
            data = data[int(part)]
        else:
            data = data.get(part) if isinstance(data, dict) else None
        if data is None:
            return None
    return data


class SmartHome:
    def __init__(self, path: Path, cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        self.path = Path(path)
        self.devices: list[Device] = []
        self.mqtt_cfg: dict = {}
        self.ha_cfg: dict = {}
        self._mqtt = None
        self._mqtt_values: dict[str, str] = {}
        self._mqtt_lock = threading.Lock()
        self.timeout = float(self.cfg.get("timeout_s", 5))
        self.load()

    def load(self) -> None:
        import yaml
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        data = data or {}
        self.mqtt_cfg = data.get("mqtt") or {}
        self.ha_cfg = data.get("homeassistant") or {}
        self.devices = []
        for did, d in (data.get("devices") or {}).items():
            d = dict(d)
            self.devices.append(Device(did, d.pop("name", did), d.pop("type", "switch"), d.pop("protocol", "http"),
                                       d.pop("aliases", []) or [], d))

    # ------------------------------------------------------------ lookup
    def find(self, spoken: str, want_sensor: bool = False) -> Optional[Device]:
        qk = phonetic(spoken)
        best, best_s = None, 0.0
        for d in self.devices:
            if want_sensor != (d.type == "sensor"):
                continue
            for name in [d.name] + list(d.aliases):
                sc = score(qk, phonetic(name))
                if phonetic(name) in qk:
                    sc = max(sc, 0.95)
                if sc > best_s:
                    best, best_s = d, sc
        return best if best_s >= 0.7 else None

    # ------------------------------------------------------------ MQTT
    def _mqtt_client(self):
        if self._mqtt is not None:
            return self._mqtt
        import paho.mqtt.client as mqtt
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="jarvis-assistant")
        except AttributeError:   # paho-mqtt < 2
            c = mqtt.Client(client_id="jarvis-assistant")
        if self.mqtt_cfg.get("username"):
            c.username_pw_set(self.mqtt_cfg["username"], self.mqtt_cfg.get("password", ""))

        def on_message(_c, _u, msg):
            with self._mqtt_lock:
                self._mqtt_values[msg.topic] = msg.payload.decode("utf-8", "replace")
        c.on_message = on_message
        c.connect(self.mqtt_cfg.get("host", "localhost"), int(self.mqtt_cfg.get("port", 1883)), 30)
        c.loop_start()
        for d in self.devices:
            for key in ("state_topic", "topic"):
                if d.protocol == "mqtt" and d.params.get(key) and (key == "state_topic" or d.type == "sensor"):
                    c.subscribe(d.params[key])
        self._mqtt = c
        return c

    def _mqtt_read(self, topic: str, wait: float = 3.0) -> Optional[str]:
        import time
        self._mqtt_client().subscribe(topic)
        t0 = time.time()
        while time.time() - t0 < wait:
            with self._mqtt_lock:
                if topic in self._mqtt_values:
                    return self._mqtt_values[topic]
            time.sleep(0.05)
        return None

    # ------------------------------------------------------------ HTTP helpers
    def _http(self, method: str, url: str, **kw):
        r = requests.request(method, url, timeout=self.timeout, **kw)
        r.raise_for_status()
        return r

    def _ha(self, method: str, path: str, body: Optional[dict] = None):
        url = str(self.ha_cfg.get("url", "http://homeassistant.local:8123")).rstrip("/") + path
        headers = {"Authorization": f"Bearer {self.ha_cfg.get('token', '')}", "Content-Type": "application/json"}
        return self._http(method, url, headers=headers, json=body)

    # ------------------------------------------------------------ actions
    def set_state(self, d: Device, on: bool) -> None:
        p = d.params
        if d.protocol == "mqtt":
            payload = p.get("payload_on", "ON") if on else p.get("payload_off", "OFF")
            self._mqtt_client().publish(p["command_topic"], payload, qos=1, retain=bool(p.get("retain", False)))
        elif d.protocol == "esphome":
            domain = p.get("domain", "light" if d.type == "light" else "switch")
            self._http("POST", f"http://{p['host']}/{domain}/{p['entity']}/{'turn_on' if on else 'turn_off'}")
        elif d.protocol == "tasmota":
            relay = p.get("relay", 1)
            cmd = f"Power{relay} {'On' if on else 'Off'}"
            self._http("GET", f"http://{p['host']}/cm", params={"cmnd": cmd})
        elif d.protocol == "homeassistant":
            entity = p["entity_id"]
            domain = entity.split(".")[0]
            self._ha("POST", f"/api/services/{domain}/{'turn_on' if on else 'turn_off'}", {"entity_id": entity})
        elif d.protocol == "http":
            url = p["on_url"] if on else p["off_url"]
            self._http(p.get("method", "GET"), url)
        else:
            raise ValueError(f"Неизвестный протокол {d.protocol}")

    def read(self, d: Device) -> dict:
        """-> {'temperature': 21.5, 'humidity': 40, ...} or {'value': ...}"""
        p = d.params
        fields = p.get("fields") or {"value": p.get("json_path", "")}
        if d.protocol == "mqtt":
            topic = p.get("state_topic") or p.get("topic")
            if not topic:
                raise ValueError(f"У устройства «{d.name}» не указан topic для чтения.")
            raw = self._mqtt_read(str(topic))
            if raw is None:
                raise TimeoutError("датчик не прислал данные по MQTT")
            try:
                data = json.loads(raw)
            except ValueError:
                return {"value": raw}
        elif d.protocol == "esphome":
            out = {}
            for key, entity in (p.get("entities") or {"value": p.get("entity")}).items():
                out[key] = self._http("GET", f"http://{p['host']}/sensor/{entity}").json().get("value")
            return out
        elif d.protocol == "tasmota":
            data = self._http("GET", f"http://{p['host']}/cm", params={"cmnd": "Status 10"}).json()
        elif d.protocol == "homeassistant":
            st = self._ha("GET", f"/api/states/{p['entity_id']}").json()
            return {"value": st.get("state"), "unit": (st.get("attributes") or {}).get("unit_of_measurement", "")}
        else:
            data = self._http(p.get("method", "GET"), p["state_url"]).json()
        return {k: _dig(data, path) if path else data for k, path in fields.items()}


def spoken_reading(d: Device, values: dict) -> str:
    names = {"temperature": ("температура", "градусов"), "humidity": ("влажность", "процентов"),
             "pressure": ("давление", ""), "co2": ("углекислый газ", "ppm")}
    parts = []
    for k, v in values.items():
        if v is None:
            continue
        label, unit = names.get(k, ("значение", values.get("unit", "")))
        try:
            v = round(float(v), 1)
            v = int(v) if v == int(v) else str(v).replace(".", ",")
        except (TypeError, ValueError):
            pass
        if k != "unit":
            parts.append(f"{label} {v} {unit}".strip())
    return f"{d.name}: " + ", ".join(parts) + "." if parts else f"{d.name}: нет данных."
