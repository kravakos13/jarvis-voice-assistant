"""«Закрой ютуб / окно / это», «сверни», «разверни» — real window control with verification.

Replies «Закрыл …» only after the window is really gone (re-checked for ~1.5 s); otherwise says what happened
(not found / asks to save / did not close). See jarvis/windows.py for how windows are found and closed.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Optional

from .. import platform_utils as pu
from .. import windows as wn
from .. import games, procs
from .base import Confirm, Skill, ToolResult, fn, s

log = logging.getLogger(__name__)

CLOSE_V = (r"закрой|закрои|закрою|закройте|закрывай|закрыть|закрой ка|закрыл|закрыла|закроем|закрывайте|выруби|вырубай|вырубить|вырубите|выключи|"
           r"выключай|убей|прибей|грохни|убери")
MIN_V = (r"сверни|свернуть|сверни ка|сверните|спрячь|скрой|"
         r"сюрни|свирни|сверне|зверни|сверли|свёрни|сварни|сверний|сверну")      # Whisper typos (user log: «Сюрни Хром»)
MAX_V = r"разверни|развернуть|разверни ка|раскрой|открой на весь экран|разверне|розверни|развирни"
ACT_V = (r"переключись на|переключи на|переключиться на|переключись в|перейди в|перейди на|перейди к|"
         r"вернись на|вернись в|вернись к|вернусь на|вернусь в|верни на|верни в|вернемся на|вернемся в|вернёмся на|"
         r"вернёмся в|давай обратно на|давай обратно в|обратно на|обратно в|"
         r"покажи окно|активируй|сделай активным|давай в|открой окно")
BACK_TO_PREV = re.compile(r"^(?:ну |давай |а )?(?:вернись|вернись обратно|вернемся|вернёмся|верни обратно|"
                          r"назад в прошлое окно|предыдущее окно|прошлое окно|в прошлое окно)$")
OPENABLE = {"ютуб": "youtube", "ютуб музыка": "ytmusic"}       # «вернись на ютуб» opens it if nothing is open
VERB_RE = re.compile(rf"^(?:(?:ну|давай|джарвис|эй|слушай|а|пожалуйста)\s+)*({ACT_V}|{CLOSE_V}|{MIN_V}|{MAX_V})\b\s*(.*)$")
FILLERS = {
    "ну", "давай", "пожалуйста", "плиз", "быстро", "срочно", "уже", "мне", "ка", "там", "тут", "вот", "же", "то",
    "нахуй", "нахер", "нахрен", "нафиг", "нафик", "наху", "хуй", "хер", "хрен", "фиг", "нах", "на", "к", "черту",
    "чертям", "блять", "бля", "блядь", "блин", "сука", "епта", "ебаный", "ебаное", "ебучий", "ебучее", "гребаный",
    "гребаное", "долбаный", "чертов", "чертово", "этот", "эту", "это", "эти", "этого", "его", "ее", "окно", "окошко", "приложение", "программу", "прогу", "джарвис", "текущее", "активное", "открытое", "мое",
    "мой", "сейчас", "все", "всё", "весь", "экран", "с",
}
NOT_WINDOWS = re.compile(r"^(звук\w*|компьютер|комп|пк|систему|свет\w*|лампу|розетк\w*|будильник\w*|таймер\w*|"
                         r"напоминани\w*|автозагрузк\w*|автозапуск\w*|микрофон\w*|режим\w*|задач\w*|скобк\w*|"
                         r"кавычк\w*|диктовк\w*|перевод\w*|переводчик\w*|телевизор|кондиционер|шторы|чайник)\b")
SELF_RE = re.compile(r"^(себя|джарвис\w*|ассистент\w*|сам\w*)$")
ALL_RE = re.compile(r"^(все|всё)( окна)?$")
TAB_RE = re.compile(r"\bвкладк\w*\b")
SPLIT_RE = re.compile(r"^(закрой|сверни|разверни)(?!те\b|ка\b)([а-я]{1,3})\s+([а-я]{3,})")   # «закройки берпанк»
GAME_RE = re.compile(r"^(?:текущую |эту |мою |открытую )?(?:игру|игрушку|игруху|игра)$")
ALL_MOD = re.compile(r"\b(?:весь|всю|вся|все|всё|полностью|целиком)\b")
ALL_NOISE = {"окна", "окон", "окошки", "вкладки", "вкладок", "вкладку", "его", "её", "ее"}


def plural(n: int, one: str, few: str, many: str) -> str:
    n100, n10 = n % 100, n % 10
    return one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many


def genitive(label: str) -> str:
    """«проводник» -> «проводника», «ютуб» -> «ютуба»; Latin / multi-word names -> «Chrome»."""
    if not re.fullmatch(r"[а-яё]+", label):
        return f"«{label}»"
    if label.endswith(("й", "ь")):
        return label[:-1] + "я"
    if label.endswith("а"):
        return label[:-1] + "ы"
    if label.endswith(("о", "е", "и", "ы", "у", "ю", "я")):
        return label
    return label + "а"


GUARD_MSG = {"game": "Игры не закрываю.", "blocked": "Это окно у тебя в списке «Не закрывать»."}


def _target_words(rest: str) -> str:
    words = [w for w in rest.split() if w not in FILLERS]
    return " ".join(words).strip()


class WindowsSkill(Skill):
    name = "windows"

    def __init__(self, ctx, manager: Optional[wn.WindowManager] = None, start: bool = True,
                 tabs=None, select_tab=None, quitter: Optional[procs.Quitter] = None,
                 steam_game_running=procs.steam_game_running):
        super().__init__(ctx)
        self.wm = manager or wn.WindowManager(wn.default_backend())
        self.quitter = quitter if quitter is not None else (procs.Quitter(own_pids=set(self.wm.own) or None)
                                                            if self.wm.b is not None else None)
        self.steam_game_running = steam_game_running
        from .. import uia
        self.tabs = tabs or uia.browser_tabs
        self.select_tab = select_tab or uia.select_tab
        self._stop = threading.Event()
        if start and self.wm.b is not None:
            threading.Thread(target=self._track_loop, name="jarvis-fgwin", daemon=True).start()

    def _track_loop(self) -> None:
        while not self._stop.wait(0.7):
            try:
                self.wm.track()
            except Exception as e:
                log.debug("track: %s", e)

    # ------------------------------------------------------------ resolution
    def _windows_for(self, name: str) -> tuple[list, str, Optional[wn.Target]]:
        """-> (windows, spoken label for the reply, target)"""
        tgt = wn.resolve_alias(name)
        if tgt is not None:
            return self.wm.find(tgt), tgt.title_ru, tgt
        wins = self.wm.find_by_name(name)
        if not wins:
            launcher = self.ctx.extra.get("launcher")
            if launcher is not None:
                try:
                    e, score = launcher.find(name)
                    exe = str(getattr(e, "target", "") or "").lower() if e else ""
                    if e and score >= 0.6 and exe.endswith(".exe"):
                        base = re.split(r"[\\/]", exe)[-1]
                        wins = [w for w in self.wm.candidates() if w.exe == base]
                    elif e and score >= 0.6 and getattr(e, "is_game", False):
                        gname = games._norm(e.name)              # «киберпанк» -> window «Cyberpunk 2077»
                        first = gname.split()[0] if gname else ""
                        wins = [w for w in self.wm.candidates()
                                if (t := games._norm(w.title)) and (t == gname or t.startswith(gname + " ")
                                                                    or (len(first) >= 5 and t.split()[0] == first))]
                        if wins:
                            return wins, e.name, None
                except Exception as ex:
                    log.debug("launcher find: %s", ex)
        return wins, name, None

    # ------------------------------------------------------------ actions
    # ------------------------------------------------------------ 1.0.9 game guard
    def _noname(self) -> bool:
        return bool(self.ctx.extra.get("noname"))

    def _say(self, text: str) -> ToolResult:
        """Refusals are silent for no-name commands (the user may not have been talking to Jarvis at all)."""
        return ToolResult("" if self._noname() else text)

    def _guard(self, w: wn.Window) -> str:
        """'' | 'blocked' (user's «Не закрывать» list) | 'game'."""
        never = ((self.ctx.cfg.get("windows") or {}).get("never_close") or []) if self.ctx.cfg else []
        if games.blocked(w, never):
            return "blocked"
        why = games.why_game(w, self.wm.b, self.ctx.extra.get("launcher"), games.game_mode_on(self.ctx))
        if why:
            log.info("Не закрываю игру %r (%s): %s", w.title, w.exe, why)
            return "game"
        return ""

    def close_game(self) -> ToolResult:
        """«Джарвис, закрой игру»: only with the name, only after «да»; never taskkill."""
        if self._noname():
            return ToolResult("")
        if self.wm.b is None:
            return ToolResult("Управлять окнами я умею только в Windows.")
        front = self.wm.close_target()
        cands = ([front] if front is not None else []) + [w for w in self.wm.candidates()
                                                          if front is None or w.hwnd != front.hwnd]
        never = (self.ctx.cfg.get("windows") or {}).get("never_close") or [] if self.ctx.cfg else []
        gm = games.game_mode_on(self.ctx)
        launcher = self.ctx.extra.get("launcher")
        game = next((w for w in cands if games.why_game(w, self.wm.b, launcher, gm)), None)
        if game is None:
            return ToolResult("Не вижу открытой игры.")
        if games.blocked(game, never):
            return ToolResult(GUARD_MSG["blocked"])
        return self._confirm_game(game, self._label(game))

    def close(self, name: str, tab: bool = False) -> ToolResult:
        if self.wm.b is None:
            return ToolResult("Управлять окнами я умею только в Windows.")
        name = name.strip()
        if GAME_RE.match(name):
            return self.close_game()
        if SELF_RE.match(name):
            return ToolResult("Себя я так не закрываю. Если хочешь меня выключить — скажи «выход».")
        if ALL_RE.match(name):
            return ToolResult("Все окна разом не закрываю — скажи, какое именно. Могу свернуть все.")
        if tab:
            w = self.wm.active()
            if w is None or not w.is_browser:
                return self._say("Сейчас активно не окно браузера — какую вкладку закрыть?")
            ok = self.wm.b.ctrl_w(w.hwnd) and self.wm._gone(w.hwnd, w.title, tab=True)
            return ToolResult("Закрыл вкладку." if ok else "Не получилось закрыть вкладку.")
        if not name:                                     # bare «закрой» = the WHOLE window in front at speech start
            w = self.wm.close_target()
            if w is None:
                return self._say("Не вижу, какое окно закрыть: назови его, например «закрой хром».")
            g = self._guard(w)
            if g:
                return self._say(GUARD_MSG[g])
            return self._close_one(w, self._label(w), whole=True)
        wins, label, tgt = self._windows_for(name)
        if procs.app_key(name) is not None or not wins:   # tray apps (Steam, Discord, WiFi Mouse…): quit fully
            q = self.quit_app(name, label if tgt is not None else name, wins)
            if q is not None:
                return q
        if not wins:
            if tgt is not None and tgt.key == "ютуб музыка":
                ok = pu.press_media_key("stop")
                return ToolResult("Окна ютуб музыки не вижу — остановил воспроизведение." if ok
                                  else "Не вижу открытого окна ютуб музыки.")
            return ToolResult(f"Не вижу открытого окна «{label}».")
        guards = [self._guard(w) for w in wins]
        keep = [w for w, g in zip(wins, guards) if not g]
        if not keep:
            if guards[0] == "game":                      # the user NAMED the game: explicit, but only with «да»
                return self._confirm_game(wins[0], label)
            return self._say(GUARD_MSG[guards[0]])
        return self._close_one(keep[0], label, more=len(keep) - 1)

    def close_all(self, name: str) -> ToolResult:
        """«закрой весь проводник / все окна хрома / весь ютуб»: every window (or YouTube tab) of that app, gracefully
        (WM_CLOSE, never a kill). «закрой все окна» = all windows of the app in front. Games are never included."""
        if self.wm.b is None:
            return ToolResult("Управлять окнами я умею только в Windows.")
        name = name.strip()
        if GAME_RE.match(name) or SELF_RE.match(name):
            return self.close(name)
        if not name:                                     # all windows of the active app
            front = self.wm.close_target()
            if front is None:
                return self._say("Все окна разом не закрываю — скажи, какие: например «закрой весь проводник».")
            if self._guard(front):
                return self._say(GUARD_MSG[self._guard(front)])
            cls = front.cls if front.exe == "explorer.exe" else None
            wins = [w for w in self.wm.candidates() if w.exe == front.exe and (cls is None or w.cls == cls)]
            label, tgt = self._label(front) if front.exe != "explorer.exe" else "проводник", None
        else:
            wins, label, tgt = self._windows_for(name)
        if tgt is not None and tgt.key == "ютуб":
            return self._close_all_youtube(tgt, wins)
        if not wins:
            if procs.app_key(name) is not None:
                q = self.quit_app(name, label, wins)
                if q is not None:
                    return q
            return ToolResult(f"Не вижу открытых окон «{label}».")
        guards = [self._guard(w) for w in wins]
        keep = [w for w, g in zip(wins, guards) if not g]
        if not keep:
            if guards[0] == "game" and name:
                return self._confirm_game(wins[0], label)
            return self._say(GUARD_MSG[guards[0]])
        if len(keep) == 1:
            return self._close_one(keep[0], label, whole=True, kill=False)
        closed, dialogs, failed = 0, 0, 0
        for w in keep:
            res = self.wm.close(w, whole=True, kill=False)
            log.info("close all %r (%s): %s", w.title, w.exe, res)
            closed += res == "closed"
            dialogs += res == "dialog"
            failed += res not in ("closed", "dialog")
        return ToolResult(self._all_reply(closed, dialogs, failed, f"{plural(closed, 'окно', 'окна', 'окон')} "
                                                                    f"{genitive(label)}"))

    def _all_reply(self, closed: int, dialogs: int, failed: int, what: str) -> str:
        parts = [f"Закрыл {closed} {what}." if closed else "Ничего не закрылось."]
        if dialogs:
            parts.append(f"{dialogs} {plural(dialogs, 'просит', 'просят', 'просят')} сохранить изменения — ответь в окне.")
        if failed:
            parts.append(f"{failed} не {plural(failed, 'закрылось', 'закрылись', 'закрылись')}.")
        text = " ".join(parts)
        return "" if self._noname() and not closed else text

    def _close_all_youtube(self, tgt: wn.Target, wins: list) -> ToolResult:
        """YouTube app windows (PWA) are closed whole; in browsers every YouTube TAB gets Ctrl+W (UIA tab list);
        without UIA only the active YouTube tabs are closed."""
        rx = re.compile(tgt.title_re, re.I) if tgt.title_re else None
        ex = re.compile(tgt.exclude_re, re.I) if tgt.exclude_re else None
        closed = 0
        for w in [w for w in wins if not w.is_browser]:
            closed += self.wm.close(w, whole=True, kill=False) == "closed"
        for w in [w for w in self.wm.candidates() if w.is_browser]:
            for _ in range(40):                          # re-read the tab list after every close (stale elements)
                tabs = [t for t in (self.tabs(w.hwnd) or [])
                        if rx and rx.search(t["name"]) and not (ex and ex.search(t["name"]))]
                if not tabs:
                    break
                if not (self.select_tab(tabs[0]["el"]) and self.wm.b is not None and self.wm.b.ctrl_w(w.hwnd)):
                    break
                self.wm.sleep(0.15)
                closed += 1
            else:
                continue
        if closed == 0:                                  # no UIA tab list: the active YouTube tab of each window
            for w in [w for w in wins if w.is_browser]:
                closed += self.wm.close(w, whole=False, kill=False) in ("tab_closed", "closed")
        if closed == 0:
            return self._say("Не вижу открытого ютуба.")
        return ToolResult(f"Закрыл ютуб: {closed} {plural(closed, 'вкладку', 'вкладки', 'вкладок')}." if closed > 1
                          else "Закрыл вкладку с ютубом.")

    def _confirm_game(self, w: wn.Window, label: str) -> ToolResult:
        if self._noname():
            return ToolResult(f"Игру закрою только по имени: «Джарвис, закрой {label}».", noname_say=True)
        nice = label if any(c.isupper() for c in label) else self._label(w)
        return ToolResult("", confirm=Confirm(
            f"Точно закрыть {nice}? Несохранённый прогресс пропадёт. Да или нет?",
            lambda: self._close_one(w, nice, whole=True, kill=False), "Окей, не закрываю."))

    # ------------------------------------------------------------ 1.0.9 quit apps (also tray-only ones)
    def quit_app(self, name: str, label: str, wins: list) -> Optional[ToolResult]:
        """-> None when no process of that app runs (caller falls back to window logic / «не вижу»)."""
        if self.quitter is None:
            return None
        found = self.quitter.find(name)
        found = [p for p in found if not games.why_game(wn.Window(0, "", p.pid, p.name), path=p.exe or "")]
        if not found:
            return None
        gw = next((w for w in wins if self._guard(w) == "game"), None)
        if gw is not None:                               # a game window under that name -> game rule
            return self._confirm_game(gw, label)
        key = procs.app_key(name)
        nice = {"вайфай маус": "WiFi Mouse", "батлнет": "Battle.net", "гог": "GOG Galaxy", "иа": "EA app",
                "юбисофт": "Ubisoft Connect", "райот": "Riot Client"}.get(key or "", label)

        def go() -> ToolResult:
            status, rest = self.quitter.quit(found)   # type: ignore[union-attr]
            log.info("quit %s: %s %s", nice, status, [p.name for p in rest])
            if status == "quit":
                return ToolResult(f"Закрыл {nice}.")
            if status == "none":
                return ToolResult(f"{nice} уже не запущен.")
            if self._noname():
                return ToolResult("")
            return ToolResult("", confirm=Confirm(
                f"{nice} не закрывается. Убить процесс? Несохранённое пропадёт. Да или нет?",
                lambda: ToolResult(f"Закрыл {nice}." if self.quitter.force(rest)   # type: ignore[union-attr]
                                   else f"Не получилось закрыть {nice}: процесс не завершается."),
                "Окей, оставил."))
        if any(p.name == "steam.exe" for p in found) and self.steam_game_running():
            if self._noname():
                return ToolResult("")
            return ToolResult("", confirm=Confirm(
                "Сейчас запущена игра из Steam — она закроется вместе с ним. Закрыть Steam? Да или нет?",
                go, "Окей, Steam не трогаю."))
        return go()

    @staticmethod
    def _label(w: wn.Window) -> str:
        t = re.sub(wn.BROWSER_SUFFIX, "", w.title).strip()
        t = t.rsplit(" - ", 1)[-1] if " - " in t else t
        return t[:40] or "окно"

    def _close_one(self, w: wn.Window, label: str, more: int = 0, whole: bool = False,
                   kill: bool = True) -> ToolResult:
        res = self.wm.close(w, whole=whole, kill=kill)
        log.info("close %r (%s pid %s): %s", w.title, w.exe, w.pid, res)
        tail = f" Открыто ещё {more} — повтори, если закрыть и их." if more > 0 else ""
        if res == "closed":
            return ToolResult(f"Закрыл {label}.{tail}")
        if res == "tab_closed":
            return ToolResult(f"Закрыл вкладку с {label}ом." if label == "ютуб" else "Закрыл вкладку.")
        if res == "dialog":                              # never force-killed: the user decides in the window
            return self._say(f"{label[:1].upper() + label[1:]} просит сохранить изменения — ответь в окне.")
        if res == "protected":
            return self._say("Это системное окно, его я не трогаю.")
        if not kill:
            return ToolResult(f"{label[:1].upper() + label[1:]} не закрывается — выйди через меню игры.")
        return self._say(f"Не получилось закрыть {label}: окно не закрывается.")

    def show(self, name: str, cmd: str) -> ToolResult:
        if self.wm.b is None:
            return ToolResult("Управлять окнами я умею только в Windows.")
        if cmd == "minimize" and ALL_RE.match(name.strip()):
            return ToolResult("Свернул все окна." if self.wm.b.show_desktop() else "Не получилось свернуть окна.")
        if name:
            wins, label, _ = self._windows_for(name)
            w = wins[0] if wins else None
        else:
            w = self.wm.active()
            label = self._label(w) if w else "окно"
        if w is None:
            return ToolResult(f"Не вижу открытого окна «{label}».")
        ok = self.wm.show(w, cmd)
        verb = {"minimize": "Свернул", "maximize": "Развернул", "restore": "Вернул"}[cmd]
        return ToolResult(f"{verb} {label}." if ok else f"Не получилось: окно {label} не поддаётся.")

    def switch(self, name: str) -> Optional[ToolResult]:
        """«переключись на хром», «вернись на ютуб»: an open window -> front (restored if minimized); else a
        background browser TAB whose title matches (UIA); else known sites (ютуб, ютуб музыка) are opened.
        Nothing found -> None, so «перейди в телеграм» can still be handled by the apps skill (launch)."""
        if self.wm.b is None:
            return ToolResult("Управлять окнами я умею только в Windows.")
        wins, label, tgt = self._windows_for(name)
        if wins:
            w = wins[0]
            self.wm.show(w, "restore")
            ok = self.wm.b.activate(w.hwnd)
            return ToolResult(f"Переключился на {label}." if ok else f"Не получилось переключиться на {label}.")
        rx = re.compile(tgt.title_re, re.I) if tgt is not None and tgt.title_re else None
        words = [w for w in (name or "").lower().split() if len(w) >= 3]
        for w in self.wm.candidates():
            if not w.is_browser:
                continue
            for tab in self.tabs(w.hwnd):
                tname = tab["name"]
                if (rx and rx.search(tname) and not (tgt is not None and tgt.exclude_re and re.search(tgt.exclude_re, tname, re.I))) \
                        or (rx is None and words and all(x in tname.lower() for x in words)):
                    self.wm.show(w, "restore")
                    ok = self.select_tab(tab["el"]) and self.wm.b.activate(w.hwnd)
                    return ToolResult(f"Переключился на вкладку {label}." if ok else
                                      f"Не получилось переключиться на {label}.")
        media = self.ctx.extra.get("media")
        if tgt is not None and tgt.key in OPENABLE and media is not None:
            fn = media.open_youtube if OPENABLE[tgt.key] == "youtube" else media.open_ytmusic
            return ToolResult(f"Открываю {tgt.title_ru}.", then=lambda: (fn(), None)[1])
        return None

    def back_to_previous(self) -> ToolResult:
        if self.wm.b is None:
            return ToolResult("Управлять окнами я умею только в Windows.")
        w = self.wm.previous()
        if w is None:
            return ToolResult("Не помню, какое окно было до этого.")
        self.wm.show(w, "restore")
        ok = self.wm.b.activate(w.hwnd)
        return ToolResult(f"Вернулся в {self._label(w)}." if ok else "Не получилось переключиться.")

    # ------------------------------------------------------------ voice
    def match(self, text: str, t: str) -> Optional[ToolResult]:
        t = t.replace("закрой-ка", "закрой ка")
        t = SPLIT_RE.sub(r"\1 \2\3", t)                  # Whisper: «закройки берпанк» -> «закрой киберпанк»
        if BACK_TO_PREV.match(t):
            return self.back_to_previous()
        m = VERB_RE.match(t)
        if not m:
            return None
        verb, rest = m.group(1), m.group(2).strip()
        if NOT_WINDOWS.match(rest):
            return None                                  # «выключи звук/компьютер/свет» — other skills
        if verb.startswith(("выключ", "выруб")) and re.match(
                r"^(эт\w* )?(музык\w*|видео|видос\w*|ролик\w*|песн\w*|трек\w*|воспроизведени\w*)$", rest):
            from .. import uia
            if uia.media_playing():                      # sound is playing -> pause it (media skill), reversible;
                return None                              # idle / unknown -> 1.0.4 behaviour: close the YT Music app
        if re.fullmatch(ACT_V, verb):
            return self.switch(_target_words(rest)) if rest else None
        is_min, is_max = bool(re.match(MIN_V, verb)), bool(re.match(MAX_V, verb))
        if is_max and re.match(r"^(все|всё)\b", rest):
            return None
        if is_min and re.match(r"^(все|всё)( окна)?$", rest):
            return self.show("все", "minimize")
        core = " ".join(w for w in rest.split() if w not in FILLERS - {"джарвис"})
        if SELF_RE.match(core) and not (is_min or is_max):
            return self.close(core)
        tab = bool(TAB_RE.search(rest)) and not is_min and not is_max
        target = _target_words(TAB_RE.sub(" ", rest))
        soft = verb.startswith(("выключ", "выруб", "убери", "убей", "прибей", "грохни"))
        if soft and (not target or wn.resolve_alias(target) is None) and not self.wm.find_by_name(target):
            return None                                  # «выключи <что-то не окно>» — not ours
        if is_min:
            return self.show(target, "minimize")
        if is_max:
            return self.show(target, "maximize")
        if ALL_MOD.search(rest):                       # 1.0.9: «закрой весь проводник», «закрой все окна хрома»
            return self.close_all(" ".join(w for w in target.split() if w not in ALL_NOISE and not ALL_MOD.fullmatch(w)))
        return self.close(target, tab=tab)

    def tools(self):
        return [
            fn("close_window", "Закрыть окно программы или сайта (ютуб, хром, стим…).",
               {"target": s("что закрыть; пусто = активное окно")}),
            fn("window_action", "Свернуть или развернуть окно.",
               {"action": s("", ["minimize", "maximize"]), "target": s("пусто = активное, «все» = все")}, ["action"]),
        ]

    def call(self, name: str, args: dict) -> Optional[ToolResult]:
        target = str(args.get("target") or "").strip().lower()
        target = _target_words(target) if target not in ("все", "всё") else target
        if name == "close_window":
            return self.close(target)
        if name == "window_action":
            return self.show(target, "maximize" if args.get("action") == "maximize" else "minimize")
        return None
