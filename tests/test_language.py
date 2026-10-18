"""Chinese-drift defences with a mocked Ollama + the "LLM claims success without a tool" guard."""
from __future__ import annotations

import json
import threading
import types

import pytest

from jarvis import llm as L
from jarvis.assistant import CLAIM_RE, HONEST_NO_TOOL, Assistant
from jarvis.skills.base import ToolResult


class Resp:
    def __init__(self, data=None, lines=()):
        self.status_code, self._data, self._lines, self.text = 200, data or {}, list(lines), ""

    def json(self):
        return self._data

    def iter_lines(self):
        yield from self._lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOllama:
    def __init__(self, replies=()):
        self.replies, self.payloads = list(replies), []

    def post(self, url, json=None, stream=False, timeout=None):
        self.payloads.append(json)
        if stream:
            return Resp(lines=[b'{"message":{"content":"\\u041f\\u0440\\u0438\\u0432\\u0435\\u0442!"},"done":true}'])
        return Resp({"message": {"content": self.replies.pop(0) if self.replies else "Хорошо."}})


def chat(replies=(), **cfg):
    s = FakeOllama(replies)
    c = L.OllamaChat({"model": "qwen2.5:7b", "system_prompt": "Ты — Джарвис.", **cfg}, session=s)  # type: ignore[arg-type]
    return c, s


# ------------------------------------------------------------------------------- prompt + options
def test_system_prompt_always_ends_with_russian_rule_and_sane_options():
    c, s = chat()
    c.system_prompt = "Custom prompt without any language rule"           # user-edited prompt
    assert "".join(c.stream("привет")) == "Привет!"
    p = s.payloads[0]
    assert p["messages"][0]["role"] == "system" and p["messages"][0]["content"].endswith(L.RU_RULE)
    o = p["options"]
    assert o["temperature"] == 0.5 and o["top_p"] == 0.9 and o["top_k"] == 40 and o["repeat_penalty"] == 1.1
    assert o["num_ctx"] == 6144 and o["num_predict"] == 512 and "num_gpu" not in o


def test_budget_keeps_system_prompt_and_trims_old_history():
    c, s = chat(num_ctx=4096, max_history_messages=100)
    for i in range(40):
        c.history += [{"role": "user", "content": f"вопрос {i} " + "слово " * 120},
                      {"role": "assistant", "content": f"ответ {i} " + "текст " * 120}]
    tools = [{"type": "function", "function": {"name": f"t{i}", "description": "описание инструмента " * 10,
                                                "parameters": {}}} for i in range(30)]
    list(c.stream("последний вопрос", tools=tools))
    msgs = s.payloads[0]["messages"]
    total = sum(L.est_tokens(m["content"]) for m in msgs) + L.est_tokens(tools)
    assert msgs[0]["role"] == "system" and L.RU_RULE in msgs[0]["content"]
    assert msgs[1]["role"] == "user"                       # never starts with an assistant turn
    assert msgs[-1]["content"].endswith("\nпоследний вопрос") and msgs[-1]["content"].startswith("[Справка")
    assert any(m["content"].endswith("\nпоследний вопрос") for m in c.history[-2:])
    assert total + c.num_predict <= c.num_ctx              # Ollama will not truncate on its own
    assert len(msgs) < 81


def test_min_num_ctx_and_migration_of_old_defaults():
    from jarvis.config import CONFIG_VERSION as C_VER
    c, _ = chat(num_ctx=1024)
    assert c.num_ctx == 2048
    from jarvis.config import migrate_local
    data, changed = migrate_local({"config_version": 2, "llm": {"temperature": 0.8, "num_ctx": 4096, "model": "x"}})
    assert changed and data["llm"] == {"model": "x"} and data["config_version"] == C_VER
    data, _ = migrate_local({"config_version": 2, "llm": {"temperature": 0.3, "num_ctx": 16384}})
    assert data["llm"] == {"temperature": 0.3, "num_ctx": 16384}           # user's own choices kept


# ------------------------------------------------------------------------------- complete() guard
def test_complete_retries_on_cjk_then_accepts_russian():
    c, s = chat(["好的，我来帮你查一下。", "Хорошо, сейчас посмотрю."])
    assert c.complete([{"role": "user", "content": "что нового"}]) == "Хорошо, сейчас посмотрю."
    assert len(s.payloads) == 2
    assert s.payloads[0]["messages"][0]["content"] == L.RU_RULE             # rule added when no system msg
    assert s.payloads[1]["messages"][-1]["content"] == L.CORRECTION
    assert s.payloads[1]["options"]["temperature"] == 0.3


def test_complete_strips_cjk_as_last_resort():
    c, s = chat(["Погода хорошая 天气很好。", "Опять 中文 тут."])
    out = c.complete([{"role": "system", "content": "sys"}, {"role": "user", "content": "погода"}])
    assert not L.has_cjk(out) and "Опять" in out and len(s.payloads) == 2
    assert s.payloads[0]["messages"][0]["content"].endswith(L.RU_RULE)


def test_complete_english_drift_retried():
    c, _ = chat(["Sure! Here is a short summary of the article you asked me about today.", "Вот коротко о статье."])
    assert c.complete([{"role": "user", "content": "перескажи"}]) == "Вот коротко о статье."


def test_translator_and_code_modes():
    c, s = chat(["Hello, how are you doing today, my good friend? Nice weather."])
    assert c.complete([{"role": "user", "content": "переведи"}], lang="any").startswith("Hello")
    assert len(s.payloads) == 1 and L.RU_RULE not in json.dumps(s.payloads[0], ensure_ascii=False)
    c, s = chat(["void setup() { pinMode(13, OUTPUT); } // blink the LED on pin 13 forever and ever"])
    assert c.complete([{"role": "user", "content": "код"}], lang="no_cjk").startswith("void setup")
    assert len(s.payloads) == 1


def test_clean_for_speech_never_passes_cjk():
    assert L.clean_for_speech("Готово! 好的。") == "Готово!"
    assert not L.has_cjk(L.clean_for_speech("中文"))
    assert L.has_cjk("한국어") and L.has_cjk("カタカナ") and not L.has_cjk("Привет, мистер Флоки! ok")


def test_callers_use_language_modes():
    import inspect

    from jarvis import cad, coding, planner
    from jarvis.skills import clipboard_skill, translator_skill
    assert 'lang="no_cjk"' in inspect.getsource(coding) and 'lang="no_cjk"' in inspect.getsource(cad)
    assert 'lang="no_cjk"' in inspect.getsource(planner)
    assert '"any"' in inspect.getsource(translator_skill) and '"any"' in inspect.getsource(clipboard_skill)


# ------------------------------------------------------------------------------- Assistant._chat guards
class FakeLLM:
    def __init__(self, pieces, calls=(), regen="Конечно, помогу."):
        self.pieces, self.calls, self.regen = list(pieces), list(calls), regen
        self.history: list = []
        self.model = "qwen2.5:7b"
        self.regenerated = 0

    def stream(self, text, tools=None, tool_calls_out=None):
        self.history.append({"role": "user", "content": text})
        yield from self.pieces
        if tool_calls_out is not None:
            tool_calls_out.extend(self.calls)
        self.history.append({"role": "assistant", "content": "".join(self.pieces).strip()})

    def regenerate_russian(self):
        self.regenerated += 1
        if self.history and self.history[-1]["role"] == "assistant" and L.not_russian(self.history[-1]["content"]):
            self.history.pop()
        self.history.append({"role": "assistant", "content": self.regen})
        return self.regen

    def add_assistant_message(self, t):
        self.history.append({"role": "assistant", "content": t})


class Speaker:
    def __init__(self):
        self.said: list = []

    def say(self, t, voice=None, volume=None):
        self.said.append(t)

    def wait(self, timeout=None):
        pass


def make_assistant(llm, tool_reply=None):
    a = Assistant.__new__(Assistant)
    a.name, a.cfg, a._turns = "Джарвис", {"llm": {"max_history_messages": 16}}, 0
    a.llm, a.speaker, a.stop_event, a.tool_calling = llm, Speaker(), threading.Event(), True
    a.events = []
    a.emit = lambda ev, **kw: a.events.append((ev, kw))           # type: ignore[method-assign]
    a._shown_as_notice = lambda t: False                           # type: ignore[method-assign]
    called = []

    def call_tool(name, args):
        called.append((name, args))
        return tool_reply
    a.router = types.SimpleNamespace(tool_schemas=lambda: [{"function": {"name": "close_window"}}],
                                     call_tool=call_tool, by_name={})
    a._called = called
    return a


def test_chat_cjk_drift_never_spoken_and_regenerated():
    llm = FakeLLM(["Конечно! ", "我可以帮你做这个。 ", "还有更多。"])
    a = make_assistant(llm)
    a._chat("помоги")
    assert llm.regenerated == 1
    assert not any(L.has_cjk(x) for x in a.speaker.said), a.speaker.said
    assert a.speaker.said[-1] == "Конечно, помогу." or a.speaker.said[-1].startswith("помогу")
    assert not any(L.has_cjk(m["content"]) for m in llm.history)


def test_chat_english_first_sentence_is_drift():
    llm = FakeLLM(["Sure, I can definitely help you with that request right now my friend. "])
    a = make_assistant(llm)
    a._chat("помоги")
    assert llm.regenerated == 1 and a.speaker.said == ["Конечно, помогу."]


def test_chat_claim_without_tool_is_replaced_with_honest_answer():
    """The user's case: «закрой ютуб» -> model says «Закрыл ютуб.» but calls nothing."""
    llm = FakeLLM(["Закрыл ютуб, ", "сэр."])
    a = make_assistant(llm)
    a._chat("закрой ютуб")
    assert a.speaker.said == [HONEST_NO_TOOL]
    assert llm.history[-1]["content"] == HONEST_NO_TOOL            # the false claim is not kept in memory


def test_chat_claim_with_tool_uses_verified_tool_reply():
    llm = FakeLLM(["Закрываю ютуб."], calls=[{"name": "close_window", "arguments": {"target": "ютуб"}}])
    a = make_assistant(llm, tool_reply=ToolResult("Не вижу открытого окна «ютуб»."))
    a._run_result = lambda r: a.speaker.say(r.reply)               # type: ignore[method-assign]
    a._chat("закрой ютуб")
    assert a._called == [("close_window", {"target": "ютуб"})]
    assert a.speaker.said == ["Не вижу открытого окна «ютуб»."]     # no premature «Закрываю»


def test_chat_normal_answer_streams_through():
    llm = FakeLLM(["Привет, мистер Флоки! ", "Чем займёмся?"])
    a = make_assistant(llm)
    a._chat("привет")
    assert a.speaker.said == ["Привет, мистер Флоки!", "Чем займёмся?"] and llm.regenerated == 0


@pytest.mark.parametrize("text,claim", [("Закрыл ютуб.", True), ("Готово, открыл.", True), ("Окей, включаю музыку.", True),
                                        ("Сейчас закрою", False), ("Закрыть окно можно крестиком.", False),
                                        ("Привет!", False), ("Хорошо, сделал.", True)])
def test_claim_regex(text, claim):
    assert bool(CLAIM_RE.match(text)) == claim, text
