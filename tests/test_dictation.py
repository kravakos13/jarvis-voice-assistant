"""1.0.7: «напиши <текст>», «пиши» dictation mode, sub-commands, 30 s silence exit, never type into Jarvis."""
import types

from jarvis import keyinput
from jarvis.skills import dictate_skill as D
from jarvis.wakeflow import WakeFlow
from jarvis.windows import Window, WindowManager


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def make(clock=None):
    out, keys = [], []
    ctx = types.SimpleNamespace(cfg={}, extra={}, notify=lambda *a: None)
    sk = D.DictateSkill(ctx, clock=clock or Clock(), typer=lambda s: out.append(s) or True,
                        presser=lambda c: keys.append(c) or True)
    return sk, out, keys


def norm(s):
    from jarvis.router import normalize
    return normalize(s)


def test_napishi_types_once_keeping_whisper_punctuation():
    sk, out, _ = make()
    r = sk.match("Напиши привет, как дела?", norm("Напиши привет, как дела?"))
    assert r is not None and out == ["привет, как дела?"] and not sk.active
    sk.match("Джарвис, напиши: Встреча в 15:00.", norm("Джарвис, напиши: Встреча в 15:00."))
    assert out[-1] == "Встреча в 15:00."


def test_napishi_leaves_other_skills_alone():
    sk, out, _ = make()
    for ph in ("напиши Маше в телеграм привет", "напиши письмо маме", "напиши код сортировки",
               "напиши скетч для ардуино", "напиши стих про осень", "напиши"):
        assert sk.match(ph, norm(ph)) is None, ph
    assert out == []


def test_dictation_mode_subcommands_and_stop():
    c = Clock()
    sk, out, keys = make(c)
    assert "Пишу" in sk.match("пиши", "пиши").reply and sk.awaiting
    sk.handle_modal("Привет, мама.", norm("Привет, мама."))
    sk.handle_modal("как дела", "как дела")
    sk.handle_modal("Новая строка.", norm("Новая строка."))
    sk.handle_modal("удали слово", "удали слово")
    sk.handle_modal("отправь", "отправь")
    assert out == ["Привет, мама.", " Как дела"] and keys == ["enter", "ctrl+backspace", "enter"]
    assert sk.handle_modal("стоп", "стоп").reply == "Диктовка окончена." and not sk.awaiting
    sk.match("пиши", "пиши")
    assert sk.handle_modal("Хватит.", norm("Хватит.")).reply == "Диктовка окончена."
    sk.match("пиши", "пиши")
    sk.handle_modal("и всё стоп диктовка", "и всё стоп диктовка")
    assert out[-1] == "И всё" and not sk.active


def test_dictation_auto_exits_after_30s_silence_and_bounds_listen():
    c = Clock()
    sk, _, _ = make(c)
    sk.match("пиши", "пиши")
    c.t += 10
    assert sk.awaiting and 19 <= sk.awaiting_timeout <= 21
    c.t += 21
    assert not sk.awaiting and sk.awaiting_timeout is None


def test_wakeflow_passes_the_dictation_timeout_and_strict_mode_stays():
    waits = []
    state = {"aw": True}
    f = WakeFlow(detect=lambda s: types.SimpleNamespace(detected=False, command=""),
                 listen=lambda timeout=None, **k: waits.append(timeout),
                 transcribe=lambda a: "", say=lambda s: None, execute=lambda s: None,
                 awaiting=lambda: state["aw"], awaiting_timeout=lambda: 12.5)
    assert f.step() == "idle" and waits == [12.5]
    state["aw"] = False
    f.step()
    assert waits[-1] is None                                 # strict wake-word mode: no open window


class FakeBackend:
    def __init__(self, fg):
        self.fg, self.activated = fg, []

    def windows(self):
        return [Window(1, "Джарвис", 999, "pythonw.exe"), Window(2, "Блокнот", 50, "notepad.exe")]

    def foreground(self):
        return self.fg

    def activate(self, h):
        self.activated.append(h)
        self.fg = h
        return True


def test_never_types_into_jarvis_restores_previous_window(monkeypatch):
    b = FakeBackend(fg=2)
    wm = WindowManager(b, own_pids={999}, sleep=lambda s: None)
    wm.track()                                               # user was in Notepad
    b.fg = 1                                                 # then clicked Jarvis' window
    monkeypatch.setattr(keyinput.time, "sleep", lambda s: None)
    assert keyinput.focus_target(wm) == 2 and b.activated == [2]
    wm2 = WindowManager(FakeBackend(fg=1), own_pids={999}, sleep=lambda s: None)
    assert keyinput.focus_target(wm2) is None                # only Jarvis known -> refuse


def test_unicode_events_cyrillic_and_newline():
    ev = keyinput.unicode_events("Я\n😀")
    assert ev[0] == (0, ord("Я"), keyinput.KEYEVENTF_UNICODE)
    assert ev[2] == (0, 0x1C, keyinput.KEYEVENTF_SCANCODE)          # Enter as a scan code
    assert len(ev) == 2 + 2 + 4                              # emoji = surrogate pair
    assert keyinput.combo_events("ctrl+backspace")[0][1] == 0x1D and keyinput.combo_events("ctrl+nope") is None
