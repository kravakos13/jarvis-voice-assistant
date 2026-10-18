"""Latency work of 1.0.5: stable prompt prefix, warm-up, placement GPU/CPU, embeddings on CPU,
Ollama server env, per-stage timing, «Секунду» acknowledgement, early first clause."""
from __future__ import annotations

import datetime as dt
import threading
import time
import types

import pytest

from jarvis import llm as L
from jarvis.timing import Timings, Trace


class Resp:
    def __init__(self, data=None, lines=(), status=200):
        self.status_code, self._data, self._lines, self.text = status, data or {}, list(lines), ""

    def json(self):
        return self._data

    def iter_lines(self):
        yield from self._lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


DONE = (b'{"message":{"content":"\\u0425\\u043e\\u0440\\u043e\\u0448\\u043e."},"done":true,'
        b'"load_duration":4000000000,"prompt_eval_count":40,"prompt_eval_duration":100000000,'
        b'"eval_count":30,"eval_duration":1000000000,"total_duration":5200000000}')


class FakeOllama:
    def __init__(self, ps=None):
        self.payloads, self.urls, self.ps = [], [], ps or {"models": []}

    def post(self, url, json=None, stream=False, timeout=None):
        self.urls.append(url)
        self.payloads.append(json)
        if url.endswith("/api/embed"):
            return Resp({"embeddings": [[0.1, 0.2]]})
        if stream:
            return Resp(lines=[DONE])
        return Resp({"message": {"content": "Хорошо."}, "load_duration": 3_000_000_000, "prompt_eval_count": 2300,
                     "prompt_eval_duration": 1_500_000_000})

    def get(self, url, timeout=None):
        return Resp(self.ps)


def chat(**cfg):
    s = FakeOllama(cfg.pop("_ps", None))
    c = L.OllamaChat({"model": "qwen2.5:7b", "system_prompt": "Ты — Джарвис.", **cfg}, session=s)  # type: ignore[arg-type]
    return c, s


TOOLS = [{"type": "function", "function": {"name": "t", "description": "d", "parameters": {}}}]


# ---------------------------------------------------------------- stable prefix (prompt cache)
def test_system_prompt_is_identical_across_minutes_and_memory_hits(monkeypatch):
    c, s = chat()
    hits = iter(["Пользователь любит кофе.", "Кота зовут Барсик."])
    c.context_provider = lambda q: next(hits)
    times = iter([dt.datetime(2026, 10, 18, 14, 10), dt.datetime(2026, 10, 18, 14, 10),
                  dt.datetime(2026, 10, 18, 14, 59), dt.datetime(2026, 10, 18, 14, 59)])

    class FakeDT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return next(times)
    monkeypatch.setattr(L._dt, "datetime", FakeDT)
    list(c.stream("привет", tools=TOOLS))
    list(c.stream("а как кота зовут?", tools=TOOLS))
    a, b = s.payloads
    assert a["messages"][0] == b["messages"][0]                       # system byte-identical
    assert "14:" not in a["messages"][0]["content"] and "кофе" not in a["messages"][0]["content"]
    assert a["tools"] == b["tools"]
    # each request = the previous one + new tokens (prefix reuse in Ollama)
    assert b["messages"][: len(a["messages"])] == a["messages"]
    assert "14:10" in a["messages"][-1]["content"] and "кофе" in a["messages"][-1]["content"]
    assert "14:59" in b["messages"][-1]["content"] and "Барсик" in b["messages"][-1]["content"]
    assert L.strip_note(b["messages"][-1]["content"]) == "а как кота зовут?"


def test_last_stats_from_ollama():
    c, _ = chat()
    list(c.stream("привет"))
    st = c.last_stats
    assert st["load_s"] == 4.0 and st["prompt_tokens"] == 40 and st["tok_s"] == 30.0


def test_main_model_keeps_one_num_ctx_so_ollama_never_reloads():
    c, s = chat(num_ctx=6144)
    c.complete([{"role": "user", "content": "x"}], num_ctx=8192)          # main model: own num_ctx ignored
    assert s.payloads[-1]["options"]["num_ctx"] == 6144
    c.complete([{"role": "user", "content": "x"}], model="qwen2.5-coder:7b", num_ctx=8192)
    coder = [p for p in s.payloads if p["model"] == "qwen2.5-coder:7b"][-1]   # (the main model is re-warmed after it)
    assert coder["options"]["num_ctx"] == 8192 and coder["keep_alive"] == "1m"


def test_aux_model_rewarms_the_main_model_in_background():
    c, s = chat()
    c.complete([{"role": "user", "content": "код"}], model="qwen2.5-coder:7b", lang="no_cjk")
    for _ in range(50):
        if any(p.get("model") == "qwen2.5:7b" and p["options"].get("num_predict") == 1 for p in s.payloads):
            break
        time.sleep(0.02)
    assert any(p.get("model") == "qwen2.5:7b" and p["options"].get("num_predict") == 1 for p in s.payloads)


# ---------------------------------------------------------------- warm-up + placement
def test_warm_up_loads_model_with_the_real_prefix():
    c, s = chat()
    assert c.warm(TOOLS) is not None
    p = s.payloads[-1]
    assert p["tools"] == TOOLS and p["messages"][0] == c._system_message()
    assert p["options"]["num_predict"] == 1 and p["keep_alive"] == "30m"
    assert c.last_stats["load_s"] == 3.0


def test_warm_up_never_raises():
    c, s = chat()
    s.post = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))      # type: ignore[method-assign]
    assert c.warm(TOOLS) is None


@pytest.mark.parametrize("placement,num_gpu", [("gpu", None), ("auto", None), ("cpu", 0)])
def test_placement_option(placement, num_gpu):
    c, s = chat(placement=placement)
    if placement == "cpu":
        c.on_cpu = True                         # the assistant sets this at start-up
    list(c.stream("привет"))
    assert s.payloads[-1]["options"].get("num_gpu") == num_gpu


def test_switch_to_cpu_rewarms_with_num_gpu_0():
    c, s = chat()
    assert c.set_on_cpu(True, TOOLS) is True
    for _ in range(50):
        if s.payloads:
            break
        time.sleep(0.02)
    assert s.payloads[-1]["options"]["num_gpu"] == 0 and s.payloads[-1]["options"]["num_predict"] == 1
    assert c.set_on_cpu(True) is False                                   # no change -> no reload


def test_embeddings_run_on_cpu_and_stay_loaded():
    c, s = chat()
    c.embed(["кот"])
    p = s.payloads[-1]
    assert p["options"] == {"num_gpu": 0} and p["keep_alive"] == "30m"


@pytest.mark.parametrize("ps,share", [
    ({"models": [{"name": "qwen2.5:7b", "size": 6_000_000_000, "size_vram": 6_000_000_000}]}, 1.0),
    ({"models": [{"name": "qwen2.5:7b", "size": 6_000_000_000, "size_vram": 4_500_000_000}]}, 0.75),
])
def test_placement_info_detects_spill_to_cpu(ps, share):
    c, _ = chat(_ps=ps)
    assert c.placement_info()["gpu_share"] == share


def _assistant(placement="auto", profile="work"):
    from jarvis.assistant import Assistant
    a = Assistant.__new__(Assistant)
    c, s = chat(placement=placement)
    a.llm, a.cfg, a.profile, a.stt = c, {"vram": {"release_in_game": True}}, profile, None
    return a, c


def test_auto_placement_moves_llm_to_ram_in_game_instead_of_unloading():
    a, c = _assistant("auto")
    a.release_gpu()
    assert c.on_cpu and c.keep_alive == "30m"                            # still answers in the game
    a.restore_gpu()
    assert not c.on_cpu


def test_gpu_placement_keeps_old_unload_in_game():
    a, c = _assistant("gpu")
    c.unload_all = lambda: None                                          # type: ignore[method-assign]
    a.release_gpu()
    assert not c.on_cpu and c.keep_alive == 0
    a.restore_gpu()
    assert c.keep_alive == "30m"


def test_settings_switch_placement_live():
    a, c = _assistant("gpu")
    assert a.apply_placement("cpu") and c.on_cpu
    assert a.apply_placement("gpu") is True and not c.on_cpu
    a.profile = "game"
    assert a.apply_placement("auto") and c.on_cpu                         # in a game: RAM right away


# ---------------------------------------------------------------- Ollama server env, Whisper, config
def test_ollama_server_env_saves_vram_but_user_vars_win(monkeypatch):
    from jarvis import ollama_setup as osu
    monkeypatch.delenv("OLLAMA_FLASH_ATTENTION", raising=False)
    monkeypatch.setenv("OLLAMA_KV_CACHE_TYPE", "f16")
    env = osu._serve_env("http://localhost:11434")
    assert env["OLLAMA_FLASH_ATTENTION"] == "1" and env["OLLAMA_NUM_PARALLEL"] == "1"
    assert env["OLLAMA_KV_CACHE_TYPE"] == "f16"                          # the user's own choice is kept


def test_ollama_tray_app_also_gets_the_env(monkeypatch):
    from jarvis import ollama_setup as osu
    seen = []
    state = {"up": False}
    monkeypatch.setattr(osu, "is_running", lambda host, session=None: state["up"])
    monkeypatch.setattr(osu, "find_app", lambda exe: "C:/Ollama/ollama app.exe")

    def popen(argv, env=None):
        seen.append(env)
        state["up"] = True
    assert osu.start({"llm": {}}, popen=popen, sleep=lambda s: None, find=lambda: "C:/Ollama/ollama.exe",
                     procs=lambda: [])
    assert seen and seen[0]["OLLAMA_FLASH_ATTENTION"] == "1"


def test_whisper_auto_compute_is_float16_on_gpu_like_1_0_4():
    from jarvis.stt import pick_device
    assert pick_device("cuda", "auto") == ("cuda", "float16")
    assert pick_device("cpu", "auto") == ("cpu", "int8")
    assert pick_device("cuda", "float32") == ("cuda", "float32")


def test_defaults_and_migration_v5():
    from jarvis import config as C
    assert C.DEFAULTS["llm"]["num_ctx"] == 6144 and C.DEFAULTS["llm"]["placement"] == "gpu"
    assert C.DEFAULTS["assistant"]["ack_after_s"] == 2.5
    d, ch = C.migrate_local({"config_version": 4, "llm": {"num_ctx": 8192, "model": "qwen2.5:7b"}})
    assert ch and d["llm"] == {"model": "qwen2.5:7b"} and d["config_version"] == C.CONFIG_VERSION >= 5
    d, _ = C.migrate_local({"config_version": 4, "llm": {"num_ctx": 12000}})
    assert d["llm"]["num_ctx"] == 12000


# ---------------------------------------------------------------- timing
def test_trace_stages_and_summary():
    clock = types.SimpleNamespace(t=100.0)
    tr = Trace("расскажи анекдот", "voice", clock=lambda: clock.t)
    tr.add("stt", 0.35)
    clock.t = 100.4
    tr.add("route", 0.002)
    tr.path = "llm"
    clock.t = 101.0
    tr.mark("llm_first")
    clock.t = 101.3
    tr.mark("first_audio")
    tr.mark("first_audio")                                               # first time only
    clock.t = 104.0
    tr.llm_stats = {"load_s": 4.1, "prompt_tokens": 40, "prompt_s": 0.1, "tok_s": 28}
    t = Timings()
    t.finish(tr)
    assert tr.get("first_audio") == pytest.approx(1.3) and tr.get("total") == pytest.approx(4.0)
    s = tr.summary()
    assert "ИИ" in s and "распознавание 0.35 с" in s and "загрузка модели 4.1 с" in s and "28 ток/с" in s
    assert "до звука 1.30 с" in s and "маршрут 2 мс" in s


def test_timings_report_for_diagnostics():
    t = Timings()
    for path, fa, load in [("system", 0.4, 0), ("media", 0.6, 0), ("llm", 1.5, 0), ("llm", 6.0, 4.5)]:
        tr = Trace("x", clock=lambda: 0.0)
        tr.path, tr.marks = path, {"stt": 0.3, "first_audio": fa}
        tr.llm_stats = {"tok_s": 25, "load_s": load} if path == "llm" else {}
        t.items.append(tr)
    r = t.report()
    assert "простые команды ~0.5 с" in r and "вопросы к ИИ" in r and "загружалась заново 1 раз" in r


def test_assistant_records_route_and_path():
    from jarvis.assistant import Assistant
    from jarvis.skills.base import ToolResult
    a = Assistant.__new__(Assistant)
    a.emit = lambda *x, **k: None                                       # type: ignore[method-assign]
    a._trace = tr = Trace("громче")
    a.router = types.SimpleNamespace(handle=lambda t: ToolResult(""), last_skill="system")
    a._run_result = lambda r: None                                       # type: ignore[method-assign]
    a.respond("громче")
    assert tr.path == "system" and tr.get("route") is not None


def test_diagnostics_speed_section():
    from jarvis import diagnostics as D
    c, _ = chat(_ps={"models": [{"name": "qwen2.5:7b", "size": 6e9, "size_vram": 4.8e9}]})
    t = Timings()
    tr = Trace("x")
    tr.path, tr.marks = "system", {"first_audio": 0.4}
    t.items.append(tr)
    checks = {x.id: x for x in D._speed({}, types.SimpleNamespace(llm=c, timings=t))}
    assert checks["llm_place"].ok is None and "80%" in checks["llm_place"].detail
    assert "простые команды" in checks["speed"].detail


# ---------------------------------------------------------------- «Секунду» + early first clause
class _Speaker:
    def __init__(self):
        self.said = []

    def say(self, text, **k):
        self.said.append(text)


def _ack_assistant(after):
    from jarvis.assistant import Assistant
    a = Assistant.__new__(Assistant)
    a.cfg = {"assistant": {"ack_after_s": after}}
    a.speaker = _Speaker()
    a._ack_text = ""
    return a


def test_ack_only_when_slow():
    a = _ack_assistant(0.05)
    tr = Trace("найди курс доллара", "voice")
    a._trace = tr
    t = a._start_ack(tr, "voice")
    time.sleep(0.15)
    t.cancel()
    assert a.speaker.said == ["Секунду."]
    # «Секунду» is not counted as the answer
    a.emit = lambda *x, **k: None                                         # type: ignore[method-assign]
    a._on_tts("speech_start", text="Секунду.")
    assert tr.get("first_audio") is None
    a._on_tts("speech_start", text="Доллар стоит 41 гривну.")
    assert tr.get("first_audio") is not None


def test_no_ack_when_answer_came_fast_or_text_mode():
    a = _ack_assistant(0.05)
    tr = Trace("громче", "voice")
    a._trace = tr
    tr.mark("first_audio")
    t = a._start_ack(tr, "voice")
    time.sleep(0.15)
    assert a.speaker.said == []
    assert a._start_ack(Trace("x", "text"), "text") is None              # typed: nothing spoken
    assert _ack_assistant(0)._start_ack(tr, "voice") is None
    if t:
        t.cancel()


def test_first_long_sentence_starts_at_a_comma():
    sp = L.SentenceSplitter()
    out = sp.feed("Чёрная дыра — это область пространства-времени, гравитационное притяжение которой ")
    assert out == ["Чёрная дыра — это область пространства-времени,"]
    assert sp.feed("настолько велико, что покинуть её не может ничто. ") == [
        "гравитационное притяжение которой настолько велико, что покинуть её не может ничто."]
    sp2 = L.SentenceSplitter()
    assert sp2.feed("Привет, мистер Флоки. ") == ["Привет, мистер Флоки."]  # short: whole sentence as before


def test_thread_safety_of_trace():
    tr = Trace("x")
    ths = [threading.Thread(target=tr.mark, args=("first_audio",)) for _ in range(20)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert "first_audio" in tr.marks
