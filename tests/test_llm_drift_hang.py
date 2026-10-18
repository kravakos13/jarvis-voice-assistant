"""1.0.6 «falls asleep»: non-Russian stream -> «переспрашиваю по-русски» -> nothing for minutes.
The retry had a 600 s timeout and no token cap. 1.0.7: bounded, one retry, Russian fallback, loop continues."""
import json
import threading
import time
import types

import requests

from jarvis import llm as L

GARBAGE = "根据赛斯沃撷不出来 onCreateViewHolder万绿原 Awobleibl maçcuitperfideisid"


class Resp:
    def __init__(self, lines, status=200):
        self.lines, self.status_code, self.text = lines, status, ""

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def iter_lines(self):
        for x in self.lines:
            yield json.dumps(x).encode()

    def json(self):
        return self.lines


class Session:
    def __init__(self, retry="hang", stream_lines=None):
        self.retry, self.calls = retry, []
        self.stream_lines = stream_lines

    def get(self, *a, **k):
        raise requests.ConnectionError("no")

    def post(self, url, json=None, stream=False, timeout=None):
        self.calls.append({"stream": stream, "timeout": timeout, "options": dict(json.get("options") or {})})
        if stream:
            return Resp(self.stream_lines or [{"message": {"content": GARBAGE + ". "}}, {"done": True}])
        if self.retry == "hang":
            raise requests.ReadTimeout(f"Read timed out. (read timeout={timeout})")
        if self.retry == "garbage":
            return Resp({"message": {"content": GARBAGE}})
        return Resp({"message": {"content": "Привет! Чем помочь?"}})


def chat(sess):
    c = L.OllamaChat({"model": "qwen2.5:7b", "timeout_s": 120}, session=sess)
    c.on_cpu = True
    c.history.append({"role": "user", "content": "привет"})
    return c


def test_retry_is_bounded_and_falls_back_in_russian():
    for mode, expect in (("hang", L.FALLBACK_RU), ("garbage", L.FALLBACK_RU), ("ok", "Привет! Чем помочь?")):
        s = Session(mode)
        c = chat(s)
        t0 = time.monotonic()
        assert c.regenerate_russian() == expect
        assert time.monotonic() - t0 < 2
        retry = [x for x in s.calls if not x["stream"]]
        assert len(retry) == 1                                   # exactly one retry, never a nested second one
        assert retry[0]["timeout"] <= 25 and retry[0]["options"]["num_predict"] == L.RETRY_MAX_TOKENS


def test_stream_has_a_total_deadline():
    class Slow(Session):
        def post(self, url, json=None, stream=False, timeout=None):
            self.calls.append({"stream": stream, "timeout": timeout})
            lines = [{"message": {"content": "а"}}] * 50

            class R(Resp):
                def iter_lines(self_inner):
                    for x in lines:
                        time.sleep(0.02)
                        yield __import__("json").dumps(x).encode()
            return R(lines)
    s = Slow()
    c = L.OllamaChat({"model": "m", "turn_timeout_s": 0.2}, session=s)
    try:
        for _ in c.stream("расскажи"):
            pass
        raise AssertionError("no deadline")
    except L.LLMError as e:
        assert "timeout" in str(e)
    assert s.calls[0]["timeout"][1] <= 0.2                       # read timeout never longer than the turn cap


def test_assistant_drift_turn_finishes_and_brain_goes_idle():
    from jarvis.assistant import Assistant
    a = Assistant.__new__(Assistant)
    a.llm = chat(Session("hang"))
    a.llm.history.clear()
    a.name, a.speaker, a.tool_calling = "Джарвис", None, False
    a.stop_event = threading.Event()
    a._turns, a.cfg = 0, {"llm": {}}
    said = []
    a.emit = lambda *x, **k: None
    a.say = lambda text, **k: said.append(text)
    a.router = types.SimpleNamespace(tool_schemas=lambda: None)
    t0 = time.monotonic()
    a._chat("Джарвис, переключись на хром")
    assert time.monotonic() - t0 < 3
    last = a.llm.history[-1]
    assert last == {"role": "assistant", "content": L.FALLBACK_RU}
    assert not any(L.has_cjk(m["content"]) for m in a.llm.history)
