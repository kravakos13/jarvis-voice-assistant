"""1.0.6: GPU Whisper without CUDA libraries must fall back to the CPU, never to text mode (mocked)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

from jarvis import cuda_libs, stt

CUBLAS_ERR = RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")


class Seg:
    def __init__(self, text):
        self.text, self.no_speech_prob = text, 0.0


class FakeWhisper:
    """Like faster-whisper: transcribe() is LAZY — the GPU error only appears when segments are iterated."""
    loads: list = []
    fail_load = False          # cuda load itself raises
    fail_run = True            # cuda inference raises (missing cuBLAS)

    def __init__(self, model, device="cpu", compute_type="int8", **kw):
        FakeWhisper.loads.append((device, compute_type, kw.get("local_files_only", False)))
        if device == "cuda" and FakeWhisper.fail_load:
            raise CUBLAS_ERR
        self.device = device

    def transcribe(self, audio, **kw):
        def gen():
            if self.device == "cuda" and FakeWhisper.fail_run:
                raise CUBLAS_ERR
            yield Seg("привет" if self.device == "cpu" else "привет с видеокарты")
        return gen(), None


@pytest.fixture
def fake_whisper(monkeypatch):
    FakeWhisper.loads = []
    FakeWhisper.fail_load, FakeWhisper.fail_run = False, True
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeWhisper))
    monkeypatch.setitem(sys.modules, "ctranslate2", types.SimpleNamespace(get_cuda_device_count=lambda: 1))
    return FakeWhisper


AUDIO = np.zeros(16000, np.float32)


def test_auto_probe_really_runs_the_model_and_falls_back_to_cpu(fake_whisper):
    notes = []
    s = stt.SpeechToText({"device": "auto"}, on_notice=notes.append)
    assert (s.device, s.compute_type) == ("cpu", "int8")
    assert [d for d, *_ in fake_whisper.loads] == ["cuda", "cpu"]      # tried the GPU, then the CPU
    assert "cublas64_12.dll" in s.gpu_error
    assert notes == []                       # auto: log only, no nagging
    assert s.transcribe(AUDIO) == "привет"


def test_old_lazy_check_would_have_missed_it(fake_whisper):
    m = FakeWhisper("small", device="cuda")
    m.transcribe(AUDIO)                      # what 1.0.5 did: no error without iterating
    with pytest.raises(RuntimeError):
        stt.probe(m)


def test_explicit_gpu_falls_back_with_russian_notice(fake_whisper):
    notes = []
    fake_whisper.fail_load = True
    s = stt.SpeechToText({"device": "cuda"}, on_notice=notes.append)
    assert s.device == "cpu"
    assert notes and "распознаю на процессоре" in notes[0] and "нет библиотек CUDA" in notes[0]


def test_gpu_that_works_stays_on_gpu(fake_whisper):
    fake_whisper.fail_run = False
    s = stt.SpeechToText({"device": "auto"})
    assert (s.device, s.compute_type) == ("cuda", "float16") and s.gpu_error is None
    assert s.transcribe(AUDIO) == "привет с видеокарты"


def test_runtime_failure_on_first_phrase_retries_on_cpu(fake_whisper):
    fake_whisper.fail_run = False
    s = stt.SpeechToText({"device": "auto"})
    assert s.device == "cuda"
    fake_whisper.fail_run = True             # e.g. cuDNN sub-library missing, only hit later
    assert s.transcribe(AUDIO) == "привет"   # same phrase, recognised on the CPU
    assert s.device == "cpu" and s.gpu_error
    s.relocate("cpu")
    s.relocate("restore")                    # game mode end must not go back to the broken GPU
    assert s.device == "cpu"


def test_cpu_errors_are_not_hidden(fake_whisper):
    s = stt.SpeechToText({"device": "cpu"})
    s.model = types.SimpleNamespace(transcribe=lambda *a, **k: (_ for _ in ()).throw(ValueError("bad audio")))
    with pytest.raises(ValueError):
        s.transcribe(AUDIO)


def test_whisper_loader_reraises_gpu_errors_without_redownload(fake_whisper):
    fake_whisper.fail_load = True
    with pytest.raises(RuntimeError):
        stt._whisper(FakeWhisper, "small", device="cuda", compute_type="int8_float16")
    assert [x[2] for x in fake_whisper.loads] == [True]     # no second (download) attempt


def test_assistant_voice_transcribe_never_raises():
    from jarvis.assistant import Assistant
    a = Assistant.__new__(Assistant)
    a.stt = types.SimpleNamespace(transcribe=lambda *a, **k: (_ for _ in ()).throw(CUBLAS_ERR))
    assert a._transcribe(AUDIO) == ""


# ---------------------------------------------------------------- DLLs / install
def _fake_nvidia(tmp_path: Path, names) -> Path:
    for lib, files in names.items():
        b = tmp_path / "nvidia" / lib / "bin"
        b.mkdir(parents=True)
        for f in files:
            (b / f).write_bytes(b"")
    return tmp_path


def test_missing_dlls_and_bin_dirs(tmp_path):
    site = _fake_nvidia(tmp_path, {"cublas": ["cublas64_12.dll", "cublasLt64_12.dll"],
                                   "cudnn": ["cudnn64_9.dll", "cudnn_ops64_9.dll", "cudnn_cnn64_9.dll", "cudnn_graph64_9.dll"]})
    dirs = cuda_libs.nvidia_bin_dirs([str(site)])
    assert {d.parent.name for d in dirs} == {"cublas", "cudnn"}
    assert cuda_libs.missing_dlls(dirs) == []
    assert cuda_libs.missing_dlls([dirs[0]]) == ["cudnn_ops64_9.dll", "cudnn_cnn64_9.dll", "cudnn_graph64_9.dll"] \
        or cuda_libs.missing_dlls([dirs[0]]) == ["cublas64_12.dll", "cublasLt64_12.dll"]
    assert "cublas64_12.dll" in cuda_libs.missing_dlls([tmp_path / "nothing"])


def test_add_dll_dirs_on_windows(tmp_path, monkeypatch):
    site = _fake_nvidia(tmp_path, {"cublas": ["cublas64_12.dll"]})
    added = []
    monkeypatch.setattr(cuda_libs.sys, "platform", "win32")
    monkeypatch.setattr(cuda_libs.os, "add_dll_directory", lambda p: added.append(p) or object(), raising=False)
    monkeypatch.setattr(cuda_libs, "_added", {})
    monkeypatch.setenv("PATH", "")
    out = cuda_libs.add_dll_dirs([str(site)], env={})
    assert out == added == [str(site / "nvidia" / "cublas" / "bin")]
    assert cuda_libs.add_dll_dirs([str(site)], env={}) == []          # idempotent


def test_add_dll_dirs_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(cuda_libs.sys, "platform", "linux")
    assert cuda_libs.add_dll_dirs() == []


def test_nvidia_gpu_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(cuda_libs.shutil, "which", lambda n: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    assert cuda_libs.nvidia_gpu_present(system_root=str(tmp_path)) is False
    (tmp_path / "System32").mkdir()
    (tmp_path / "System32" / "nvcuda.dll").write_bytes(b"")
    assert cuda_libs.nvidia_gpu_present(system_root=str(tmp_path)) is True


def test_migration_v6_back_to_cpu_default():
    from jarvis import config as C
    assert C.DEFAULTS["stt"]["device"] == "cpu"
    for dev in ("cuda", "auto"):
        d, ch = C.migrate_local({"config_version": 5, "stt": {"device": dev, "compute_type": "int8_float16", "model": "small"}})
        assert ch and d["stt"] == {"model": "small"} and d["config_version"] == C.CONFIG_VERSION == 6
    d, _ = C.migrate_local({"config_version": 5, "stt": {"device": "cpu", "compute_type": "int8"}})
    assert d["stt"] == {"device": "cpu", "compute_type": "int8"}


def test_cpu_never_gets_a_gpu_only_compute_type(fake_whisper):
    from jarvis.stt import pick_device
    for ct in ("float16", "int8_float16"):
        assert pick_device("cpu", ct) == ("cpu", "int8")
    s = stt.SpeechToText({"device": "cpu", "compute_type": "int8_float16"})
    assert fake_whisper.loads == [("cpu", "int8", True)]
    s._load("cpu", "float16")
    assert fake_whisper.loads[-1][:2] == ("cpu", "int8")


@pytest.mark.parametrize("dev", ["cpu", "auto", "cuda"])
def test_cuda_error_on_every_construction_path_ends_on_cpu(fake_whisper, dev):
    """Whatever the setting: a cuBLAS error at load, at the probe or at the first phrase -> CPU, voice keeps working."""
    for fail_load in (True, False):
        fake_whisper.fail_load, fake_whisper.fail_run = fail_load, True
        s = stt.SpeechToText({"device": dev})
        assert s.transcribe(AUDIO) == "привет" and s.device == "cpu"
        s.relocate("cpu")
        s.relocate("restore")
        assert s.transcribe(AUDIO) == "привет"
        s.relocate(None)                              # unloaded in game mode -> lazy CPU reload
        assert s.transcribe(AUDIO) == "привет" and s.device == "cpu"


def test_cublas_error_even_when_device_says_cpu_rebuilds_on_cpu(fake_whisper):
    s = stt.SpeechToText({"device": "cpu"})
    calls = []

    def broken(*a, **k):
        calls.append(1)
        raise CUBLAS_ERR
    s.model = types.SimpleNamespace(transcribe=broken)    # e.g. a stale GPU model behind a "cpu" label
    assert s.transcribe(AUDIO) == "привет" and calls == [1] and s.device == "cpu"


def test_assistant_init_retries_whisper_on_cpu(monkeypatch):
    """Assistant.__init__ must not raise because of the STT device (that used to mean text mode)."""
    from jarvis import assistant as A
    made = []

    class STT:
        def __init__(self, cfg, on_notice=None):
            made.append(dict(cfg))
            if cfg.get("device") != "cpu":
                raise CUBLAS_ERR
            self.device = "cpu"
    monkeypatch.setattr(stt, "SpeechToText", STT)
    a = A.Assistant.__new__(A.Assistant)
    a.emit = lambda *x, **k: None
    assert a._load_stt({"device": "cuda", "compute_type": "float16"}).device == "cpu"
    assert [m["device"] for m in made] == ["cuda", "cpu"]



def test_diagnostics_explains_cpu_fallback(monkeypatch):
    from jarvis import diagnostics
    monkeypatch.setattr(cuda_libs, "missing_dlls", lambda *a: ["cublas64_12.dll", "cublasLt64_12.dll"])
    monkeypatch.setattr(cuda_libs, "nvidia_gpu_present", lambda: True)
    a = types.SimpleNamespace(stt=types.SimpleNamespace(model=object(), device="cpu", gpu_error="cublas64_12.dll"))
    c = diagnostics._whisper({"stt": {"model": "small"}}, a, True)
    assert c.ok is None and "процессоре" in c.detail and not c.fixes      # no automatic 1 GB download
    a.stt.gpu_error = None
    assert diagnostics._whisper({"stt": {"model": "small"}}, a, True).ok is True


def test_users_manual_cpu_override_survives_upgrade(tmp_path, monkeypatch):
    """The fix the user applied by hand in 1.0.5 must stay exactly as written after the 1.0.6 migration."""
    from jarvis import config as C
    local = tmp_path / "settings.local.yaml"
    local.write_text('config_version: 5\ngui: {face_image: custom}\nstt:\n  device: "cpu"\n  compute_type: "int8"\n', encoding="utf-8")
    monkeypatch.setattr(C, "LOCAL_SETTINGS", local)
    cfg = C.load_config()
    assert (cfg["stt"]["device"], cfg["stt"]["compute_type"]) == ("cpu", "int8")
    assert stt.pick_device(cfg["stt"]["device"], cfg["stt"]["compute_type"]) == ("cpu", "int8")
    import yaml
    saved = yaml.safe_load(local.read_text(encoding="utf-8"))
    assert saved["stt"] == {"device": "cpu", "compute_type": "int8"} and saved["gui"] == {"face_image": "custom"}
