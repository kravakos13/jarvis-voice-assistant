"""1.0.8: system load (presets, VRAM fit, priority), LLM garbage self-test, cancel, fuzzy commands, conversation."""
from __future__ import annotations

import types

import pytest

from jarvis import config as C
from jarvis import fuzzy, llm as L, noname, resources
from jarvis.router import normalize
from jarvis.skills import chat_skill

CONF = (-0.2, 0.05)


# ------------------------------------------------------------------ presets / memory fit
def test_presets_default_keeps_7b_and_explicit_values_win():
    c = C.apply_load_preset({})
    assert c["llm"]["model"] if "model" in c["llm"] else True
    assert c["llm"]["num_ctx"] == 4096 and c["llm"]["keep_alive"] == "24h" and c["llm"]["num_thread"] == 4
    assert c["stt"]["cpu_threads"] == 4 and c["gui"]["fps"] == 30 and c["llm"]["tools"] == "core"
    assert "model" not in c["llm"]                                         # 7b stays (from config/defaults)
    light = C.apply_load_preset({"load": {"preset": "light"}, "llm": {"model": "qwen2.5:7b"}})
    assert light["llm"]["model"] == "qwen2.5:3b"                           # only by explicit choice
    own = C.apply_load_preset({"llm": {"num_ctx": 8192, "keep_alive": "1h"}, "stt": {"cpu_threads": 6}})
    assert own["llm"]["num_ctx"] == 8192 and own["llm"]["keep_alive"] == "1h" and own["stt"]["cpu_threads"] == 6
    assert C.apply_load_preset({"load": {"preset": "max"}})["llm"]["num_thread"] == 0


def test_migration_v8_drops_old_pinned_defaults_and_unsafe_env():
    d, ch = C.migrate_local({"config_version": 7, "llm": {"keep_alive": "30m", "num_ctx": 6144, "model": "x"},
                             "gui": {"fps": 30}, "audio": {"silence_ms": 800},
                             "ollama": {"server_env": {"OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0",
                                                       "OLLAMA_NUM_PARALLEL": "1"}}})
    assert ch and d["llm"] == {"model": "x"} and d["gui"] == {} and d["audio"] == {}
    assert d["ollama"]["server_env"] == {"OLLAMA_NUM_PARALLEL": "1"} and d["config_version"] == C.CONFIG_VERSION


def test_fit_layers_gtx1660_super():
    # 6 GB card, desktop/Chrome hold ~0.9 GB: 7b q4 (4.7 GB) + 4096 ctx f16 KV + buffers does not fit with 1 GB room
    n = resources.fit_layers(4.7, 28, 4096, free_vram=5.1, headroom=1.0)
    assert n is not None and 15 <= n < 28
    assert resources.fit_layers(4.7, 28, 4096, free_vram=7.5, headroom=1.0) is None      # fits: Ollama decides
    assert resources.fit_layers(4.7, 28, 4096, free_vram=None, headroom=1.0) is None     # no NVIDIA GPU
    assert resources.fit_layers(4.7, 28, 4096, free_vram=0.8, headroom=1.0) == 0
    assert resources.spill_gb(4.7, 28, None) == 0.0 and resources.spill_gb(4.7, 28, 0) == 4.7


def test_vram_query_parses_nvidia_smi_and_caches():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="6144, 5120\n")
    resources._gpu_cache = (0.0, None)
    assert resources.vram_gb(run=run, now=1000.0) == (6.0, 5.0)
    assert resources.vram_gb(run=run, now=1010.0) == (6.0, 5.0) and len(calls) == 1
    resources._gpu_cache = (0.0, None)


def test_thread_env_and_monitor_line(monkeypatch):
    assert resources.thread_env(4)["OMP_NUM_THREADS"] == "4" and resources.thread_env(0) == {}
    monkeypatch.setattr(resources, "vram_gb", lambda: (6.0, 2.5))
    llm = types.SimpleNamespace(placement_info=lambda: {"model": "qwen2.5:7b", "gpu_share": 0.7})
    ln = resources.Monitor(llm).line()
    assert ln.startswith("Ресурсы:") and "видеопамять свободно 2.5 из 6.0" in ln and "70% на видеокарте" in ln


class Sess:
    def __init__(self, answer="Да", smi=None):
        self.answer, self.payloads = answer, []

    def post(self, url, json=None, timeout=None, stream=False):
        self.payloads.append((url, json))
        return types.SimpleNamespace(status_code=200, json=lambda: {"message": {"content": self.answer}})

    def get(self, url, timeout=None):
        return types.SimpleNamespace(status_code=200, json=lambda: {"models": []})


@pytest.mark.parametrize("answer,res", [("Да.", "ok"), ("榜东庚洙和合zano:SqlCommand", "garbage"),
                                        ("(*(id7.},{ ={\"/r.", "garbage"), ("да", "ok")])
def test_selftest_detects_garbage(answer, res):
    c = L.OllamaChat({"model": "qwen2.5:7b"}, session=Sess(answer))
    assert c.selftest() == res


def test_low_memory_blocks_llm_instead_of_silent_spill(monkeypatch):
    c = L.OllamaChat({"model": "qwen2.5:7b", "min_free_ram_gb": 6.0}, session=Sess())
    monkeypatch.setattr(resources, "vram_gb", lambda: (6.0, 4.0))
    monkeypatch.setattr(resources, "free_ram_gb", lambda: 5.0)
    msg = c.ensure_fit(force=True)
    assert "мало свободной памяти" in msg and c.model == "qwen2.5:7b"               # no silent model switch
    with pytest.raises(L.LLMError):
        list(c.stream("привет"))
    monkeypatch.setattr(resources, "free_ram_gb", lambda: 12.0)
    assert c.ensure_fit(force=True) == "" and c.num_gpu is None          # 1.0.9: Ollama places the layers
    assert "num_gpu" not in c.options()


def test_tok_s_is_not_reported_for_tiny_answers():
    st = L._stats({"eval_count": 1, "eval_duration": 1000})
    assert st["tok_s"] == 0.0
    assert L._stats({"eval_count": 30, "eval_duration": 1_000_000_000})["tok_s"] == 30.0


def test_num_thread_in_options():
    c = L.OllamaChat({"model": "qwen2.5:7b", "num_thread": 4}, session=Sess())
    assert c.options()["num_thread"] == 4


def test_cancel_aborts_stream():
    class Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            pass

        def iter_lines(self):
            import json
            for i in range(1000):
                if i == 3:
                    c.cancel()
                yield json.dumps({"message": {"content": f"слово{i} "}}).encode()

    class S(Sess):
        def post(self, url, json=None, timeout=None, stream=False):
            return Resp()
    c = L.OllamaChat({"model": "qwen2.5:7b"}, session=S())
    got = []
    with pytest.raises(L.LLMError, match="cancelled"):
        for p in c.stream("привет"):
            got.append(p)
    assert len(got) == 3


def test_warm_and_check_restarts_ollama_on_garbage(monkeypatch):
    from jarvis import ollama_setup
    from jarvis.assistant import Assistant
    a = Assistant.__new__(Assistant)
    a.cfg = {"ollama": {"selftest": True}, "llm": {}}
    notices = []
    a.on_event = lambda e, **d: notices.append(d.get("text"))
    results = iter(["garbage", "ok"])
    a.llm = types.SimpleNamespace(warm=lambda tools: 1.0, warm_tools=None, selftest=lambda: next(results))
    restarted = []
    monkeypatch.setattr(ollama_setup, "restart_safe", lambda cfg: restarted.append(1) or True)
    a._warm_and_check()
    assert restarted == [1] and any("снова в порядке" in (n or "") for n in notices)


# ------------------------------------------------------------------ fuzzy / user log phrases
@pytest.mark.parametrize("phrase,fixed", [
    ("Джарвис нашми Энтер", "нажми энтер"), ("Джарвис и Грай", "играй"),
    ("Джарвис, первая вкладка открой", "открой первая вкладка"), ("Вес назад.", "назад"),
])
def test_fuzzy_variants_from_user_log(phrase, fixed):
    t = normalize(phrase).replace("джарвис ", "")
    assert fixed in list(fuzzy.variants(t))


def test_unknown_short_command_does_not_go_to_llm():
    assert fuzzy.looks_like_command("нажми фигню") and not fuzzy.looks_like_command("расскажи анекдот")


@pytest.mark.parametrize("phrase,group", [("сверни все окна", "windows"), ("Разверни ютуб", "windows"),
                                          ("Переключись на хром", "windows"), ("Переключить на YouTube", "windows"),
                                          ("Вес назад.", "nav")])
def test_user_log_phrases_work_without_name(phrase, group):
    cmd = noname.decide(phrase, {}, CONF, True)
    assert cmd is not None and noname.classify(cmd) == group


# ------------------------------------------------------------------ conversation mode
class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def chat(enabled=True):
    llm = types.SimpleNamespace(style_hint="")
    conf = {"v": CONF}
    ctx = types.SimpleNamespace(cfg={"assistant": {"conversation": {"enabled": enabled, "idle_s": 22}}}, llm=llm,
                                extra={"stt_conf": lambda: conf["v"]})
    clk = Clock()
    return chat_skill.ChatSkill(ctx, clock=clk), llm, clk, conf


@pytest.mark.parametrize("phrase", ["давай поболтаем", "расскажи что-нибудь", "как дела", "расскажи интересный факт",
                                    "расскажи анекдот", "расскажи историю про космос", "поговори со мной"])
def test_conversation_starts_and_llm_answers(phrase):
    sk, llm, clk, _ = chat()
    assert sk.match(phrase, normalize(phrase)) is None             # the LLM answers the opener
    assert sk.awaiting and "режим беседы" in llm.style_hint.lower()


def test_conversation_followups_stop_and_idle():
    sk, llm, clk, conf = chat()
    sk.match("", "давай поболтаем")
    assert sk.handle_modal("а ты любишь кофе", "а ты любишь кофе") is None       # routed (LLM with context)
    conf["v"] = (-1.2, 0.1)
    assert sk.handle_modal("ммм", "ммм").reply == ""                               # unsure noise: silently ignored
    conf["v"] = CONF
    assert sk.handle_modal("хм", "хм").reply == ""                                 # one-word noise
    assert sk.handle_modal("да", "да") is None                                     # short real answer
    for stop in ("хватит болтать", "пока", "спасибо всё", "тихо"):
        sk.match("", "давай поболтаем")
        r = sk.handle_modal(stop, normalize(stop))
        assert r is not None and "зови" in r.reply and not sk.awaiting and llm.style_hint == ""
    sk.match("", "давай поболтаем")
    clk.t += 10
    sk.after_turn()                                                                # Jarvis finished answering
    clk.t += 21
    assert sk.awaiting and 0 < sk.awaiting_timeout <= 1.5
    clk.t += 2
    assert not sk.awaiting and llm.style_hint == ""                                # ~22 s silence -> wake mode


def test_conversation_toggle_off_and_style_in_user_note():
    sk, llm, _, _ = chat(enabled=False)
    sk.match("", "давай поболтаем")
    assert not sk.awaiting
    c = L.OllamaChat({"model": "qwen2.5:7b"}, session=Sess())
    c.style_hint = chat_skill.STYLE
    assert "режим беседы" in c._turn_context().lower() and "беседы" not in c._system_message()["content"].lower()


def test_wakeflow_conversation_accepts_followups_without_name():
    from jarvis.wakeflow import WakeFlow
    sk, llm, clk, _ = chat()
    ran = []
    phrases = iter(["Джарвис, давай поболтаем", "А ты любишь кофе?", "Хватит болтать", "А теперь?"])

    def detect(text):
        d = text.lower().startswith("джарвис")
        return types.SimpleNamespace(detected=d, command=text.split(",", 1)[1] if d else "")

    def execute(c):
        ran.append(c.strip())
        t = normalize(c)
        if sk.awaiting:
            sk.handle_modal(c, t)
        else:
            sk.match(c, t)
    flow = WakeFlow(detect=detect, listen=lambda **k: object(), transcribe=lambda a: next(phrases),
                    say=lambda s: None, execute=execute, awaiting=lambda: sk.awaiting,
                    awaiting_timeout=lambda: sk.awaiting_timeout)
    assert [flow.step() for _ in range(4)] == ["direct", "reply", "reply", "ignored"]
    assert ran == ["давай поболтаем", "А ты любишь кофе?", "Хватит болтать"]
