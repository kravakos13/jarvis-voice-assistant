"""Air raid alerts (Ukraine) for a configured oblast/raion. Announces only state changes;
always spoken (critical priority — ignores game/night modes).

Sources (jarvis/alerts_sources.py, automatic fallback, backend "auto" by default):
  alerts_in_ua — https://api.alerts.in.ua (official, free token on request) — only if a token is set
  siren        — https://siren.pp.ua/api/v3 (no key, districts + reasons)
  ubilling     — https://ubilling.net.ua/aerialalerts/ (no key, oblast level only)
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

import requests

from .. import alerts_sources as asrc
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
    DOWN_NOTICE_S = 180              # all sources dead this long -> one quiet notice (not spoken)

    def __init__(self, ctx, session=requests, start: bool = True, provider: Optional[asrc.AlertProvider] = None):
        super().__init__(ctx)
        self.cfg = ctx.cfg.get("alerts", {})
        self.oblast = self.cfg.get("oblast", "Сумська область")
        self.raions = list(self.cfg.get("raions") or [])          # empty = whole oblast
        if provider is None:
            cache = None
            try:
                from .. import platform_utils as pu
                cache = pu.data_dir("cache")
            except Exception:
                pass
            provider = asrc.AlertProvider(self.cfg, session=session, cache_dir=cache)
        self.provider = provider
        self._lock = threading.Lock()
        self.state: Optional[bool] = None
        self.places: list = []
        self.reasons: list = []
        self.coarse = False
        self.source = ""
        self._down_since: Optional[float] = None
        self._down_told = False
        self._stop = threading.Event()
        if start:
            threading.Thread(target=self._loop, name="jarvis-alerts", daemon=True).start()

    def fetch(self) -> tuple[bool, list, list]:
        """-> (active, places (ua), reasons). Raises asrc.SourceError (short Russian text) if all sources fail."""
        with self._lock:
            st = self.provider.fetch()
        self.coarse, self.source = st.coarse, st.source
        return st.active, st.places, st.reasons

    def summary(self) -> tuple[Optional[bool], str]:
        with self._lock:
            return self.provider.summary()

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
        if getattr(self, "coarse", False) and getattr(self, "raions", None):
            tail += " Район уточнить не могу — основной источник недоступен."
        return f"Внимание! Воздушная тревога в {where}!{tail}{self._reason_text(reasons)}"

    def poll_once(self, now: Optional[float] = None) -> Optional[str]:
        """One poll; returns the spoken text if something was announced."""
        now = time.monotonic() if now is None else now
        try:
            active, places, reasons = self.fetch()
        except Exception as e:
            if self._down_since is None:
                self._down_since = now
                log.warning("Тревоги: %s", asrc.short_reason(e))
            elif not self._down_told and now - self._down_since >= self.DOWN_NOTICE_S:
                self._down_told = True
                self.ctx.notify("Воздушные тревоги", "Нет связи с источниками тревог — проверь интернет. "
                                "Буду пробовать дальше.")
            return None
        if self._down_told:
            self.ctx.notify("Воздушные тревоги", "Связь с источником тревог восстановлена ("
                            + asrc.TITLES.get(self.source, self.source) + ").")
        self._down_since, self._down_told = None, False
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
            return text
        return None

    def _loop(self):
        interval = max(10.0, float(self.cfg.get("interval_s", 20)))
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("alerts loop")
            self._stop.wait(interval)

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if ASK_RE.search(t):
            if self.state is None:
                return ToolResult("Пока нет данных о тревогах — источники недоступны, проверь интернет.")
            return ToolResult(self.describe(self.state, self.places, self.reasons) if self.state
                              else f"Сейчас в {ru_place(self.oblast)} тревоги нет.")
        return None
