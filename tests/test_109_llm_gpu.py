"""1.0.9 regression: the user's server.log showed `-ngl 0` on every load (placement «cpu» left over from a
1.0.7 crash workaround) and a self-test reload with `-c 512`. num_gpu must never be sent by default, every main
request must use the same num_ctx, and the real placement is logged after load."""
from __future__ import annotations

import types

import pytest

from jarvis import config, ollama_setup, resources
from jarvis import llm as L


class Sess:
    def __init__(self, ps=None):
        self.payloads: list = []
        self.ps = ps or []

    def post(self, url, json=None, timeout=None, stream=False):
        self.payloads.append((url, json))
        body = {"message": {"content": "Да", "role": "assistant"}, "done": True,
                "prompt_eval_count": 10, "eval_count": 1}
        return types.SimpleNamespace(status_code=200, json=lambda: body, text="")

    def get(self, url, timeout=None):
        if url.endswith("/api/ps"):
            return types.SimpleNamespace(status_code=200, json=lambda: {"models": self.ps})
        return types.SimpleNamespace(status_code=200, json=lambda: {"models": []})


@pytest.mark.parametrize("free", [5.3, 3.0, 1.2])           # fits / partly / hardly: never our own num_gpu
def test_no_num_gpu_by_default_in_any_request(monkeypatch, free):
    monkeypatch.setattr(resources, "vram_gb", lambda: (6.0, free))
    monkeypatch.setattr(resources, "free_ram_gb", lambda: 16.0)
    s = Sess()
    c = L.OllamaChat({"model": "qwen2.5:7b", "num_ctx": 4096}, session=s)
    c.warm()
    c.selftest()
    c.complete([{"role": "user", "content": "привет"}], num_ctx=8192)
    main = [p for u, p in s.payloads if p.get("model") == "qwen2.5:7b" and "options" in p]
    assert len(main) == 3
    for p in main:
        assert "num_gpu" not in p["options"]
        assert p["options"]["num_ctx"] == 4096                 # identical: no reload with -c 512 / -c 8192


def test_num_gpu_0_only_by_explicit_cpu_choice():
    s = Sess()
    c = L.OllamaChat({"model": "qwen2.5:7b", "placement": "cpu"}, session=s)
    assert c.options()["num_gpu"] == 0
    assert "num_gpu" not in L.OllamaChat({"model": "qwen2.5:7b", "placement": "auto"}, session=s).options()


def test_migration_resets_stale_cpu_placement_once():
    config.MIGRATION_NOTICES.clear()
    data, changed = config.migrate_local({"config_version": 8, "llm": {"placement": "cpu"}})
    assert changed and data["llm"]["placement"] == "gpu" and data["config_version"] == 9
    assert config.MIGRATION_NOTICES and "видеокарте" in config.MIGRATION_NOTICES[0]
    config.MIGRATION_NOTICES.clear()
    data2, _ = config.migrate_local({"config_version": 9, "llm": {"placement": "cpu"}})   # chosen again later: kept
    assert data2["llm"]["placement"] == "cpu" and not config.MIGRATION_NOTICES


def test_placement_report_lines_and_warning():
    gb = 1e9
    full = [{"name": "qwen2.5:7b", "size": 5.0 * gb, "size_vram": 5.0 * gb}]
    c = L.OllamaChat({"model": "qwen2.5:7b"}, session=Sess(full))
    line, warn = c.placement_report(gpu_present=True)
    assert line.startswith("ИИ qwen2.5:7b: 29/29 слоёв на GPU, 5.0") and not warn
    part = [{"name": "qwen2.5:7b", "size": 5.0 * gb, "size_vram": 4.3 * gb}]
    assert "25/29" in L.OllamaChat({"model": "qwen2.5:7b"}, session=Sess(part)).placement_report(True)[0]
    none = [{"name": "qwen2.5:7b", "size": 5.0 * gb, "size_vram": 0}]
    line, warn = L.OllamaChat({"model": "qwen2.5:7b"}, session=Sess(none)).placement_report(True)
    assert "0/29" in line and warn
    assert not L.OllamaChat({"model": "qwen2.5:7b"}, session=Sess(none)).placement_report(False)[1]   # no NVIDIA
    cpu = L.OllamaChat({"model": "qwen2.5:7b", "placement": "cpu"}, session=Sess(none))
    line, warn = cpu.placement_report(True)
    assert not warn and "Оперативка" in line                                                        # user's choice


def test_assistant_warns_and_notifies_when_gpu_unused():
    from jarvis.assistant import Assistant
    a = Assistant.__new__(Assistant)
    events = []
    a.emit = lambda ev, **kw: events.append((ev, kw))
    a.llm = types.SimpleNamespace(placement_report=lambda: ("ИИ qwen2.5:7b: 0/29 слоёв на GPU", True))
    a._report_placement()
    assert events and "без видеокарты" in events[0][1]["text"]
    events.clear()
    a.llm = types.SimpleNamespace(placement_report=lambda: ("ИИ qwen2.5:7b: 29/29 слоёв на GPU", False))
    a._report_placement()
    assert events == []


def test_presets_keep_model_resident_and_small_headroom():
    assert resources.PRESETS["balanced"]["keep_alive"] == "24h"
    assert resources.PRESETS["balanced"]["vram_headroom_gb"] <= 0.5
    assert resources.PRESETS["light"]["keep_alive"] == "5m"                # Light: unload after idle


def test_spawned_ollama_does_not_inherit_whisper_cuda_dirs(monkeypatch):
    import os
    bad = os.path.join("C:", "py", "Lib", "site-packages", "nvidia", "cublas", "bin")
    monkeypatch.setenv("PATH", bad + os.pathsep + "/usr/bin")
    env = ollama_setup._serve_env("http://localhost:11434")
    assert bad not in env["PATH"] and "/usr/bin" in env["PATH"]
