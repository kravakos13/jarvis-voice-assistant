"""Air-raid alert sources with automatic fallback (1.0.3).

1.0.2 relied on siren.pp.ua alone: when the PC could not resolve that one host (ISP DNS, network not up yet at
login) the alerts silently stopped. Now ``AlertProvider`` tries several independent public sources in order:

  alerts_in_ua  api.alerts.in.ua      official, district detail + threat types; ONLY if the user set a token
  siren         siren.pp.ua           no key; districts/hromadas + reasons                  (default primary)
  ubilling      ubilling.net.ua       no key; oblast level ("alert anywhere in the oblast")  (fallback)

alerts.com.ua/api/states is deliberately NOT used: it only reports whole-oblast alerts and stays "no alert"
while a district of the oblast is under alert (would cause a false all-clear).

Robustness: short timeouts, per-source cool-down after failures (exponential, max 10 min), the siren region
tree cached on disk for a week, the last good result kept with its time and source. Errors are mapped to short
Russian reasons; raw exceptions go to the log only. Every network call takes an injectable ``session``.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

TIMEOUT = (5, 10)                    # connect, read
TOKEN_SECRET = "alerts_in_ua_token"  # DPAPI/keyring name (Settings stores it there, not in YAML)
UA = {"User-Agent": "Jarvis-voice-assistant (+https://github.com/kravakos13/jarvis-voice-assistant)"}


@dataclass
class AlertStatus:
    active: bool
    places: list = field(default_factory=list)     # Ukrainian names of alerted regions
    reasons: list = field(default_factory=list)    # raw reason / threat strings
    source: str = ""                               # source key ("siren", ...)
    coarse: bool = False                           # oblast-level only (cannot tell which district)
    at: float = 0.0                                # time.time() of the fetch


class SourceError(Exception):
    """Short Russian reason, safe to show to the user."""


def short_reason(e: BaseException) -> str:
    """Raw exception -> short Russian reason (no URLs, no stack, no host internals)."""
    if isinstance(e, SourceError):
        return str(e)
    text = f"{type(e).__name__} {e}".lower()
    if isinstance(e, requests.HTTPError) and e.response is not None:
        code = e.response.status_code
        if code in (401, 403):
            return "токен не принят" if code == 401 else "доступ запрещён"
        if code == 429:
            return "слишком частые запросы"
        return f"сервер ответил ошибкой {code}"
    if "nameresolution" in text or "getaddrinfo" in text or "name or service not known" in text \
            or "nodename nor servname" in text or "temporary failure in name resolution" in text:
        return "адрес не находится (DNS) — нет интернета или его блокирует провайдер"
    if isinstance(e, requests.Timeout) or "timed out" in text or "timeout" in text:
        return "не отвечает (таймаут)"
    if isinstance(e, requests.exceptions.SSLError) or "ssl" in text or "certificate" in text:
        return "ошибка защищённого соединения"
    if isinstance(e, requests.ConnectionError):
        return "нет соединения"
    if isinstance(e, (ValueError, KeyError, TypeError)):
        return "непонятный ответ"
    return "ошибка связи"


def _get(session, url: str, **kw):
    headers = dict(UA)
    headers.update(kw.pop("headers", {}) or {})
    r = session.get(url, timeout=kw.pop("timeout", TIMEOUT), headers=headers, **kw)
    if r.status_code != 304:
        r.raise_for_status()
    return r


# ---------------------------------------------------------------------------------------------- sources
class Source:
    key = ""
    title = ""
    coarse = False

    def __init__(self, oblast: str, raions: list, session=requests, cache_dir: Optional[Path] = None,
                 clock: Callable[[], float] = time.time):
        self.oblast, self.raions, self.session, self.cache_dir, self.clock = oblast, list(raions or []), session, \
            cache_dir, clock

    def available(self) -> bool:
        return True

    def fetch(self) -> AlertStatus:
        raise NotImplementedError


class SirenSource(Source):
    """https://siren.pp.ua/api/v3 — /regions (tree, cached) + /alerts (active alerts, AIR + reasons)."""
    key, title = "siren", "siren.pp.ua"
    BASE = "https://siren.pp.ua/api/v3"
    TREE_TTL = 7 * 86400

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._ids: Optional[set] = None

    def _tree_file(self) -> Optional[Path]:
        return self.cache_dir / "siren_regions.json" if self.cache_dir else None

    def _load_tree(self) -> dict:
        f = self._tree_file()
        fresh = None
        if f and f.exists() and self.clock() - f.stat().st_mtime < self.TREE_TTL:
            try:
                fresh = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                fresh = None
        if fresh:
            return fresh
        try:
            data = _get(self.session, f"{self.BASE}/regions", timeout=(5, 20)).json()
            if not isinstance(data, dict) or not data.get("states"):
                raise SourceError("непонятный ответ")
            if f:
                try:
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
            return data
        except Exception:
            if f and f.exists():                       # stale cache beats nothing
                try:
                    return json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    pass
            raise

    def ids(self) -> set:
        if self._ids is None:
            data = self._load_tree()
            ids: set = set()

            def walk(node: dict, inside: bool, parents: str) -> None:
                inside = inside or node.get("regionName") == self.oblast
                if inside:
                    ok = (not self.raions or node.get("regionType") == "State" or node.get("regionName") in self.raions
                          or any(r in parents for r in self.raions))
                    if ok:
                        ids.add(str(node.get("regionId")))
                for ch in node.get("regionChildIds") or []:
                    walk(ch, inside, parents + "|" + str(node.get("regionName", "")))
            for st in data.get("states", []):
                walk(st, False, "")
            if not ids:
                raise SourceError(f"область «{self.oblast}» не найдена")
            self._ids = ids
        return self._ids

    def fetch(self) -> AlertStatus:
        ids = self.ids()
        data = _get(self.session, f"{self.BASE}/alerts").json()
        if not isinstance(data, list):
            raise SourceError("непонятный ответ")
        hits = [x for x in data if str(x.get("regionId")) in ids
                and any(a.get("type") == "AIR" for a in x.get("activeAlerts") or [])]
        reasons = [str(lvl.get("reason", "")) for x in hits for a in x.get("activeAlerts") or []
                   for lvl in a.get("activeAlertLevels") or []]
        return AlertStatus(bool(hits), sorted({x.get("regionName", "") for x in hits}), reasons, self.key,
                           False, self.clock())


class UbillingSource(Source):
    """https://ubilling.net.ua/aerialalerts/ — {"states": {"Сумська область": {"alertnow": bool}}}.
    alertnow = air alert anywhere in the oblast (incl. a single district) -> safe superset when raions are set."""
    key, title, coarse = "ubilling", "ubilling.net.ua", True
    URL = "https://ubilling.net.ua/aerialalerts/"

    def fetch(self) -> AlertStatus:
        data = _get(self.session, self.URL).json()
        states = data.get("states") if isinstance(data, dict) else None
        if not isinstance(states, dict) or len(states) < 20:
            raise SourceError("непонятный ответ")
        st = states.get(self.oblast)
        if not isinstance(st, dict) or "alertnow" not in st:
            raise SourceError(f"нет данных по «{self.oblast}»")
        active = bool(st.get("alertnow"))
        return AlertStatus(active, [self.oblast] if active else [], [], self.key, True, self.clock())


class AlertsInUaSource(Source):
    """https://api.alerts.in.ua/v1/alerts/active.json (Bearer token; 8-12 req/min; If-Modified-Since)."""
    key, title = "alerts_in_ua", "alerts.in.ua"
    URL = "https://api.alerts.in.ua/v1/alerts/active.json"
    MIN_INTERVAL = 15.0                       # stay well under the soft limit (8-10/min)

    def __init__(self, *a, token_getter: Callable[[], str] = lambda: "", **kw):
        super().__init__(*a, **kw)
        self.token_getter = token_getter
        self._last_mod = ""
        self._cached: Optional[AlertStatus] = None
        self._last_call = 0.0

    def available(self) -> bool:
        return bool(self.token_getter())

    def fetch(self) -> AlertStatus:
        token = self.token_getter()
        if not token:
            raise SourceError("нет токена")
        if self._cached is not None and self.clock() - self._last_call < self.MIN_INTERVAL:
            return AlertStatus(**{**self._cached.__dict__, "at": self.clock()})
        headers = {"Authorization": f"Bearer {token}"}
        if self._last_mod and self._cached is not None:
            headers["If-Modified-Since"] = self._last_mod
        self._last_call = self.clock()
        r = _get(self.session, self.URL, headers=headers)
        if r.status_code == 304 and self._cached is not None:
            return AlertStatus(**{**self._cached.__dict__, "at": self.clock()})
        data = r.json()
        if not isinstance(data, dict) or not isinstance(data.get("alerts"), list):
            raise SourceError("непонятный ответ")
        hits = [a for a in data["alerts"] if a.get("alert_type", "air_raid") == "air_raid"
                and a.get("location_oblast") == self.oblast
                and (not self.raions or a.get("location_type") == "oblast" or a.get("location_raion") in self.raions
                     or a.get("location_title") in self.raions)]
        reasons = [str(t.get("threat_type", "")) + " " + str(t.get("source_message", ""))
                   for a in hits for t in a.get("threats") or []]
        st = AlertStatus(bool(hits), sorted({a.get("location_title", "") for a in hits}), reasons, self.key,
                         False, self.clock())
        self._cached, self._last_mod = st, r.headers.get("Last-Modified", "") or ""
        return st


SOURCES: dict = {"alerts_in_ua": AlertsInUaSource, "siren": SirenSource, "ubilling": UbillingSource}
TITLES = {k: v.title for k, v in SOURCES.items()}
DEFAULT_ORDER = ["alerts_in_ua", "siren", "ubilling"]


def order_for(backend: str) -> list:
    """'auto' (default) -> alerts_in_ua (if token) -> siren -> ubilling. A specific backend goes first; the
    others stay as fallbacks so a single dead host never silences alerts."""
    b = (backend or "auto").strip().lower()
    if b not in SOURCES:
        return list(DEFAULT_ORDER)
    return [b] + [k for k in DEFAULT_ORDER if k != b]


def stored_token(cfg_alerts: dict) -> str:
    """Token from DPAPI/keyring (Settings), falling back to a legacy plain value in the YAML."""
    try:
        from .secrets import get_secret
        tok = get_secret(TOKEN_SECRET)
        if tok:
            return tok.strip()
    except Exception:
        pass
    return str(cfg_alerts.get("alerts_in_ua_token") or "").strip()


class AllSourcesDown(SourceError):
    pass


class AlertProvider:
    """Tries the sources in order; skips ones in cool-down; remembers the last good status."""

    def __init__(self, cfg_alerts: dict, session=requests, cache_dir: Optional[Path] = None,
                 clock: Callable[[], float] = time.time, token_getter: Optional[Callable[[], str]] = None,
                 sources: Optional[list] = None):
        self.cfg = cfg_alerts or {}
        self.oblast = self.cfg.get("oblast", "Сумська область")
        self.raions = list(self.cfg.get("raions") or [])
        self.clock = clock
        tg = token_getter or (lambda: stored_token(self.cfg))
        if sources is None:
            sources = []
            for k in order_for(str(self.cfg.get("backend", "auto"))):
                kw = {"token_getter": tg} if k == "alerts_in_ua" else {}
                sources.append(SOURCES[k](self.oblast, self.raions, session=session, cache_dir=cache_dir,
                                          clock=clock, **kw))
        self.sources = sources
        self.errors: dict = {}                    # key -> short reason of the last failure
        self._fails: dict = {}
        self._until: dict = {}                    # key -> clock() until which the source is skipped
        self.last: Optional[AlertStatus] = None
        self.last_source = ""

    def _cooldown(self, key: str) -> float:
        n = self._fails.get(key, 0)
        return min(600.0, 30.0 * (2 ** max(0, n - 1)))      # 30 s, 60, 120 ... 10 min

    def fetch(self) -> AlertStatus:
        now = self.clock()
        usable = [s for s in self.sources if s.available()]
        if not usable:
            raise AllSourcesDown("не настроен ни один источник")
        ready = [s for s in usable if self._until.get(s.key, 0) <= now] or usable   # all cooling -> try all
        for s in ready:
            try:
                st = s.fetch()
            except Exception as e:
                reason = short_reason(e)
                self.errors[s.key] = reason
                self._fails[s.key] = self._fails.get(s.key, 0) + 1
                self._until[s.key] = now + self._cooldown(s.key)
                n = self._fails[s.key]
                (log.warning if n == 1 or n % 60 == 0 else log.debug)(
                    "Тревоги: %s — %s (сбой %d подряд; %r)", s.title, reason, n, e)
                continue
            if self._fails.get(s.key):
                log.info("Тревоги: %s снова отвечает", s.title)
            self._fails[s.key], self._until[s.key] = 0, 0
            self.errors.pop(s.key, None)
            if self.last_source and st.source != self.last_source:
                log.info("Тревоги: источник %s -> %s", TITLES.get(self.last_source), s.title)
            self.last, self.last_source = st, st.source
            return st
        raise AllSourcesDown("; ".join(f"{TITLES.get(k, k)} — {v}" for k, v in self.errors.items()))

    def summary(self) -> tuple[Optional[bool], str]:
        """Short Russian status for diagnostics. ok: True (primary works) / None (fallback in use) / False."""
        try:
            st = self.fetch()
        except AllSourcesDown as e:
            return False, f"Нет связи ни с одним источником тревог ({e}). Проверь интернет."
        state = "сейчас ТРЕВОГА" if st.active else "тревоги нет"
        if st.coarse and self.raions and st.active:
            state = "сейчас ТРЕВОГА в области (район этот источник не уточняет)"
        primary = next((s for s in self.sources if s.available()), None)
        title = TITLES.get(st.source, st.source)
        if primary is not None and st.source != primary.key:
            why = self.errors.get(primary.key, "не отвечает")
            return None, f"{primary.title} недоступен ({why}) — работаю через запасной {title}; {state}."
        return True, f"Работает ({title}) — {state}."
