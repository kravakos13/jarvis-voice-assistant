"""1.0.8: commands without the wake word, voice key emulation, «назад/вперёд» + seek, «вернись на/в X»."""
from __future__ import annotations

import types

import pytest

from jarvis import keyinput, keyspeech, noname
from jarvis import windows as wn
from jarvis.router import normalize
from jarvis.skills import keys_skill, nav_skill
from jarvis.skills.windows_skill import WindowsSkill
from jarvis.wakeflow import WakeFlow
from tests.test_windows import JARVIS_PID, FakeBackend, W

CONF = (-0.2, 0.05)


# ------------------------------------------------------------------ key parser
@pytest.mark.parametrize("phrase,combo,times", [
    ("нажми энтер", "enter", 1), ("Нажми Enter.", "enter", 1), ("нажал интер", "enter", 1), ("ентер нажми", None, 1),
    ("нажми ентер", "enter", 1), ("нажми один", "1", 1), ("нажми пять", "5", 1), ("нажми ноль", "0", 1),
    ("нажми а", "a", 1), ("нажми эй", "a", 1), ("нажми би", "b", 1), ("нажми пробел", "space", 1),
    ("нажми эскейп", "esc", 1), ("нажми эскейб", "esc", 1), ("нажми escape", "esc", 1), ("нажми отмена", "esc", 1),
    ("нажми таб", "tab", 1), ("нажми бэкспейс", "backspace", 1), ("нажми стёрка", "backspace", 1),
    ("нажми удали", "backspace", 1), ("нажми делит", "delete", 1), ("нажми вверх", "up", 1),
    ("нажми стрелку влево", "left", 1), ("нажми вправо", "right", 1), ("нажми хоум", "home", 1),
    ("нажми энд", "end", 1), ("нажми пейдж ап", "pageup", 1), ("нажми пейдж даун", "pagedown", 1),
    ("нажми эф пять", "f5", 1), ("нажми F12", "f12", 1), ("нажми эф двенадцать", "f12", 1),
    ("нажми контрол цэ", "ctrl+c", 1), ("нажми ctrl c", "ctrl+c", 1), ("нажми ctrl+c", "ctrl+c", 1),
    ("нажми контрл вэ", "ctrl+v", 1), ("альт таб", "alt+tab", 1), ("шифт таб", "shift+tab", 1),
    ("нажми контрол шифт эскейп", "ctrl+shift+esc", 1), ("вин дэ", "win+d", 1), ("нажми вниз три раза", "down", 3),
    ("нажми вниз 5 раз", "down", 5), ("тыцни энтер дважды", "enter", 2), ("жмакни пробел", "space", 1),
    ("клацни таб", "tab", 1), ("нажмём энтер", "enter", 1), ("нажать энтер", "enter", 1), ("жми энтер", "enter", 1),
    ("энтер", None, 1), ("нажми", None, 1), ("нажми банан", None, 1), ("нажми эф тринадцать", None, 1),
    ("нажми а бэ", None, 1),
])
def test_key_parser(phrase, combo, times):
    kp = keyspeech.parse(normalize(phrase), need_verb=False)
    if combo is None:
        assert kp is None
    else:
        assert kp is not None and kp.combo == combo and kp.times == times
        assert keyinput.parse_combo(combo)               # every parsed combo is sendable


def test_russian_letter_is_typed_and_forbidden_combo_refused():
    assert keyspeech.parse("нажми я").text == "я"
    typed, pressed = [], []
    ctx = types.SimpleNamespace(extra={})
    sk = keys_skill.KeysSkill(ctx, press=lambda c, n=1: pressed.append((c, n)) or True,
                              type_text=lambda s: typed.append(s) or True)
    assert "не нажимаю" in sk.match("", "нажми вин эль").reply
    assert pressed == []


# ------------------------------------------------------------------ nav / seek
@pytest.mark.parametrize("phrase,exp", [
    ("назад", ("back", 1)), ("вперёд", ("forward", 1)), ("назад три раза", ("back", 3)),
    ("назад 2 раза", ("back", 2)), ("дважды назад", ("back", 2)), ("ну назад", ("back", 1)),
])
def test_parse_nav(phrase, exp):
    assert nav_skill.parse_nav(normalize(phrase)) == exp
    assert nav_skill.parse_seek(normalize(phrase)) is None


@pytest.mark.parametrize("phrase,exp", [
    ("перемотай назад", ("back", 10)), ("вперёд на 30 секунд", ("forward", 30)), ("назад на минуту", ("back", 60)),
    ("перемотай вперёд на две минуты", ("forward", 120)), ("назад на десять секунд", ("back", 10)),
])
def test_parse_seek(phrase, exp):
    assert nav_skill.parse_seek(normalize(phrase)) == exp


def _nav(win, monkeypatch):
    pressed = []
    b = FakeBackend([W(1, "Джарвис", JARVIS_PID, "pythonw.exe", "Qt"), win], fg=win.hwnd)
    wm = wn.WindowManager(b, own_pids={JARVIS_PID})
    wm.track()
    monkeypatch.setattr(keyinput, "focus_target", lambda wm, settle_s=0.15: win.hwnd)
    sk = nav_skill.NavSkill(types.SimpleNamespace(extra={"wm": wm}), press=lambda c, n=1: pressed.append((c, n)) or True)
    return sk, pressed


@pytest.mark.parametrize("win,combo", [
    (W(5, "YouTube - Google Chrome", 7, "chrome.exe"), "alt+left"),
    (W(5, "YouTube Music", 7, "chrome.exe"), "alt+left"),                       # PWA
    (W(5, "Discord", 8, "discord.exe", "Chrome_WidgetWin_1"), "alt+left"),      # Electron
    (W(5, "Загрузки", 9, "explorer.exe", "CabinetWClass"), "alt+left"),
    (W(5, "Медиаплеер", 9, "player.exe", "WinUI"), "browser_back"),
])
def test_back_per_app(win, combo, monkeypatch):
    sk, pressed = _nav(win, monkeypatch)
    assert sk.match("", "назад три раза").reply == ""
    assert pressed == [(combo, 3)]


def test_seek_youtube_and_music(monkeypatch):
    sk, pressed = _nav(W(5, "Видео - YouTube - Google Chrome", 7, "chrome.exe"), monkeypatch)
    sk.match("", "вперед на 30 секунд")
    assert pressed == [("right", 6)]                     # 5 s per arrow press
    sk, pressed = _nav(W(5, "YouTube Music", 7, "chrome.exe"), monkeypatch)
    sk.match("", "перемотай назад")
    assert pressed == [("shift+left", 1)]                # 10 s per Shift+Left
    sk, pressed = _nav(W(5, "Блокнот", 9, "notepad.exe", "Notepad"), monkeypatch)
    assert "только в YouTube" in sk.match("", "перемотай назад").reply and pressed == []


def test_previous_track_stays_media():
    assert nav_skill.parse_nav("предыдущий трек") is None and nav_skill.parse_seek("предыдущее") is None


# ------------------------------------------------------------------ вернись на/в X
def _ws(wins, fg, tabs=None, media=None):
    b = FakeBackend(wins, fg=fg)
    wm = wn.WindowManager(b, own_pids={JARVIS_PID}, sleep=lambda s: None)
    selected = []
    ctx = types.SimpleNamespace(extra={"media": media} if media else {}, cfg={})
    sk = WindowsSkill(ctx, manager=wm, start=False, tabs=lambda h: (tabs or {}).get(h, []),
                      select_tab=lambda el: selected.append(el) or True)
    return sk, b, wm, selected


def test_vernis_na_window_and_bare_previous():
    chrome = W(5, "Почта - Google Chrome", 7, "chrome.exe")
    yt = W(6, "YouTube", 7, "chrome.exe")
    note = W(8, "Без имени — Блокнот", 9, "notepad.exe", "Notepad")
    sk, b, wm, _ = _ws([W(1, "Джарвис", JARVIS_PID, "pythonw.exe", "Qt"), chrome, yt, note], fg=6)
    wm.track()
    for phrase, hwnd in (("вернись на хром", 5), ("вернись в блокнот", 8), ("давай обратно на ютуб", 6),
                         ("вернёмся в хром", 5), ("верни на ютуб", 6)):
        assert sk.match("", normalize(phrase)) is not None, phrase
        assert b.activated[-1] == hwnd, phrase
        b.fg = hwnd
        wm.track()
    b.fg = 8
    wm.track()                                           # ютуб(6) -> блокнот(8)
    assert "Вернулся" in sk.match("", "вернись").reply and b.activated[-1] == 6


def test_vernis_na_background_tab_and_open_when_missing():
    chrome = W(5, "Почта - Google Chrome", 7, "chrome.exe")
    tabs = {5: [{"name": "Почта", "el": "t1"}, {"name": "Котики - YouTube", "el": "t2"}]}
    sk, b, wm, selected = _ws([chrome], fg=5, tabs=tabs)
    r = sk.match("", "вернись на ютуб")
    assert "вкладку" in r.reply and selected == ["t2"] and b.activated[-1] == 5
    opened = []
    media = types.SimpleNamespace(open_youtube=lambda: opened.append("yt"), open_ytmusic=lambda: opened.append("ytm"))
    sk, b, wm, selected = _ws([W(8, "Блокнот", 9, "notepad.exe", "Notepad")], fg=8, media=media)
    r = sk.match("", "вернись на ютуб")
    assert r.reply == "Открываю ютуб." and r.then is not None
    r.then()
    assert opened == ["yt"]
    assert sk.match("", "вернись на телеграм") is None   # unknown & not openable: not ours


def test_verni_vkladku_stays_reopen_tab():
    sk, *_ = _ws([W(5, "Почта - Google Chrome", 7, "chrome.exe")], fg=5)
    assert sk.match("", "верни вкладку") is None
    assert noname.classify("верни вкладку") == "browser"


# ------------------------------------------------------------------ no-name classification & gating
POS = {
    "Пауза.": "media", "Продолжи.": "media", "Громче!": "media", "Тише": "media", "Без звука": "media",
    "Следующее.": "media", "Стоп.": "media", "Включи второе видео.": "youtube", "Открой второй файл": "explorer",
    "Открой третью папку": "explorer", "Назад.": "nav", "Вперёд": "nav", "Назад три раза": "nav",
    "Перемотай назад на 30 секунд": "nav", "Новая вкладка": "browser", "Закрой вкладку": "browser",
    "Верни вкладку": "browser", "Вторая вкладка": "browser", "Обнови": "browser", "Листай вниз": "browser",
    "Увеличь масштаб": "browser", "Сверни": "windows", "Разверни": "windows", "Сверни всё": "windows",
    "Переключись на хром": "windows", "Вернись на ютуб": "windows", "Вернись": "windows", "Копируй": "windows",
    "Вставь": "windows", "Выдели всё": "windows", "Отмени": "windows", "Сохрани": "windows", "Скриншот": "windows",
    "Громкость 30%": "windows", "Напиши привет, как дела?": "dictation", "Пиши": "dictation", "Нажми энтер": "keys",
    "Нажал интер": "keys", "Альт таб": "keys", "Нажми вниз три раза": "keys", "Жмакни пробел": "keys",
    "Открыл ютуб": "apps", "Открой ютуб": "apps", "Закрой ютуб": "apps", "Закрыл хром": "apps",
    "Включи ютуб музыку": "apps", "Включи Rammstein Sonne": "music", "Ну давай пауза": "media",
}
NEG = ["Привет", "Как дела", "Я закрыл дверь и пошёл", "Выключи компьютер", "Перезагрузи компьютер",
       "Заблокируй компьютер", "Расскажи анекдот", "Открой банановый сайт", "Закрой глаза", "Переключись на что-нибудь",
       "Запиши заметку купить хлеб", "Это было второе видео которое я смотрел вчера",
       "Ну и что дальше будет", "Да", "Энтер", "Спасибо", "Включи", "Вернись домой", "Нажми банан", "Закрой себя",
       "Выключи Джарвис"]


@pytest.mark.parametrize("phrase,group", list(POS.items()))
def test_noname_positive(phrase, group):
    assert noname.classify(normalize(phrase)) == group


@pytest.mark.parametrize("phrase", NEG)
def test_noname_negative(phrase):
    assert noname.classify(normalize(phrase)) is None


def test_noname_gates():
    cfg = {"noname": {"enabled": True, "groups": {}}}
    assert noname.decide("Пауза.", cfg, CONF, True) == "пауза"
    assert noname.decide("Пауза.", cfg, (-0.9, 0.05), True) is None          # Whisper unsure
    assert noname.decide("Пауза.", cfg, (-0.2, 0.6), True) is None           # probably not speech
    assert noname.decide("Пауза.", cfg, None, True) is None
    assert noname.decide("Пауза.", cfg, CONF, False) is None                 # during / right after TTS
    assert noname.decide("Пауза.", {"noname": {"enabled": False}}, CONF, True) is None
    assert noname.decide("Пауза.", {"noname": {"groups": {"media": False}}}, CONF, True) is None
    assert noname.decide("Назад", {"noname": {"groups": {"media": False}}}, CONF, True) == "назад"
    assert noname.decide("Напиши Привет, мир!", cfg, CONF, True) == "Напиши Привет, мир!"   # case kept
    assert noname.decide("Открыл ютуб", cfg, CONF, True) == "открой ютуб"
    assert noname.decide("Пауза пауза пауза пауза пауза пауза пауза", cfg, CONF, True) is None   # > 6 words


def test_wakeflow_runs_noname_only_without_the_name():
    ran, quiet = [], []

    def wr(text):
        d = text.lower().startswith("джарвис")
        return types.SimpleNamespace(detected=d, command=text[8:] if d else "")
    phrases = iter(["Пауза.", "Джарвис, расскажи анекдот", "Привет"])
    flow = WakeFlow(detect=wr, listen=lambda **k: object(), transcribe=lambda a: next(phrases),
                    say=lambda s: None, execute=lambda c: ran.append(c),
                    noname=lambda t: quiet.append(t) or t == "Пауза.")
    assert flow.step() == "noname"
    assert flow.step() == "direct" and ran == ["расскажи анекдот"]
    assert flow.step() == "ignored" and quiet == ["Пауза.", "Привет"]


def test_assistant_noname_silent_and_never_llm_or_exit(monkeypatch):
    from jarvis.assistant import Assistant
    from jarvis.skills.base import ToolResult
    a = Assistant.__new__(Assistant)
    events, said, chats = [], [], []
    a.on_event = lambda e, **d: events.append((e, d))
    a.say = lambda *x, **k: said.append(x)
    a._chat = lambda t: chats.append(t)
    results = {"пауза": ToolResult("Поставил на паузу."), "стоп": ToolResult("Пока!", action="exit")}
    a.router = types.SimpleNamespace(handle=lambda t: results.get(t))
    a._respond_silent("пауза")
    a._respond_silent("стоп")
    a._respond_silent("непонятно")
    assert said == [] and chats == []
    assert [d["text"] for e, d in events if e == "user_text"] == ["пауза", "стоп"]
    assert all(d.get("source") == "noname" for e, d in events if e == "user_text")


# --- found by the manual test: no-name must never TYPE text that was meant for notes / code / telegram
@pytest.mark.parametrize("text", ["запиши заметку купить подарок маме", "запиши купить хлеб",
                                  "напиши скетч для ESP32 мигать светодиодом", "напиши маме в телеграм что буду",
                                  "напиши код на питоне", "запомни что кот Барсик"])
def test_noname_never_types_other_skills_text(text):
    assert noname.decide(text, {}, (-0.2, 0.05), True) is None


def test_fuzzy_keeps_real_verbs():
    from jarvis import fuzzy
    assert fuzzy.near_verb("запиши") == "" and fuzzy.near_verb("нашми") == "нажми"
    assert "напиши заметку" not in list(fuzzy.variants("запиши заметку"))
