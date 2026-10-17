"""Air raid alerts (Ukraine) for a configured oblast/raion. Announces only state changes;
always spoken (critical priority — ignores game/night modes).

Backends:
  siren      — https://siren.pp.ua/api/v3 (public mirror of ukrainealarm data, no key)   [default]
  alerts_in_ua — https://api.alerts.in.ua (official partner API, free token on request)
  ubilling   — https://ubilling.net.ua/aerialalerts/ (no key, oblast level only)
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Optional

import requests

from .base import Skill, ToolResult

log = logging.getLogger(__name__)

ASK_RE = re.compile(r"\b(есть ли|какая|что с|статус)\b.*\b(тревог\w*)\b|^(тревога|тревога есть)\??$|\bтревога (сейчас|в сумах|в области)\b")

UA_RU = {"Сумська область": "Сумской области", "Сумський район": "Сумском районе", "Шосткинський район": "Шосткинском районе",
         "Конотопський район": "Конотопском районе", "Охтирський район": "Ахтырском районе", "Роменський район": "Роменском районе"}
REASONS = {"ракет": "ракетная угроза", "балісти": "угроза баллистики", "дрон": "угроза дронов", "шахед": "угроза дронов",
           "авіа": "угроза авиабомб", "кас": "угроза КАБов", "артил": "угроза артобстрела"}


def ru_place(ua: str) -> str:
    if ua in UA_RU:
        return UA_RU[ua]
    m = re.match(r"(.+)ський район", ua)
    if m:
        return f"{m.group(1)}ском районе"
    return ua


class AlertsSkill(Skill):
    name = "alerts"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.cfg = ctx.cfg.get("alerts", {})
        self.oblast = self.cfg.get("oblast", "Сумська область")
        self.raions = list(self.cfg.get("raions") or [])          # empty = whole oblast
        self.backend = self.cfg.get("backend", "siren")
        self.state: Optional[bool] = None
        self.places: list = []
        self.reasons: list = []
        self._ids: Optional[set] = None
        self._names: dict = {}
        self._stop = threading.Event()
        threading.Thread(target=self._loop, name="jarvis-alerts", daemon=True).start()

    # ------------------------------------------------------------ backends
    def _siren_ids(self) -> set:
        if self._ids is None:
            data = requests.get("https://siren.pp.ua/api/v3/regions", timeout=15).json()
            ids, names = set(), {}

            def walk(node, inside):
                inside = inside or node.get("regionName") == self.oblast
                if inside:
                    ok = (not self.raions or node.get("regionType") == "State" or node.get("regionName") in self.raions
                          or any(r in str(node.get("_parents", "")) for r in self.raions))
                    if ok:
                        ids.add(str(node["regionId"]))
                        names[str(node["regionId"])] = node.get("regionName", "")
                for ch in node.get("regionChildIds") or []:
                    ch["_parents"] = str(node.get("_parents", "")) + "|" + node.get("regionName", "")
                    walk(ch, inside)
            for st in data.get("states", []):
                walk(st, False)
            self._ids, self._names = ids, names
        return self._ids

    def fetch(self) -> tuple[bool, list, list]:
        """-> (active, places (ua), reasons)"""
        if self.backend == "alerts_in_ua":
            token = self.cfg.get("alerts_in_ua_token", "")
            r = requests.get("https://api.alerts.in.ua/v1/alerts/active.json", params={"token": token}, timeout=10)
            r.raise_for_status()
            hits = [a for a in r.json().get("alerts", []) if a.get("location_oblast") == self.oblast
                    and (not self.raions or a.get("location_raion") in self.raions or a.get("location_type") == "oblast")]
            return bool(hits), sorted({a.get("location_title", "") for a in hits}), sorted({a.get("alert_type", "") for a in hits})
        if self.backend == "ubilling":
            st = requests.get("https://ubilling.net.ua/aerialalerts/", timeout=10).json()["states"].get(self.oblast, {})
            return bool(st.get("alertnow")), [self.oblast] if st.get("alertnow") else [], []
        ids = self._siren_ids()
        data = requests.get("https://siren.pp.ua/api/v3/alerts", timeout=10).json()
        hits = [x for x in data if str(x.get("regionId")) in ids and any(a.get("type") == "AIR" for a in x.get("activeAlerts", []))]
        reasons = []
        for x in hits:
            for a in x.get("activeAlerts", []):
                for lvl in a.get("activeAlertLevels", []) or []:
                    reasons.append(str(lvl.get("reason", "")))
        return bool(hits), sorted({x.get("regionName", "") for x in hits}), reasons

    @staticmethod
    def _reason_text(reasons: list) -> str:
        found = []
        low = " ".join(reasons).lower()
        for k, v in REASONS.items():
            if k in low and v not in found:
                found.append(v)
        return (" " + ", ".join(found).capitalize() + ".") if found else ""

    def describe(self, active: bool, places: list, reasons: list) -> str:
        if not active:
            return f"Отбой воздушной тревоги в {ru_place(self.oblast)}."
        where = ru_place(self.oblast)
        sub = [ru_place(p) for p in places if p != self.oblast][:3]
        tail = f" В том числе в {', '.join(sub)}." if sub else ""
        return f"Внимание! Воздушная тревога в {where}!{tail}{self._reason_text(reasons)}"

    def _loop(self):
        interval = max(10.0, float(self.cfg.get("interval_s", 20)))
        fails = 0
        while not self._stop.is_set():
            try:
                active, places, reasons = self.fetch()
                fails = 0
                changed = self.state is not None and active != self.state
                first_active = self.state is None and active and self.cfg.get("announce_on_start", True)
                self.state, self.places, self.reasons = active, places, reasons
                a = self.ctx.extra.get("assistant")
                if a:
                    a.emit("alert", active=active, places=places)
                if changed or first_active:
                    text = self.describe(active, places, reasons)
                    self.ctx.notify("Воздушная тревога" if active else "Отбой тревоги", text)
                    self.ctx.say(text, "critical")
            except Exception as e:
                fails += 1
                if fails in (1, 10):
                    log.warning("Тревоги: источник недоступен (%s)", e)
            self._stop.wait(interval)

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if ASK_RE.search(t):
            if self.state is None:
                return ToolResult("Пока нет данных о тревогах — источник недоступен.")
            return ToolResult(self.describe(self.state, self.places, self.reasons) if self.state
                              else f"Сейчас в {ru_place(self.oblast)} тревоги нет.")
        return None
