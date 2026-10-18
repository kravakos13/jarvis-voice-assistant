"""Alert sources with mocked HTTP: each source's parser, fallback order, cool-down, caching, short Russian errors,
and the skill's spoken announcements (critical priority)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from jarvis import alerts_sources as asrc

FIX = Path(__file__).parent / "fixtures" / "alerts"
REGIONS = json.loads((FIX / "siren_regions.json").read_text(encoding="utf-8"))
SUMY_RAIONS = ['Шосткинський район', 'Охтирський район', 'Роменський район', 'Сумський район', 'Конотопський район']


def sid(name: str) -> str:
    def walk(n):
        if n["regionName"] == name:
            return str(n["regionId"])
        for c in n["regionChildIds"]:
            r = walk(c)
            if r:
                return r
        return None
    for s in REGIONS["states"]:
        r = walk(s)
        if r:
            return r
    raise KeyError(name)


def siren_alert(name: str, rtype: str = "District", reason: str = "") -> dict:
    lv = [{"alertLevel": "red", "reason": reason, "createdAt": "2026-10-18T11:18:45Z"}] if reason else []
    return {"regionId": sid(name), "regionType": rtype, "regionName": name, "regionEngName": "",
            "lastUpdate": "2026-10-18T11:18:45Z", "activeAlerts": [{"type": "AIR", "activeAlertLevels": lv}]}


UBILLING = {"source": "Vadym Klymenko API (default)", "cachedat": "2026-10-18 14:27:12",
            "states": {n: {"alertnow": False, "changed": "1970-01-01 03:00:00"} for n in
                       ["Вінницька область", "Волинська область", "Дніпропетровська область", "Донецька область",
                        "Житомирська область", "Закарпатська область", "Запорізька область", "Івано-Франківська область",
                        "Київська область", "Кіровоградська область", "Луганська область", "Львівська область",
                        "Миколаївська область", "Одеська область", "Полтавська область", "Рівненська область",
                        "Сумська область", "Тернопільська область", "Харківська область", "Херсонська область",
                        "Хмельницька область", "Черкаська область", "Чернівецька область", "Чернігівська область",
                        "м. Київ", "АР Крим"]}}


class Resp:
    def __init__(self, status=200, data=None, headers=None, text=None):
        self.status_code, self._data, self.headers = status, data, headers or {}
        self.text = text if text is not None else json.dumps(data, ensure_ascii=False)

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)    # type: ignore[arg-type]


class Session:
    """routes: url-prefix -> Resp | Exception | callable(url, kw) -> Resp"""

    def __init__(self, routes: dict):
        self.routes, self.calls = routes, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        for prefix, r in self.routes.items():
            if url.startswith(prefix):
                if isinstance(r, BaseException):
                    raise r
                return r(url, kw) if callable(r) else r
        raise requests.ConnectionError(f"no route {url}")


DNS = requests.ConnectionError("HTTPSConnectionPool(host='siren.pp.ua', port=443): Max retries exceeded "
                               "(Caused by NameResolutionError(\"Failed to resolve 'siren.pp.ua' "
                               "([Errno 11001] getaddrinfo failed)\"))")
SIREN_R = "https://siren.pp.ua/api/v3/regions"
SIREN_A = "https://siren.pp.ua/api/v3/alerts"
UBI = "https://ubilling.net.ua/aerialalerts/"
AIU = "https://api.alerts.in.ua/v1/alerts/active.json"


def cfg(**kw):
    c = {"enabled": True, "backend": "auto", "oblast": "Сумська область", "raions": [], "interval_s": 20}
    c.update(kw)
    return c


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ------------------------------------------------------------------------------- per source parsers
def test_siren_district_alert_and_reasons(tmp_path):
    s = Session({SIREN_R: Resp(200, REGIONS),
                 SIREN_A: Resp(200, [siren_alert("Сумський район", reason="Загроза ударних БпЛА (дрони)"),
                                     siren_alert("Полтавська область", "State")])})
    st = asrc.SirenSource("Сумська область", [], session=s, cache_dir=tmp_path).fetch()
    assert st.active and st.places == ["Сумський район"] and not st.coarse and st.source == "siren"
    assert "дрони" in " ".join(st.reasons)
    assert (tmp_path / "siren_regions.json").exists()            # region tree cached on disk


def test_siren_raion_filter():
    s = Session({SIREN_R: Resp(200, REGIONS), SIREN_A: Resp(200, [siren_alert("Шосткинський район")])})
    assert not asrc.SirenSource("Сумська область", ["Сумський район"], session=s).fetch().active
    assert asrc.SirenSource("Сумська область", ["Шосткинський район"], session=s).fetch().active
    # a whole-oblast (State) alert counts for any raion filter
    s2 = Session({SIREN_R: Resp(200, REGIONS), SIREN_A: Resp(200, [siren_alert("Сумська область", "State")])})
    assert asrc.SirenSource("Сумська область", ["Сумський район"], session=s2).fetch().active


def test_siren_uses_cached_tree_when_regions_endpoint_dies(tmp_path):
    ok = Session({SIREN_R: Resp(200, REGIONS), SIREN_A: Resp(200, [])})
    asrc.SirenSource("Сумська область", [], session=ok, cache_dir=tmp_path).fetch()
    dead_tree = Session({SIREN_R: requests.Timeout("t"), SIREN_A: Resp(200, [siren_alert("Конотопський район")])})
    st = asrc.SirenSource("Сумська область", [], session=dead_tree, cache_dir=tmp_path).fetch()
    assert st.active and not any(u.startswith(SIREN_R) for u, _ in dead_tree.calls)   # fresh cache: no call


def test_siren_bad_payload():
    s = Session({SIREN_R: Resp(200, REGIONS), SIREN_A: Resp(200, {"error": "x"})})
    with pytest.raises(asrc.SourceError):
        asrc.SirenSource("Сумська область", [], session=s).fetch()


def test_ubilling_parser():
    data = json.loads(json.dumps(UBILLING))
    data["states"]["Сумська область"]["alertnow"] = True
    st = asrc.UbillingSource("Сумська область", [], session=Session({UBI: Resp(200, data)})).fetch()
    assert st.active and st.coarse and st.places == ["Сумська область"] and st.source == "ubilling"
    st = asrc.UbillingSource("Сумська область", [], session=Session({UBI: Resp(200, UBILLING)})).fetch()
    assert not st.active


def test_ubilling_rejects_garbage():
    for bad in ({"states": {}}, {"x": 1}, ["a"]):
        with pytest.raises(asrc.SourceError):
            asrc.UbillingSource("Сумська область", [], session=Session({UBI: Resp(200, bad)})).fetch()
    with pytest.raises(asrc.SourceError):          # unknown oblast name
        asrc.UbillingSource("Нет такой", [], session=Session({UBI: Resp(200, UBILLING)})).fetch()


AIU_DATA = {"alerts": [
    {"id": 1, "location_title": "Сумський район", "location_type": "raion", "alert_type": "air_raid",
     "location_oblast": "Сумська область", "location_raion": "Сумський район", "alert_level": "red",
     "threats": [{"threat_type": "drones", "level": "red", "source_message": "Загроза ударних БпЛА"}]},
    {"id": 2, "location_title": "Полтавська область", "location_type": "oblast", "alert_type": "air_raid",
     "location_oblast": "Полтавська область"},
    {"id": 3, "location_title": "Шосткинський район", "location_type": "raion", "alert_type": "artillery_shelling",
     "location_oblast": "Сумська область", "location_raion": "Шосткинський район"}]}


def test_alerts_in_ua_parser_token_header_and_304():
    seen = []

    def route(url, kw):
        seen.append(kw.get("headers", {}))
        if "If-Modified-Since" in kw.get("headers", {}):
            return Resp(304, None, text="")
        return Resp(200, AIU_DATA, headers={"Last-Modified": "Sun, 18 Oct 2026 11:00:00 GMT"})
    clk = Clock()
    src = asrc.AlertsInUaSource("Сумська область", [], session=Session({AIU: route}), clock=clk,
                                token_getter=lambda: "tok123")
    st = src.fetch()
    assert st.active and st.places == ["Сумський район"]                 # artillery alert ignored
    assert "drones" in " ".join(st.reasons)
    assert seen[0]["Authorization"] == "Bearer tok123"
    assert all("tok123" not in u for u, _ in src.session.calls)          # token never in the URL (logs)
    clk.t += 5
    src.fetch()
    assert len(seen) == 1                                                # < 15 s: served from cache (rate limit)
    clk.t += 20
    st2 = src.fetch()
    assert len(seen) == 2 and seen[1]["If-Modified-Since"] and st2.active   # 304 -> cached result


def test_alerts_in_ua_raion_filter_and_no_token():
    src = asrc.AlertsInUaSource("Сумська область", ["Роменський район"], session=Session({AIU: Resp(200, AIU_DATA)}),
                                token_getter=lambda: "t")
    assert not src.fetch().active
    assert not asrc.AlertsInUaSource("Сумська область", [], token_getter=lambda: "").available()


# ------------------------------------------------------------------------------- provider / fallback
def test_order_for():
    assert asrc.order_for("auto") == ["alerts_in_ua", "siren", "ubilling"]
    assert asrc.order_for("") == asrc.order_for("whatever") == asrc.DEFAULT_ORDER
    assert asrc.order_for("ubilling") == ["ubilling", "alerts_in_ua", "siren"]
    assert asrc.order_for("siren")[0] == "siren"


def test_auto_without_token_uses_siren_first():
    s = Session({SIREN_R: Resp(200, REGIONS), SIREN_A: Resp(200, []), UBI: Resp(200, UBILLING)})
    p = asrc.AlertProvider(cfg(), session=s, token_getter=lambda: "")
    st = p.fetch()
    assert st.source == "siren" and not any(u.startswith(AIU) for u, _ in s.calls)
    assert p.summary() == (True, "Работает (siren.pp.ua) — тревоги нет.")


def test_fallback_to_ubilling_on_dns_failure_user_case():
    """The user's 1.0.2 failure: getaddrinfo failed for siren.pp.ua -> ubilling.net.ua keeps alerts working."""
    data = json.loads(json.dumps(UBILLING))
    data["states"]["Сумська область"]["alertnow"] = True
    s = Session({SIREN_R: DNS, SIREN_A: DNS, UBI: Resp(200, data)})
    p = asrc.AlertProvider(cfg(), session=s, token_getter=lambda: "")
    st = p.fetch()
    assert st.source == "ubilling" and st.active and st.coarse
    ok, text = p.summary()
    assert ok is None
    assert text.startswith("siren.pp.ua недоступен (адрес не находится (DNS)")
    assert "запасной ubilling.net.ua" in text and "ТРЕВОГА" in text
    assert "HTTPSConnectionPool" not in text and "getaddrinfo" not in text and "Errno" not in text


def test_token_source_first_then_fallback():
    s = Session({AIU: Resp(401, {"message": "API token required."}), SIREN_R: Resp(200, REGIONS),
                 SIREN_A: Resp(200, [siren_alert("Охтирський район")]), UBI: Resp(200, UBILLING)})
    p = asrc.AlertProvider(cfg(), session=s, token_getter=lambda: "bad")
    st = p.fetch()
    assert st.source == "siren" and st.active
    assert p.errors["alerts_in_ua"] == "токен не принят"
    assert s.calls[0][0].startswith(AIU)


def test_all_down_short_message():
    s = Session({SIREN_R: DNS, SIREN_A: DNS, UBI: requests.Timeout("read timed out")})
    p = asrc.AlertProvider(cfg(), session=s, token_getter=lambda: "")
    with pytest.raises(asrc.AllSourcesDown):
        p.fetch()
    ok, text = p.summary()
    assert ok is False and text.startswith("Нет связи ни с одним источником тревог")
    assert "siren.pp.ua — адрес не находится" in text and "ubilling.net.ua — не отвечает (таймаут)" in text
    assert text.endswith("Проверь интернет.") and "HTTPSConnectionPool" not in text


def test_cooldown_skips_dead_source_then_retries():
    clk = Clock()
    state = {"siren_up": False}

    def siren(url, kw):
        if not state["siren_up"]:
            raise DNS
        return Resp(200, REGIONS) if url.startswith(SIREN_R) else Resp(200, [])
    s = Session({"https://siren.pp.ua": siren, UBI: Resp(200, UBILLING)})
    p = asrc.AlertProvider(cfg(), session=s, clock=clk, token_getter=lambda: "")
    assert p.fetch().source == "ubilling"
    n = len(s.calls)
    clk.t += 10
    assert p.fetch().source == "ubilling"
    assert not any(u.startswith("https://siren") for u, _ in s.calls[n:])     # cooling down: not hammered
    state["siren_up"] = True
    clk.t += 31
    assert p.fetch().source == "siren"                                         # back to the detailed source
    assert "siren" not in p.errors


def test_cooldown_backoff_grows_and_caps():
    p = asrc.AlertProvider(cfg(), session=Session({}), token_getter=lambda: "")
    vals = []
    for n in range(1, 9):
        p._fails["siren"] = n
        vals.append(p._cooldown("siren"))
    assert vals[:3] == [30.0, 60.0, 120.0] and vals[-1] == 600.0


def test_explicit_backend_still_has_fallbacks():
    s = Session({UBI: requests.ConnectionError("x"), SIREN_R: Resp(200, REGIONS), SIREN_A: Resp(200, [])})
    p = asrc.AlertProvider(cfg(backend="ubilling"), session=s, token_getter=lambda: "")
    assert [x.key for x in p.sources] == ["ubilling", "alerts_in_ua", "siren"]
    assert p.fetch().source == "siren"


def test_short_reason_mapping():
    r = Resp(429, {})
    assert asrc.short_reason(requests.HTTPError(response=r)) == "слишком частые запросы"   # type: ignore[arg-type]
    assert asrc.short_reason(requests.HTTPError(response=Resp(503, {}))) == "сервер ответил ошибкой 503"  # type: ignore[arg-type]
    assert asrc.short_reason(DNS).startswith("адрес не находится")
    assert asrc.short_reason(requests.exceptions.SSLError("bad cert")) == "ошибка защищённого соединения"
    assert asrc.short_reason(ValueError("Expecting value")) == "непонятный ответ"
    assert asrc.short_reason(requests.ConnectionError("refused")) == "нет соединения"


def test_stored_token_prefers_secret(monkeypatch):
    import jarvis.secrets as sec
    monkeypatch.setattr(sec, "get_secret", lambda n: "from-dpapi" if n == asrc.TOKEN_SECRET else "")
    assert asrc.stored_token({"alerts_in_ua_token": "yaml"}) == "from-dpapi"
    monkeypatch.setattr(sec, "get_secret", lambda n: "")
    assert asrc.stored_token({"alerts_in_ua_token": " yaml "}) == "yaml"


# ------------------------------------------------------------------------------- skill: always spoken
class Ctx:
    def __init__(self, alerts):
        self.cfg = {"alerts": alerts}
        self.extra: dict = {}
        self.said: list = []
        self.notes: list = []

    def say(self, text, prio="normal"):
        self.said.append((text, prio))

    def notify(self, title, text):
        self.notes.append((title, text))


def make_skill(provider_session, **alerts):
    from jarvis.skills.alerts_skill import AlertsSkill
    a = cfg(**alerts)
    ctx = Ctx(a)
    prov = asrc.AlertProvider(a, session=provider_session, token_getter=lambda: "")
    return AlertsSkill(ctx, start=False, provider=prov), ctx


def test_skill_speaks_alert_and_all_clear_critical():
    alerts = {"list": [siren_alert("Сумський район", reason="ракетна небезпека")]}
    s = Session({SIREN_R: Resp(200, REGIONS), SIREN_A: lambda u, k: Resp(200, alerts["list"])})
    sk, ctx = make_skill(s)
    text = sk.poll_once(0)
    assert text and text.startswith("Внимание! Воздушная тревога в Сумской области!")
    assert "Сумском районе" in text and "ракетная угроза" in text.lower()
    assert ctx.said[-1][1] == "critical"
    assert sk.poll_once(20) is None                       # no change -> silent
    alerts["list"] = []
    assert sk.poll_once(40) == "Отбой воздушной тревоги в Сумской области."
    assert ctx.said[-1] == ("Отбой воздушной тревоги в Сумской области.", "critical")


def test_skill_coarse_fallback_wording_with_raions():
    data = json.loads(json.dumps(UBILLING))
    data["states"]["Сумська область"]["alertnow"] = True
    s = Session({SIREN_R: DNS, SIREN_A: DNS, UBI: Resp(200, data)})
    sk, ctx = make_skill(s, raions=["Сумський район"])
    text = sk.poll_once(0)
    assert text and "Район уточнить не могу" in text and ctx.said[-1][1] == "critical"


def test_skill_quiet_notice_when_all_down_and_recovery():
    state = {"up": False}

    def ubi(url, kw):
        if not state["up"]:
            raise DNS
        return Resp(200, UBILLING)
    s = Session({"https://siren.pp.ua": DNS, UBI: ubi})
    sk, ctx = make_skill(s)
    assert sk.poll_once(0) is None and sk.poll_once(100) is None and not ctx.notes
    sk.poll_once(200)
    assert ctx.notes and "Нет связи с источниками тревог" in ctx.notes[-1][1]
    assert not ctx.said                                   # outage notice is not spoken
    state["up"] = True
    sk.provider._until.clear()
    sk.poll_once(300)
    assert "восстановлена" in ctx.notes[-1][1]
    m = sk.match("есть ли тревога", "есть ли тревога")
    assert m is not None and "тревоги нет" in m.reply


def test_diagnostics_alerts_short_text(monkeypatch):
    from jarvis import diagnostics

    class FakeSkill:
        def summary(self):
            return None, "siren.pp.ua недоступен (адрес не находится (DNS) — нет интернета или его блокирует провайдер) — работаю через запасной ubilling.net.ua; тревоги нет."

    class A:
        class router:
            by_name = {"alerts": FakeSkill()}
    c = diagnostics._alerts({"alerts": cfg()}, A())
    assert c is not None and c.ok is None and "запасной ubilling.net.ua" in c.detail


# ------------------------------------------------------------------------------- HF quiet + progress
def test_hfquiet_env_and_logger(monkeypatch):
    import logging
    import os
    from jarvis import hfquiet
    for k in ("HF_HUB_DISABLE_SYMLINKS_WARNING", "HF_HUB_DISABLE_IMPLICIT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    hfquiet.quiet()
    assert os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] == "1" and os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1"
    assert logging.getLogger("huggingface_hub.utils._http").getEffectiveLevel() >= logging.ERROR


def test_hf_size_monitor(tmp_path):
    from jarvis import hfquiet
    folder = hfquiet.repo_folder(tmp_path, "Systran/faster-whisper-small")
    assert folder.name == "models--Systran--faster-whisper-small"
    out: list = []
    clk = {"t": 0.0}
    mon = hfquiet.SizeMonitor(folder, total=4 << 20, out=out.append, clock=lambda: clk["t"])
    (folder / "blobs").mkdir(parents=True)
    (folder / "blobs" / "x.incomplete").write_bytes(b"\0" * (2 << 20))
    clk["t"] = 6
    assert mon.tick() == "  Whisper: 50% (2 из 4 МБ), 6 с"
    mon.total = 0
    assert "скачано 2 МБ" in mon.tick() and "не закрывай окно" in out[-1]


def test_hf_expected_size():
    from jarvis import hfquiet
    tree = [{"type": "file", "path": "model.bin", "size": 1, "lfs": {"size": 483546902}},
            {"type": "file", "path": "config.json", "size": 2250}, {"type": "file", "path": "README.md", "size": 999},
            {"type": "file", "path": "vocabulary.txt", "size": 460000}]
    s = Session({"https://huggingface.co/api/models/Systran/faster-whisper-small/tree/main": Resp(200, tree)})
    assert hfquiet.expected_size("Systran/faster-whisper-small", session=s) == 483546902 + 2250 + 460000
    assert hfquiet.expected_size("x/y", session=Session({})) == 0
    assert hfquiet.repo_for("small") == "Systran/faster-whisper-small" and hfquiet.repo_for("a/b") == "a/b"
