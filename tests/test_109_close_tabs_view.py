"""1.0.9: bare «закрой» (active window at speech start, never games), game guard, UWP Store apps (Calculator),
quitting tray apps (Steam, WiFi Mouse…), browser tab phrasings, YouTube view commands. Simulated windows only."""
from __future__ import annotations

import types

import numpy as np
import pytest

from jarvis import games, noname, procs
from jarvis import windows as wn
from jarvis.skills.windows_skill import WindowsSkill
from tests.test_windows import JARVIS_PID, FakeBackend


def W(hwnd, title, pid, exe, cls="Chrome_WidgetWin_1", host_pid=0):
    return wn.Window(hwnd, title, pid, exe, cls, host_pid=host_pid)


class GB(FakeBackend):
    """FakeBackend + exe paths, fullscreen state, owned popups, SC_CLOSE / UIA close."""

    def __init__(self, wins, paths=None, full=None, popup=(), uwp_ignore=(), sys_ok=True, **kw):
        super().__init__(wins, **kw)
        self.paths, self.full, self.popup = paths or {}, full or {}, set(popup)
        self.uwp_ignore, self.sys_ok = set(uwp_ignore), sys_ok
        self.sysclosed, self.uiaclosed = [], []

    def exe_path(self, pid):
        return self.paths.get(pid, "")

    def fullscreen(self, hwnd):
        return self.full.get(hwnd, (False, False))

    def has_popup(self, hwnd):
        return hwnd in self.popup

    def post_close(self, hwnd):
        if hwnd in self.uwp_ignore:
            self.closed.append(hwnd)
            return True
        return super().post_close(hwnd)

    def sys_close(self, hwnd):
        self.sysclosed.append(hwnd)
        if self.sys_ok and hwnd in self.uwp_ignore:
            self.wins.pop(hwnd, None)
        return True

    def uia_close(self, hwnd):
        self.uiaclosed.append(hwnd)
        return False


class Launcher:
    def __init__(self, names=()):
        self.names = list(names)

    def games(self):
        return [types.SimpleNamespace(name=n, is_game=True) for n in self.names]

    def find(self, q):
        for n in self.names:
            if games._norm(q)[:5] in ("кибер", "cyber") and "cyber" in n.lower():
                return types.SimpleNamespace(name=n, is_game=True, target="steam://rungameid/1"), 0.9
        return None, 0.0


def make(wins, fg=0, cfg=None, launcher=None, profile="work", quitter=None, steam_game=False, **kw):
    b = GB(wins, fg=fg, **kw)
    clock = {"t": 0.0}

    def sleep(x):
        clock["t"] += x
    wm = wn.WindowManager(b, own_pids={JARVIS_PID}, sleep=sleep, clock=lambda: clock["t"])
    ctx = types.SimpleNamespace(cfg=cfg or {}, extra={"get_profile": lambda: profile}, llm=None)
    if launcher is not None:
        ctx.extra["launcher"] = launcher
    q = quitter if quitter is not None else procs.Quitter(procs=lambda: [], alive=lambda p: False,
                                                          close_windows=lambda s: 0, taskkill=lambda *a, **k: True,
                                                          run=lambda a: True, sleep=sleep, clock=lambda: clock["t"])
    sk = WindowsSkill(ctx, manager=wm, start=False, quitter=q, steam_game_running=lambda: steam_game)
    return sk, b, wm, ctx


JARVIS = W(1, "Джарвис", JARVIS_PID, "pythonw.exe", "Qt6QWindowIcon")
NOTE = W(6, "Безымянный — Блокнот", 400, "notepad.exe", "Notepad")
CHROME = W(4, "Почта - Google Chrome", 202, "chrome.exe")
YT_TAB = W(2, "Котики - YouTube - Google Chrome", 203, "chrome.exe")
DESKTOP = W(7, "Program Manager", 500, "explorer.exe", "Progman")
TASKBAR = W(10, "", 500, "explorer.exe", "Shell_TrayWnd")
FOLDER = W(8, "Загрузки", 500, "explorer.exe", "CabinetWClass")


def say(sk, phrase):
    return sk.match(phrase, phrase)


# ---------------------------------------------------------------- bare «закрой»
@pytest.mark.parametrize("phrase", ["закрой", "закрой это", "закрой окно", "закрой текущее", "закрыл", "закройте",
                                    "закрой это окно", "закрой активное окно", "ну закрой нахрен"])
def test_bare_close_closes_active_window(phrase):
    sk, b, wm, _ = make([JARVIS, CHROME, NOTE], fg=6)
    r = say(sk, phrase)
    assert b.closed == [6] and "Закрыл" in r.reply


def test_bare_close_uses_window_in_front_at_speech_start():
    sk, b, wm, _ = make([JARVIS, CHROME, NOTE], fg=6)
    wm.mark_start()                       # user starts «закрой» while Notepad is in front
    b.fg = 4                              # …then focus moves to Chrome before the command is handled
    wm.commit_utterance()
    say(sk, "закрой")
    assert b.closed == [6]


def test_bare_close_when_jarvis_in_front_uses_previous_window():
    sk, b, wm, _ = make([JARVIS, CHROME, NOTE], fg=6)
    wm.track()
    b.fg = 1                              # the user clicked Jarvis's window
    wm.mark_start()
    wm.commit_utterance()
    say(sk, "закрой")
    assert b.closed == [6]


def test_bare_close_snapshot_expires_and_text_mode_clears_it():
    sk, b, wm, _ = make([JARVIS, CHROME, NOTE], fg=6)
    wm.mark_start()
    wm.commit_utterance()
    b.fg = 4
    wm.cmd_at -= 100                      # stale snapshot -> the current window
    say(sk, "закрой")
    assert b.closed == [4]


def test_bare_close_in_browser_closes_whole_window_even_with_youtube():
    sk, b, wm, _ = make([JARVIS, YT_TAB], fg=2)
    say(sk, "закрой")
    assert b.closed == [2] and b.ctrlw == []
    sk2, b2, *_ = make([JARVIS, YT_TAB], fg=2)
    say(sk2, "закрой вкладку")
    assert b2.ctrlw == [2] and b2.closed == []


@pytest.mark.parametrize("fg_win", [DESKTOP, TASKBAR])
def test_bare_close_never_desktop_taskbar(fg_win):
    sk, b, wm, _ = make([JARVIS, NOTE, fg_win], fg=fg_win.hwnd)
    wm.mark_start()
    wm.commit_utterance()
    r = say(sk, "закрой")
    assert b.closed == [] and b.killed == [] and "какое окно" in r.reply


def test_bare_close_explorer_folder_ok_and_never_jarvis():
    sk, b, *_ = make([JARVIS, FOLDER], fg=8)
    say(sk, "закрой")
    assert b.closed == [8]
    sk2, b2, *_ = make([JARVIS], fg=1)
    say(sk2, "закрой")
    assert b2.closed == []


def test_save_dialog_never_killed():
    sk, b, *_ = make([JARVIS, NOTE], fg=6, dialog={6})
    r = say(sk, "закрой")
    assert "просит сохранить" in r.reply and b.killed == []
    sk2, b2, *_ = make([JARVIS, NOTE], fg=6, ignore_close={6}, popup={6})    # Win11: owned popup, still enabled
    r2 = say(sk2, "закрой")
    assert "просит сохранить" in r2.reply and b2.killed == []


# ---------------------------------------------------------------- games
GAMES = [
    ("library", W(20, "ELDEN RING", 900, "eldenring2.exe", "x"), {900: r"D:\SteamLibrary\steamapps\common\ELDEN\x.exe"}, {}),
    ("epic", W(20, "Game", 900, "fn.exe", "x"), {900: r"C:\Program Files\Epic Games\Fortnite\fn.exe"}, {}),
    ("gog", W(20, "Witcher", 900, "w.exe", "x"), {900: r"C:\GOG Galaxy\Games\Witcher\w.exe"}, {}),
    ("riot", W(20, "VALORANT", 900, "valorant-win64-shipping.exe", "x"), {}, {}),
    ("known", W(20, "Counter-Strike 2", 900, "cs2.exe", "SDL_app"), {}, {}),
    ("shipping", W(20, "Some UE game", 900, "someue-win64-shipping.exe", "UnrealWindow"), {}, {}),
    ("borderless", W(20, "Unknown", 900, "unknown.exe", "x"), {}, {20: (True, True)}),
]


@pytest.mark.parametrize("case,win,paths,full", GAMES, ids=[g[0] for g in GAMES])
def test_bare_close_never_closes_games(case, win, paths, full):
    sk, b, wm, ctx = make([JARVIS, win], fg=20, paths=paths, full=full)
    r = say(sk, "закрой")
    assert b.closed == [] and b.killed == [] and r.reply == "Игры не закрываю."
    ctx.extra["noname"] = True
    assert say(sk, "закрой").reply == "" and b.closed == []


def test_game_by_launcher_index_title_and_game_mode():
    sk, b, *_ = make([JARVIS, W(20, "Hades II", 900, "hades2.exe", "x")], fg=20, launcher=Launcher(["Hades II"]))
    assert say(sk, "закрой").reply == "Игры не закрываю." and b.closed == []
    # fullscreen with caption: a game only in game mode
    win = W(21, "Something", 901, "thing.exe", "x")
    sk2, b2, *_ = make([JARVIS, win], fg=21, full={21: (True, False)}, profile="work")
    say(sk2, "закрой")
    assert b2.closed == [21]
    sk3, b3, *_ = make([JARVIS, win], fg=21, full={21: (True, False)}, profile="game")
    assert say(sk3, "закрой").reply == "Игры не закрываю." and b3.closed == []


def test_fullscreen_browser_video_player_not_games():
    for w in (W(2, "YouTube - Google Chrome", 203, "chrome.exe"), W(30, "film.mkv - VLC", 950, "vlc.exe", "Qt")):
        sk, b, *_ = make([JARVIS, w], fg=w.hwnd, full={w.hwnd: (True, True)})
        say(sk, "закрой")
        assert b.closed == [w.hwnd]


def test_launchers_are_not_games():
    w = W(31, "Epic Games Launcher", 960, "epicgameslauncher.exe", "x")
    assert games.why_game(w, path=r"C:\Program Files (x86)\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe") == ""
    assert games.why_game(W(32, "Steam", 961, "steam.exe"), path=r"C:\Program Files (x86)\Steam\steam.exe") == ""


def test_user_blocklist():
    obs = W(40, "OBS 30.1", 970, "obs64.exe", "Qt")
    sk, b, *_ = make([JARVIS, obs], fg=40, cfg={"windows": {"never_close": ["obs64.exe"]}})
    assert "Не закрывать" in say(sk, "закрой").reply and b.closed == []
    abl = W(41, "Ableton Live 12 Suite", 971, "ableton live 12 suite.exe", "x")
    sk2, b2, *_ = make([JARVIS, abl], fg=41, cfg={"windows": {"never_close": ["Ableton"]}})
    say(sk2, "закрой")
    assert b2.closed == []


CYBER = W(50, "Cyberpunk 2077 (C) 2020 by CD Projekt RED", 990, "cyberpunk2077.exe", "x")


@pytest.mark.parametrize("phrase", ["закрой киберпанк", "закройки берпанк"])
def test_named_game_confirm_with_name_hint_without(phrase):
    sk, b, wm, ctx = make([JARVIS, NOTE, CYBER], fg=6, launcher=Launcher(["Cyberpunk 2077"]))
    r = say(sk, phrase)
    assert r.confirm is not None and "Точно закрыть Cyberpunk 2077" in r.confirm.prompt and b.closed == []
    r2 = r.confirm.on_yes()
    assert b.closed == [50] and b.killed == [] and "Закрыл" in r2.reply
    sk, b, wm, ctx = make([JARVIS, NOTE, CYBER], fg=6, launcher=Launcher(["Cyberpunk 2077"]))
    ctx.extra["noname"] = True
    r3 = say(sk, phrase)
    assert r3.noname_say and "только по имени" in r3.reply and r3.confirm is None


def test_close_game_explicit_with_confirm_never_killed():
    sk, b, wm, ctx = make([JARVIS, NOTE, W(20, "CS2", 900, "cs2.exe")], fg=6, ignore_close={20})
    r = say(sk, "закрой игру")
    assert r.confirm is not None and b.closed == []
    r2 = r.confirm.on_yes()
    assert b.closed == [20] and b.killed == [] and "через меню игры" in r2.reply
    assert noname.decide("закрой игру", {}, (-0.2, 0.05), True) is None
    ctx.extra["noname"] = True
    assert say(sk, "закрой игру").reply == "" and say(sk, "закрой игру").confirm is None
    sk2, *_ = make([JARVIS, NOTE], fg=6)
    assert "Не вижу открытой игры" in say(sk2, "закрой игру").reply


@pytest.mark.parametrize("phrase", ["закрой", "Закрой это.", "закрой окно", "закрой текущее", "закрыл", "закройте"])
def test_noname_bare_close_is_windows_group(phrase):
    assert noname.classify(phrase) == "windows"
    cfg = {"noname": {"groups": {"windows": False}}}
    assert noname.decide(phrase, cfg, (-0.2, 0.05), True) is None


# ---------------------------------------------------------------- UWP (Calculator & co)
CALC = W(60, "Калькулятор", 1200, "calculatorapp.exe", "ApplicationFrameWindow", host_pid=1100)
CALC_MIN = W(61, "Калькулятор", 1100, "applicationframehost.exe", "ApplicationFrameWindow")   # suspended / minimized
SETTINGS = W(62, "Параметры", 1300, "systemsettings.exe", "ApplicationFrameWindow", host_pid=1100)


@pytest.mark.parametrize("phrase", ["закрой калькулятор", "закрой калк", "закрой calculator"])
def test_close_calculator_uwp(phrase):
    sk, b, *_ = make([JARVIS, NOTE, CALC], fg=6)
    r = say(sk, phrase)
    assert b.closed == [60] and r.reply.startswith("Закрыл")


def test_close_uwp_unresolved_frame_by_title_and_bare():
    sk, b, *_ = make([JARVIS, CALC_MIN], fg=6)
    say(sk, "закрой калькулятор")
    assert b.closed == [61]
    sk2, b2, *_ = make([JARVIS, SETTINGS], fg=62)
    say(sk2, "закрой")
    assert b2.closed == [62]


def test_uwp_sc_close_then_uia_then_kill_only_safe_never_host():
    sk, b, *_ = make([JARVIS, CALC], fg=60, uwp_ignore={60})
    say(sk, "закрой")
    assert b.sysclosed == [60] and b.killed == []                    # SC_CLOSE worked
    sk2, b2, *_ = make([JARVIS, CALC], fg=60, uwp_ignore={60}, sys_ok=False)
    say(sk2, "закрой")
    assert b2.uiaclosed == [60] and b2.killed == [(1200, True)]       # calculator: safe to kill, the APP pid
    s3 = W(63, "Параметры", 1300, "systemsettings.exe", "ApplicationFrameWindow", host_pid=1100)
    sk3, b3, *_ = make([JARVIS, s3], fg=63, uwp_ignore={63}, sys_ok=False)
    r3 = say(sk3, "закрой")
    assert b3.killed == [] and "не закрывается" in r3.reply          # never kill Settings / the frame host
    sk4, b4, *_ = make([JARVIS, CALC_MIN], fg=61, uwp_ignore={61}, sys_ok=False)
    say(sk4, "закрой")
    assert b4.killed == []                                            # host pid only -> never killed


# ---------------------------------------------------------------- quitting tray apps
class FakeProcs:
    def __init__(self, plist, exits_on=("close",), never=()):
        self.p = {p.pid: p for p in plist}
        self.exits_on, self.never = set(exits_on), set(never)
        self.log: list = []

    def procs(self):
        return list(self.p.values())

    def alive(self, pid):
        return pid in self.p

    def _exit(self, pids, how):
        for pid in list(pids):
            if pid in self.p and how in self.exits_on and self.p[pid].name not in self.never:
                self.p.pop(pid)

    def close_windows(self, pids):
        self.log.append(("close", sorted(pids)))
        self._exit(pids, "close")
        return len(pids)

    def taskkill(self, pid, force=False):
        self.log.append(("kill", pid, force))
        if pid in self.p and (force or "taskkill" in self.exits_on):
            if force or self.p[pid].name not in self.never:
                self.p.pop(pid)
        return True

    def run(self, args):
        self.log.append(("run", args))
        if args[-1] == "-shutdown":
            self._exit([p.pid for p in self.p.values() if p.name.startswith("steam")], "shutdown")
        return True


def quitter(fp):
    clock = {"t": 0.0}

    def sleep(x):
        clock["t"] += x
    return procs.Quitter(procs=fp.procs, alive=fp.alive, close_windows=fp.close_windows, taskkill=fp.taskkill,
                         run=fp.run, sleep=sleep, clock=lambda: clock["t"], own_pids={1})


STEAM = [procs.Proc(700, "steam.exe", r"C:\Program Files (x86)\Steam\steam.exe"),
         procs.Proc(701, "steamwebhelper.exe", r"C:\Program Files (x86)\Steam\bin\cef\steamwebhelper.exe")]


def test_close_steam_tray_uses_shutdown_and_verifies():
    fp = FakeProcs(STEAM, exits_on=("shutdown",))
    offers = W(70, "Специальные предложения", 701, "steamwebhelper.exe")
    sk, b, *_ = make([JARVIS, offers], fg=6, quitter=quitter(fp))
    r = say(sk, "закрой стим")
    assert fp.log[0] == ("run", [r"C:\Program Files (x86)\Steam\steam.exe", "-shutdown"])
    assert r.reply == "Закрыл Steam." and not fp.p


def test_close_steam_while_game_running_asks():
    fp = FakeProcs(STEAM, exits_on=("shutdown",))
    sk, *_ = make([JARVIS], fg=6, quitter=quitter(fp), steam_game=True)
    r = say(sk, "закрой стим")
    assert r.confirm is not None and "запущена игра" in r.confirm.prompt and fp.log == []
    assert r.confirm.on_yes().reply == "Закрыл Steam."


@pytest.mark.parametrize("phrase", ["закрой вайфай маус", "закрой вифи маус", "закрой wifi mouse"])
def test_close_tray_only_app_wifi_mouse(phrase):
    fp = FakeProcs([procs.Proc(800, "wifimouseserver.exe", r"C:\Program Files\WiFi Mouse\WiFiMouseServer.exe")],
                   exits_on=("close",))
    sk, *_ = make([JARVIS], fg=6, quitter=quitter(fp))
    r = say(sk, phrase)
    assert r.reply == "Закрыл WiFi Mouse." and ("close", [800]) in fp.log


def test_quit_escalation_graceful_then_safe_force_then_ask():
    fp = FakeProcs([procs.Proc(810, "discord.exe")], exits_on=())       # ignores WM_CLOSE and graceful taskkill
    sk, *_ = make([JARVIS], fg=6, quitter=quitter(fp))
    r = say(sk, "закрой дискорд")
    assert ("kill", 810, False) in fp.log and ("kill", 810, True) in fp.log and r.reply == "Закрыл Дискорд."
    fp2 = FakeProcs([procs.Proc(820, "obs64.exe")], exits_on=())          # not in SAFE_KILL -> ask
    sk2, *_ = make([JARVIS], fg=6, quitter=quitter(fp2))
    r2 = say(sk2, "закрой обс")
    assert r2.confirm is not None and "Убить процесс" in r2.confirm.prompt and ("kill", 820, True) not in fp2.log
    assert r2.confirm.on_yes().reply.startswith("Закрыл") and ("kill", 820, True) in fp2.log


def test_quit_never_critical_or_own_or_games():
    crit = [procs.Proc(1, "pythonw.exe"), procs.Proc(2, "explorer.exe"), procs.Proc(3, "svchost.exe"),
            procs.Proc(4, "ollama.exe"), procs.Proc(5, "audiodg.exe"), procs.Proc(6, "msmpeng.exe")]
    for p in crit:
        assert procs.match(p.name.replace(".exe", ""), crit) == []
    q = quitter(FakeProcs(crit))
    assert q.quit(crit) == ("none", [])
    fp = FakeProcs([procs.Proc(830, "cs2.exe", r"D:\steamapps\common\cs2\cs2.exe")])
    sk, *_ = make([JARVIS], fg=6, quitter=quitter(fp))
    r = say(sk, "закрой cs2")
    assert fp.log == [] and "Не вижу" in r.reply


def test_taskkill_has_no_tree_flag():
    import inspect
    assert '"/T"' not in inspect.getsource(procs.taskkill)


# ---------------------------------------------------------------- browser tabs
@pytest.fixture
def hotkeys():
    from jarvis.skills.hotkeys_skill import HotkeysSkill
    pressed: list = []
    b = GB([JARVIS, CHROME], fg=4)
    wm = wn.WindowManager(b, own_pids={JARVIS_PID}, sleep=lambda s: None)
    ctx = types.SimpleNamespace(cfg={}, extra={"wm": wm}, llm=None)
    import jarvis.keyinput as ki
    old = ki.focus_target
    ki.focus_target = lambda wm, settle_s=0.15: 4
    sk = HotkeysSkill(ctx, press=lambda c: pressed.append(c) or True)
    yield sk, pressed, b
    ki.focus_target = old


@pytest.mark.parametrize("phrase,keys", [
    ("вторая", ["ctrl+2"]), ("третья вкладка", ["ctrl+3"]), ("на третью вкладку", ["ctrl+3"]),
    ("вкладка три", ["ctrl+3"]), ("вкладка 3", ["ctrl+3"]), ("открой пятую вкладку", ["ctrl+5"]),
    ("перейди на пятую вкладку", ["ctrl+5"]), ("переключись на пятую вкладку", ["ctrl+5"]),
    ("третью вкладку открой", ["ctrl+3"]), ("последняя вкладка", ["ctrl+9"]), ("вторую фкладку", ["ctrl+2"]),
    ("вкладки пять", ["ctrl+5"]), ("на вкладку два", ["ctrl+2"]), ("вторая вкладочка", ["ctrl+2"]),
    ("восьмая вкладка", ["ctrl+8"]), ("десятая вкладка", ["ctrl+1"] + ["ctrl+tab"] * 9),
])
def test_tab_phrasings(hotkeys, phrase, keys):
    sk, pressed, _ = hotkeys
    assert sk.match(phrase, phrase) is not None and pressed == keys


@pytest.mark.parametrize("phrase", ["третье", "второе", "главная", "новая"])
def test_neuter_and_words_are_not_tabs(hotkeys, phrase):
    sk, pressed, _ = hotkeys
    sk.match(phrase, phrase)
    assert pressed == []


def test_bare_feminine_only_in_browser(hotkeys):
    sk, pressed, b = hotkeys
    b.fg = 6
    b.wins[6] = NOTE
    b.order.append(6)
    assert sk.match("вторая", "вторая") is None and pressed == []


@pytest.mark.parametrize("phrase", ["вторая", "третья вкладка", "на третью вкладку", "перейди на пятую вкладку",
                                    "последняя вкладка", "третью вкладку открой"])
def test_tab_phrases_allowed_no_name(phrase):
    assert noname.classify(phrase) in ("browser", "youtube", "explorer")


# ---------------------------------------------------------------- YouTube view
def view(fg_win, full=False, edit=(False,)):
    from jarvis.skills.view_skill import ViewSkill
    import jarvis.keyinput as ki
    pressed: list = []
    b = GB([JARVIS, fg_win], fg=fg_win.hwnd, full={fg_win.hwnd: (full, full)})
    wm = wn.WindowManager(b, own_pids={JARVIS_PID}, sleep=lambda s: None)
    ctx = types.SimpleNamespace(cfg={}, extra={"wm": wm}, llm=None)
    edits = list(edit)
    focused = []
    sk = ViewSkill(ctx, press=lambda c, *a: pressed.append(c) or True,
                   focus_in_edit=lambda: edits.pop(0) if len(edits) > 1 else edits[0],
                   focus_player=lambda h: focused.append(h) or True)
    ki_old = ki.focus_target
    ki.focus_target = lambda wm, settle_s=0.15: fg_win.hwnd
    return sk, pressed, focused, lambda: setattr(ki, "focus_target", ki_old)


YT = W(2, "Котики - YouTube - Google Chrome", 203, "chrome.exe")
YTM = W(3, "Thunderstruck | YouTube Music", 204, "chrome.exe")
SITE = W(4, "Хабр - Google Chrome", 205, "chrome.exe")


@pytest.mark.parametrize("phrase,full,keys", [
    ("полный экран", False, ["f"]), ("на весь экран", False, ["f"]), ("разверни видео", False, ["f"]),
    ("полный экран", True, []),                                        # idempotent
    ("обычный экран", True, ["esc"]), ("малый экран", True, ["esc"]), ("выйди из полного экрана", True, ["esc"]),
    ("сверни видео", True, ["esc"]), ("обычный экран", False, []),
    ("кинотеатр", False, ["t"]), ("широкий экран", False, ["t"]), ("мини-плеер", False, ["i"]),
    ("субтитры", False, ["c"]), ("включи субтитры", False, ["c"]),
])
def test_youtube_view(phrase, full, keys):
    sk, pressed, _, undo = view(YT, full)
    try:
        assert sk.match(phrase, phrase) is not None and pressed == keys
    finally:
        undo()


def test_view_focus_moves_from_search_box_or_refuses():
    sk, pressed, focused, undo = view(YT, False, edit=(True, False))
    try:
        sk.match("полный экран", "полный экран")
        assert focused == [2] and pressed == ["f"]
    finally:
        undo()
    sk2, pressed2, _, undo2 = view(YT, False, edit=(True,))           # stuck in the search box: never type «f»
    try:
        r = sk2.match("полный экран", "полный экран")
        assert "f" not in pressed2 and "поле поиска" in r.reply
    finally:
        undo2()


@pytest.mark.parametrize("win", [SITE, YTM])
def test_view_other_site_or_ytmusic_is_f11(win):
    sk, pressed, _, undo = view(win, False)
    try:
        sk.match("полный экран", "полный экран")
        assert pressed == ["f11"]
        sk.match("кинотеатр", "кинотеатр")
        assert pressed == ["f11"]                                    # YouTube-only keys are not sent
    finally:
        undo()
    sk2, pressed2, _, undo2 = view(win, True)
    try:
        sk2.match("обычный экран", "обычный экран")
        assert pressed2 == ["f11"]
    finally:
        undo2()


def test_view_not_browser_and_bare_minimize_untouched():
    sk, pressed, _, undo = view(NOTE, False)
    try:
        assert sk.match("полный экран", "полный экран") is None and pressed == []
        assert sk.match("сверни", "сверни") is None
    finally:
        undo()
    assert noname.classify("полный экран") == "media" and noname.classify("сверни") == "windows"
    from jarvis.skills.view_skill import parse_view
    for p in ("разверни ютуб", "сверни ютуб", "сверни", "разверни", "разверни хром"):
        assert parse_view(p) is None                                  # window maximize / minimize
    assert parse_view("ютуб на весь экран") == "full" and parse_view("разверни видео") == "full"


# ---------------------------------------------------------------- speech-start hook
def test_segment_utterance_calls_on_start_once():
    from jarvis.audio import segment_utterance

    class V:
        def is_speech(self, f, r):
            return bool(f[0])
    frames = [np.zeros(480, np.float32)] * 3 + [np.ones(480, np.float32)] * 20 + [np.zeros(480, np.float32)] * 40
    calls = []
    out = segment_utterance(iter(frames), V(), 16000, on_start=lambda: calls.append(1))
    assert out is not None and calls == [1]


def test_noname_named_game_and_tray_targets_reach_windows_skill():
    sk, b, wm, ctx = make([JARVIS, NOTE, CYBER], fg=6, launcher=Launcher(["Cyberpunk 2077"]))
    router = types.SimpleNamespace(by_name={"windows": sk})
    for phrase in ("закрой киберпанк", "закройки берпанк"):
        assert noname.classify(phrase, router) in ("apps", "windows")
    assert noname.classify("закрой вайфай маус") in ("apps", "windows")
    assert noname.classify("закрой фигню какую-то", router) is None


# ---------------------------------------------------------------- «закрой весь/все/всё X»
def folders(n, start=80):
    return [W(start + i, f"Папка {i}", 500, "explorer.exe", "CabinetWClass") for i in range(n)]


@pytest.mark.parametrize("phrase", ["закрой весь проводник", "закрой все окна проводника", "закрой все проводники",
                                    "закрой проводник полностью", "закрой проводник целиком", "закройте всё проводник"])
def test_close_all_explorer_windows_never_shell(phrase):
    sk, b, *_ = make([JARVIS, DESKTOP, TASKBAR, NOTE] + folders(4), fg=6)
    r = say(sk, phrase)
    assert sorted(b.closed) == [80, 81, 82, 83] and b.killed == []
    assert r.reply == "Закрыл 4 окна проводника."


def test_close_all_chrome_and_counts_dialogs():
    c1, c2, c3 = (W(90 + i, f"Сайт {i} - Google Chrome", 210 + i, "chrome.exe") for i in range(3))
    sk, b, *_ = make([JARVIS, NOTE, c1, c2, c3], fg=6, dialog={92})
    r = say(sk, "закрой все окна хрома")
    assert sorted(b.closed) == [90, 91, 92] and b.killed == []
    assert r.reply.startswith("Закрыл 2 окна «Chrome».") and "1 просит сохранить" in r.reply
    sk2, b2, *_ = make([JARVIS, NOTE, c1, c2], fg=6)
    assert say(sk2, "закрой весь хром").reply == "Закрыл 2 окна «Chrome»."


def test_close_all_windows_of_active_app():
    sk, b, *_ = make([JARVIS, NOTE, W(16, "Заметки — Блокнот", 401, "notepad.exe", "Notepad"), CHROME], fg=6)
    r = say(sk, "закрой все окна")
    assert sorted(b.closed) == [6, 16] and 4 not in b.closed and r.reply.startswith("Закрыл 2 окна")
    sk2, b2, *_ = make([JARVIS, CHROME] + folders(3), fg=81)
    assert say(sk2, "закрой всё").reply == "Закрыл 3 окна проводника." and 4 not in b2.closed


def test_close_all_never_games():
    g1, g2 = W(95, "CS2", 900, "cs2.exe"), W(96, "Dota 2", 901, "dota2.exe")
    sk, b, *_ = make([JARVIS, g1, g2, NOTE], fg=95)
    say(sk, "закрой все окна")
    assert b.closed == []
    sk2, b2, *_ = make([JARVIS, NOTE, W(97, "Notepad++", 402, "notepad++.exe")], fg=6)
    say(sk2, "закрой всё")
    assert b2.closed == [6]


def test_close_all_single_window_keeps_short_reply():
    sk, b, *_ = make([JARVIS, NOTE] + folders(1), fg=6)
    assert say(sk, "закрой весь проводник").reply == "Закрыл проводник."


def test_close_all_youtube_tabs_in_browsers_and_pwa():
    pwa = W(2, "Котики - YouTube", 200, "chrome.exe", "Chrome_WidgetWin_1")
    tabs_state = {4: ["Котики - YouTube", "Почта", "Музыка - YouTube", "YouTube Music"]}

    def tabs(h):
        return [{"name": n, "el": (h, n)} for n in tabs_state.get(h, [])]
    sel = {}

    def select(el):
        sel["cur"] = el
        return True
    b = GB([JARVIS, pwa, CHROME], fg=4)
    b.ctrl_w = lambda h: (tabs_state[h].remove(sel["cur"][1]), True)[1]
    wm = wn.WindowManager(b, own_pids={JARVIS_PID}, sleep=lambda s: None)
    ctx = types.SimpleNamespace(cfg={}, extra={}, llm=None)
    sk = WindowsSkill(ctx, manager=wm, start=False, tabs=tabs, select_tab=select)
    r = say(sk, "закрой весь ютуб")
    assert tabs_state[4] == ["Почта", "YouTube Music"]                       # YT Music is not «ютуб»
    assert "Закрыл ютуб" in r.reply and b.killed == []


@pytest.mark.parametrize("phrase,group", [("закрой весь проводник", "apps"), ("закрой все окна хрома", "apps"),
                                          ("закрой весь ютуб", "apps"), ("закрой все окна", "windows"),
                                          ("закрой всё", "windows")])
def test_close_all_no_name_groups(phrase, group):
    assert noname.classify(phrase) == group                # named = «apps» like «закрой ютуб»


def test_genitive_and_plural():
    from jarvis.skills.windows_skill import genitive, plural
    assert [genitive(x) for x in ("проводник", "блокнот", "телеграм", "Chrome")] == \
        ["проводника", "блокнота", "телеграма", "«Chrome»"]
    assert [plural(n, "окно", "окна", "окон") for n in (1, 2, 5, 11, 21, 22, 25)] == \
        ["окно", "окна", "окон", "окон", "окно", "окна", "окон"]


@pytest.mark.parametrize("phrase", ["статья", "семья", "главная", "новая", "свинья", "третье", "любая"])
def test_bare_words_are_not_tab_numbers_no_name(phrase):
    assert noname.classify(phrase) is None
