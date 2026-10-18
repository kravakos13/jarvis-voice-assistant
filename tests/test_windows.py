"""Window control with a fake Win32 backend: phrase parsing (Russian, mat, Whisper misspellings), PWA vs
browser-tab handling, verification before «Закрыл», protection of Jarvis/shell/system windows."""
from __future__ import annotations

import types

import pytest

from jarvis import windows as wn
from jarvis.skills.windows_skill import WindowsSkill

JARVIS_PID = 100


class FakeBackend(wn.Backend):
    def __init__(self, wins, fg=0, ignore_close=(), dialog=(), ctrlw_ok=True):
        self.wins = {w.hwnd: w for w in wins}
        self.order = [w.hwnd for w in wins]
        self.fg, self.ignore, self.dialog, self.ctrlw_ok = fg, set(ignore_close), set(dialog), ctrlw_ok
        self.closed, self.killed, self.shown, self.ctrlw = [], [], [], []
        self.disabled: set = set()
        self.desktop = False

    def windows(self):
        return [self.wins[h] for h in self.order if h in self.wins]

    def foreground(self):
        return self.fg

    def activate(self, hwnd):
        self.activated = getattr(self, "activated", []) + [hwnd]
        self.fg = hwnd
        return hwnd in self.wins

    def alive(self, hwnd):
        return hwnd in self.wins

    def title(self, hwnd):
        return self.wins[hwnd].title if hwnd in self.wins else ""

    def enabled(self, hwnd):
        return hwnd not in self.disabled

    def post_close(self, hwnd):
        self.closed.append(hwnd)
        if hwnd in self.dialog:
            self.disabled.add(hwnd)            # "Save changes?" modal
        elif hwnd not in self.ignore:
            self.wins.pop(hwnd, None)
        return True

    def kill(self, pid, force=False):
        self.killed.append((pid, force))
        for h in [h for h, w in self.wins.items() if w.pid == pid]:
            self.wins.pop(h)
        return True

    def show(self, hwnd, cmd):
        self.shown.append((hwnd, cmd))
        return True

    def ctrl_w(self, hwnd):
        self.ctrlw.append(hwnd)
        if self.ctrlw_ok:
            w = self.wins[hwnd]
            self.wins[hwnd] = wn.Window(w.hwnd, "Новая вкладка - Google Chrome", w.pid, w.exe, w.cls)
        return self.ctrlw_ok

    def show_desktop(self):
        self.desktop = True
        return True


def W(hwnd, title, pid, exe, cls="Chrome_WidgetWin_1"):
    return wn.Window(hwnd, title, pid, exe, cls)


def desktop():
    return [
        W(1, "Джарвис", JARVIS_PID, "pythonw.exe", "Qt6QWindowIcon"),
        W(2, "Rick Astley - Never Gonna Give You Up - YouTube", 200, "chrome.exe"),       # YouTube PWA
        W(3, "YouTube Music", 201, "chrome.exe"),                                         # YT Music PWA
        W(4, "Почта - Google Chrome", 202, "chrome.exe"),                                 # normal Chrome
        W(5, "Steam", 300, "steam.exe", "SDL_app"),
        W(6, "Безымянный — Блокнот", 400, "notepad.exe", "Notepad"),
        W(7, "Program Manager", 500, "explorer.exe", "Progman"),
        W(8, "Загрузки", 500, "explorer.exe", "CabinetWClass"),
        W(9, "", 600, "dwm.exe", "x"),
    ]


def make(wins=None, **kw):
    b = FakeBackend(wins or desktop(), **kw)
    clock = {"t": 0.0}

    def sleep(x):
        clock["t"] += x
    wm = wn.WindowManager(b, own_pids={JARVIS_PID}, sleep=sleep, clock=lambda: clock["t"])
    ctx = types.SimpleNamespace(cfg={}, extra={}, llm=None)
    return WindowsSkill(ctx, manager=wm, start=False), b


@pytest.mark.parametrize("phrase", [
    "закрой ютуб", "закрой youtube", "закрой ютюб", "закрои ютуб", "закрой нахуй этот ютуб", "ну закрой ютубчик",
    "закрой ю туб", "закрой пожалуйста ютуб", "закрой-ка ютуб", "выруби ютуб", "закрой окно ютуба",
    "давай закрой ютуб блять", "закрой утуб",
])
def test_close_youtube_pwa_by_title(phrase):
    sk, b = make()
    r = sk.match(phrase, phrase.replace("-", " ") if "ка" not in phrase else phrase)
    assert r is not None, phrase
    assert r.reply == "Закрыл ютуб.", (phrase, r.reply)
    assert b.closed == [2] and b.killed == []                    # exactly the PWA window, nothing killed


@pytest.mark.parametrize("phrase", ["выруби музыку", "закрой ютуб музыку", "выключи музыку", "закрой музон",
                                    "закрой youtube music"])
def test_close_youtube_music(phrase, monkeypatch):
    from jarvis import uia
    monkeypatch.setattr(uia, "media_playing", lambda: False)   # idle: «выключи музыку» closes the app (1.0.4)
    sk, b = make()
    r = sk.match(phrase, phrase)
    assert r is not None and r.reply == "Закрыл ютуб музыку." and b.closed == [3], (phrase, r and r.reply)


def test_youtube_tab_in_normal_chrome_closes_only_the_tab():
    wins = [W(1, "Джарвис", JARVIS_PID, "pythonw.exe"), W(4, "Котики - YouTube - Google Chrome", 202, "chrome.exe")]
    sk, b = make(wins)
    r = sk.match("закрой ютуб", "закрой ютуб")
    assert r.reply == "Закрыл вкладку с ютубом." and b.ctrlw == [4] and b.closed == [] and 4 in b.wins


def test_pwa_preferred_over_tab():
    wins = [W(4, "Котики - YouTube - Google Chrome", 202, "chrome.exe"), W(2, "YouTube", 200, "chrome.exe")]
    sk, b = make(wins)
    sk.match("закрой ютуб", "закрой ютуб")
    assert b.closed == [2] and b.ctrlw == []


@pytest.mark.parametrize("phrase,hwnd,reply", [
    ("закрой хром", 4, "Закрыл Chrome."), ("закрой гугл хром", 4, "Закрыл Chrome."),
    ("закрой стим", 5, "Закрыл Steam."), ("закрой steam", 5, "Закрыл Steam."), ("закрой стем", 5, "Закрыл Steam."),
    ("закрой блокнот", 6, "Закрыл блокнот."), ("закрой блок нот", 6, "Закрыл блокнот."),
    ("закрой проводник", 8, "Закрыл проводник."),
])
def test_close_apps(phrase, hwnd, reply):
    sk, b = make()
    r = sk.match(phrase, phrase)
    assert r is not None and r.reply == reply and b.closed == [hwnd], (phrase, r and r.reply, b.closed)


def test_chrome_never_closes_pwas_or_kills_chrome():
    wins = [W(2, "YouTube", 200, "chrome.exe"), W(4, "Почта - Google Chrome", 202, "chrome.exe")]
    sk, b = make(wins, ignore_close=(4,))
    r = sk.match("закрой хром", "закрой хром")
    assert b.closed == [4] and b.killed == [] and 2 in b.wins          # browser: never taskkill
    assert r.reply.startswith("Не получилось закрыть Chrome")


@pytest.mark.parametrize("phrase", ["закрой окно", "закрой это", "закрой", "закрой это окно нахрен",
                                    "закрой текущее окно", "закрой его"])
def test_close_active_uses_previous_foreground_when_jarvis_is_focused(phrase):
    sk, b = make()
    b.fg = 6
    sk.wm.track()                          # user was in Notepad...
    b.fg = 1                               # ...then talked to Jarvis (its window is foreground)
    sk.wm.track()
    r = sk.match(phrase, phrase)
    assert r is not None and b.closed == [6] and r.reply.startswith("Закрыл"), (phrase, r and r.reply)


def test_close_active_foreground():
    sk, b = make(fg=5)
    assert sk.match("закрой это", "закрой это").reply == "Закрыл Steam." and b.closed == [5]


def test_never_closes_jarvis_shell_system():
    sk, b = make(fg=1)
    r = sk.match("закрой окно", "закрой окно")
    assert b.closed == [] and "какое окно закрыть" in r.reply
    assert sk.match("закрой джарвис", "закрой джарвис").reply.startswith("Себя я так не закрываю")
    for w in desktop():
        if w.hwnd in (1, 7, 9):
            assert sk.wm.protected(w)
    assert sk.wm.close(desktop()[6]) == "protected"


def test_not_found_and_unsaved_dialog():
    sk, b = make()
    r = sk.match("закрой дискорд", "закрой дискорд")
    assert r.reply == "Не вижу открытого окна «Дискорд»." and b.closed == []
    sk, b = make(dialog=(6,))
    r = sk.match("закрой блокнот", "закрой блокнот")
    assert "просит сохранить изменения" in r.reply and b.killed == []


def test_unresponsive_app_falls_back_to_taskkill_pid():
    sk, b = make(ignore_close=(5,))
    r = sk.match("закрой стим", "закрой стим")
    assert r.reply == "Закрыл Steam." and b.killed == [(300, False)]


def test_failed_close_is_reported_not_claimed():
    sk, b = make(ignore_close=(5,))
    b.kill = lambda pid, force=False: False                        # type: ignore[method-assign]
    r = sk.match("закрой стим", "закрой стим")
    assert r.reply.startswith("Не получилось закрыть") and 5 in b.wins


def test_minimize_maximize_and_minimize_all():
    sk, b = make(fg=6)
    assert sk.match("сверни блокнот", "сверни блокнот").reply == "Свернул блокнот." and b.shown == [(6, "minimize")]
    assert sk.match("разверни ютуб", "разверни ютуб").reply == "Развернул ютуб." and b.shown[-1] == (2, "maximize")
    assert sk.match("сверни все окна", "сверни все окна").reply == "Свернул все окна." and b.desktop
    assert sk.match("сверни окно", "сверни окно").reply.startswith("Свернул")


@pytest.mark.parametrize("phrase", ["выключи звук", "выключи компьютер", "выруби комп", "выключи свет",
                                    "закройся", "закрой задачу купить хлеб", "убери звук", "выключи таймер",
                                    "открой ютуб", "включи музыку", "как закрыть окно в линуксе"])
def test_not_ours(phrase):
    sk, _ = make()
    assert sk.match(phrase, phrase) is None, phrase


def test_tool_call_close_window():
    sk, b = make()
    assert sk.call("close_window", {"target": "YouTube"}).reply == "Закрыл ютуб." and b.closed == [2]
    sk, b = make(fg=5)
    assert sk.call("close_window", {"target": ""}).reply == "Закрыл Steam."
    assert sk.call("window_action", {"action": "minimize", "target": "все"}).reply == "Свернул все окна."


def test_no_backend_on_other_os():
    sk = WindowsSkill(types.SimpleNamespace(cfg={}, extra={}), manager=wn.WindowManager(None, own_pids=set()),
                      start=False)
    assert "только в Windows" in sk.match("закрой ютуб", "закрой ютуб").reply


def test_router_routes_close_before_llm():
    from pathlib import Path

    from jarvis.config import load_config
    from jarvis.router import Router
    from jarvis.skills.base import SkillContext
    cfg = load_config(use_local=False)
    cfg["alerts"]["enabled"] = False
    ctx = SkillContext(cfg=cfg, llm=None, say=lambda *a, **k: None, notify=lambda *a: None, project_root=Path("."))
    r = Router(ctx)
    assert "windows" in r.by_name
    res = r.handle("Джарвис, закрой ютуб!")
    assert res is not None and ("Windows" in res.reply or "ютуб" in res.reply)
    assert not any(t["function"]["name"] == "close_window" for t in r.tool_schemas())   # 1.0.8: core tools only
    cfg["llm"]["tools"] = "all"                                                        # «Нагрузка: максимальная»
    assert any(t["function"]["name"] == "close_window" for t in r.tool_schemas())


def test_own_pids_keep_launched_apps_closable(monkeypatch):
    """Programs Jarvis started (children) stay closable; explorer parent is not 'ours'."""
    import psutil

    class P:
        def __init__(self, pid, name):
            self.pid, self._n = pid, name

        def name(self):
            return self._n

    class Me:
        def parents(self):
            return [P(10, "pythonw.exe"), P(11, "explorer.exe")]

        def children(self, recursive=False):
            return [P(20, "QtWebEngineProcess.exe"), P(21, "notepad.exe"), P(22, "chrome.exe")]
    monkeypatch.setattr(psutil, "Process", lambda *a: Me())
    pids = wn._own_pids()
    assert {10, 20} <= pids and not ({11, 21, 22} & pids)


# ---------------------------------------------------------------- 1.0.7: user log 2026-10-18 routing misses
def test_log_phrases_parse_wake_without_comma_and_hit_fast_path():
    from jarvis.router import normalize
    from jarvis.wake import WakeWordDetector
    wd = WakeWordDetector.from_name("Джарвис", [])
    for said, expect in (("Джарвис Сюрни Хром.", "minimize"), ("Джарвис, переключись на хром.", "switch"),
                         ("Джарвис сверни хром", "minimize"), ("Джарвис, разверни хром.", "maximize")):
        r = wd.detect(said)
        assert r.detected and r.command, said
        sk, b = make([W(1, "Джарвис", JARVIS_PID, "pythonw.exe"), W(4, "Новая вкладка - Google Chrome", 202, "chrome.exe")])
        t = normalize(r.command)
        res = sk.match(r.command, t)
        assert res is not None, said
        if expect == "switch":
            assert res.reply == "Переключился на Chrome." and b.activated == [4], res.reply
        else:
            assert res.reply.startswith("Свернул" if expect == "minimize" else "Развернул"), res.reply


def test_switch_to_not_open_app_is_left_to_apps():
    sk, b = make([W(1, "Джарвис", JARVIS_PID, "pythonw.exe")])
    assert sk.match("переключись на телеграм", "переключись на телеграм") is None
