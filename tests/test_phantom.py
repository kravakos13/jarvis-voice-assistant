"""1.0.7: no phantom «привет» from silence/noise/own voice."""
import types

import pytest

from jarvis import config as C
from jarvis import stt as S


def seg(text, ns=0.05, lp=-0.2):
    return types.SimpleNamespace(text=text, no_speech_prob=ns, avg_logprob=lp)


@pytest.mark.parametrize("segs", [
    [seg(" Привет.", ns=0.4, lp=-0.7)],                    # lone «привет», unsure
    [seg("Спасибо.", ns=0.3, lp=-0.3)],                    # lone «спасибо» with high no-speech
    [seg("Продолжение следует...")],
    [seg("Субтитры сделал DimaTorzok")],
    [seg("Редактор субтитров А.Семкин Корректор А.Егорова")],
    [seg("ДИМАТОР СУБТИТРОВ")],
    [seg("Подписывайтесь на канал!")],
    [seg("Включи музыку", ns=0.9, lp=-0.2)],               # whisper says: silence
    [seg("Включи музыку", ns=0.65, lp=-1.1)],              # silence-ish AND guessing
    [seg("бла бла", lp=-1.6)],                             # pure guess
    [seg("Голосовой помощник Джарвис.")],                 # echo of initial_prompt
])
def test_phantoms_are_dropped(segs):
    assert S.accept(segs, "Голосовой помощник Джарвис.") == ""


@pytest.mark.parametrize("segs,text", [
    ([seg("Привет.", ns=0.02, lp=-0.15)], "Привет."),      # clearly said «привет» still works
    ([seg("Спасибо!", ns=0.05, lp=-0.3)], "Спасибо!"),
    ([seg("Джарвис, включи субтитры.")], "Джарвис, включи субтитры."),
    ([seg("Джарвис,", lp=-0.5), seg(" стоп", ns=0.5, lp=-0.6)], "Джарвис, стоп"),
    ([seg("шум", ns=0.95), seg("Открой ютуб")], "Открой ютуб"),
])
def test_real_speech_kept(segs, text):
    assert S.accept(segs, "Голосовой помощник Джарвис.") == text


class FakeModel:
    def __init__(self, fail_vad=False):
        self.calls, self.fail_vad = [], fail_vad

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        if self.fail_vad and kw.get("vad_filter"):
            raise RuntimeError("onnxruntime is not installed (silero vad)")
        return iter([seg("Открой ютуб")]), None


def stt_with(model, **cfg):
    s = S.SpeechToText.__new__(S.SpeechToText)
    s.model, s.language, s.beam_size = model, "ru", 1
    s.initial_prompt, s.vad_filter = "Голосовой помощник Джарвис.", cfg.get("vad_filter", True)
    return s


def test_whisper_params_vad_and_thresholds():
    import numpy as np
    m = FakeModel()
    assert stt_with(m)._transcribe((np.sin(np.arange(16000) / 5) * 0.1).astype(np.float32)) == "Открой ютуб"
    kw = m.calls[0]
    assert kw["vad_filter"] is True and kw["vad_parameters"]["min_speech_duration_ms"] >= 150
    assert kw["condition_on_previous_text"] is False and kw["no_speech_threshold"] == 0.6
    assert kw["log_prob_threshold"] == -1.0


def test_missing_silero_falls_back_without_vad():
    import numpy as np
    m = FakeModel(fail_vad=True)
    s = stt_with(m)
    assert s._transcribe((np.sin(np.arange(16000) / 5) * 0.1).astype(np.float32)) == "Открой ютуб" and s.vad_filter is False


def test_default_prompt_has_no_greeting_and_old_one_is_migrated():
    cfg = C.load_config(use_local=False)
    assert "привет" not in cfg["stt"]["initial_prompt"].lower() and cfg["stt"]["vad_filter"] is True
    assert cfg["stt"]["hotwords"] == cfg["assistant"]["name"]          # name as hotword, never «{name}, привет.»
    for old in ("Джарвис, привет.", C.OLD_PROMPT, "Пятница, привет. Пятница, открой"):
        d, _ = C.migrate_local({"config_version": 6, "stt": {"initial_prompt": old}})
        assert "initial_prompt" not in d["stt"], old
    d, ch = C.migrate_local({"config_version": 6, "stt": {"initial_prompt": C.OLD_PROMPT, "device": "cpu"}})
    assert ch and d["stt"] == {"device": "cpu"} and d["config_version"] == C.CONFIG_VERSION
    d, _ = C.migrate_local({"config_version": 6, "stt": {"initial_prompt": "Моя подсказка"}})
    assert d["stt"]["initial_prompt"] == "Моя подсказка"          # user's own prompt untouched


def test_own_voice_is_not_a_command():
    from jarvis.assistant import Assistant
    a = Assistant.__new__(Assistant)
    a._said = []
    a.stt = types.SimpleNamespace(transcribe=lambda audio, language=None: "включаю второе видео")
    a._on_tts("speech_start", text="Включаю второе видео: Котики.")
    a.emit = lambda *x, **k: None
    assert a._transcribe(None) == ""
    a.stt = types.SimpleNamespace(transcribe=lambda audio, language=None: "открой блокнот")
    assert a._transcribe(None) == "открой блокнот"


def test_strict_wake_mode_drops_text_without_name_silently():
    from jarvis.wakeflow import WakeFlow
    ran = []
    f = WakeFlow(detect=lambda s: types.SimpleNamespace(detected=False, command=""),
                 listen=lambda **k: object(), transcribe=lambda a: "привет", say=lambda s: None,
                 execute=ran.append)
    assert f.step() == "ignored" and ran == []                     # never shown/executed


def test_hotword_echo_dropped_only_when_unsure():
    assert S.accept([seg("Джарвис.", ns=0.4, lp=-0.8)], "", "Джарвис") == ""
    assert S.accept([seg("Джарвис.", ns=0.02, lp=-0.2)], "", "Джарвис") == "Джарвис."
    assert S.accept([seg("Джарвис, привет.", ns=0.02, lp=-0.2)], "", "Джарвис") == "Джарвис, привет."


def test_log_1018_phantom_is_dropped_with_the_old_prompt():
    """jarvis.log 18:33:12 — after a run of empty STT on noise: «Джарвис, привет.» echoed from initial_prompt."""
    assert S.accept([seg("Джарвис, привет.", ns=0.3, lp=-0.5)], "Джарвис, привет. ") == ""


def test_energy_gate_skips_whisper_on_noise():
    import numpy as np
    m = FakeModel()
    s = stt_with(m)
    s.min_level = S.MIN_LEVEL
    rng = np.random.default_rng(0)
    assert s._transcribe((rng.standard_normal(32000) * 0.001).astype(np.float32)) == "" and m.calls == []
    speech = np.concatenate([np.zeros(8000), np.sin(np.arange(16000) / 5) * 0.1]).astype(np.float32)
    assert s._transcribe(speech) == "Открой ютуб" and len(m.calls) == 1
