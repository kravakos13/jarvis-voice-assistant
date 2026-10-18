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
from .base import Skill, ToolResult, fn, s

log = logging.getLogger(__name__)

CLOSE_V = (r"закрой|закрои|закрою|закройте|закрывай|закрыть|закрой ка|выруби|вырубай|вырубить|вырубите|выключи|"
           r"выключай|убей|прибей|грохни|убери")
MIN_V = r"сверни|свернуть|сверни ка|сверните|спрячь|скрой"
MAX_V = r"разверни|развернуть|разверни ка|раскрой|открой на весь экран"
VERB_RE = re.compile(rf"^(?:(?:ну|давай|джарвис|эй|слушай|а|пожалуйста)\s+)*({CLOSE_V}|{MIN_V}|{MAX_V})\b\s*(.*)$")
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


def _target_words(rest: str) -> str:
    words = [w for w in rest.split() if w not in FILLERS]
    return " ".join(words).strip()


class WindowsSkill(Skill):
    name = "windows"

    def __init__(self, ctx, manager: Optional[wn.WindowManager] = None, start: bool = True):
        super().__init__(ctx)
        self.wm = manager or wn.WindowManager(wn.default_backend())
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
                except Exception as ex:
                    log.debug("launcher find: %s", ex)
        return wins, name, None

    # ------------------------------------------------------------ actions
    def close(self, name: str, tab: bool = False) -> ToolResult:
        if self.wm.b is None:
            return ToolResult("Управлять окнами я умею только в Windows.")
        name = name.strip()
        if SELF_RE.match(name):
            return ToolResult("Себя я так не закрываю. Если хочешь меня выключить — скажи «выход».")
        if ALL_RE.match(name):
            return ToolResult("Все окна разом не закрываю — скажи, какое именно. Могу свернуть все.")
        if tab or not name:
            w = self.wm.active()
            if w is None:
                return ToolResult("Не понял, какое окно закрыть: назови его, например «закрой ютуб».")
            if tab:
                if not w.is_browser:
                    return ToolResult("Сейчас активно не окно браузера — какую вкладку закрыть?")
                ok = self.wm.b.ctrl_w(w.hwnd) and self.wm._gone(w.hwnd, w.title, tab=True)
                return ToolResult("Закрыл вкладку." if ok else "Не получилось закрыть вкладку.")
            return self._close_one(w, self._label(w))
        wins, label, tgt = self._windows_for(name)
        if not wins:
            if tgt is not None and tgt.key == "ютуб музыка":
                ok = pu.press_media_key("stop")
                return ToolResult("Окна ютуб музыки не вижу — остановил воспроизведение." if ok
                                  else "Не вижу открытого окна ютуб музыки.")
            return ToolResult(f"Не вижу открытого окна «{label}».")
        return self._close_one(wins[0], label, more=len(wins) - 1)

    @staticmethod
    def _label(w: wn.Window) -> str:
        t = re.sub(wn.BROWSER_SUFFIX, "", w.title).strip()
        t = t.rsplit(" - ", 1)[-1] if " - " in t else t
        return t[:40] or "окно"

    def _close_one(self, w: wn.Window, label: str, more: int = 0) -> ToolResult:
        res = self.wm.close(w)
        log.info("close %r (%s pid %s): %s", w.title, w.exe, w.pid, res)
        tail = f" Открыто ещё {more} — повтори, если закрыть и их." if more > 0 else ""
        if res == "closed":
            return ToolResult(f"Закрыл {label}.{tail}")
        if res == "tab_closed":
            return ToolResult(f"Закрыл вкладку с {label}ом." if label == "ютуб" else "Закрыл вкладку.")
        if res == "dialog":
            return ToolResult(f"{label[:1].upper() + label[1:]} спрашивает, сохранить ли изменения — ответь в окне.")
        if res == "protected":
            return ToolResult("Это системное окно, его я не трогаю.")
        return ToolResult(f"Не получилось закрыть {label}: окно не закрывается.")

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

    # ------------------------------------------------------------ voice
    def match(self, text: str, t: str) -> Optional[ToolResult]:
        t = t.replace("закрой-ка", "закрой ка")
        m = VERB_RE.match(t)
        if not m:
            return None
        verb, rest = m.group(1), m.group(2).strip()
        if NOT_WINDOWS.match(rest):
            return None                                  # «выключи звук/компьютер/свет» — other skills
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
        if re.match(r"^(все|всё)( окна)?$", rest):
            return self.close("все")
        return self.close(target, tab=tab)

    def tools(self):
        return [
            fn("close_window", "Закрыть открытое окно программы/сайта на компьютере (YouTube, YouTube Music, Chrome, "
               "Steam, блокнот и т.п.). Пустой target = текущее активное окно.",
               {"target": s("что закрыть, например «ютуб», «хром», «стим»; пусто = активное окно")}),
            fn("window_action", "Свернуть или развернуть окно.",
               {"action": s("действие", ["minimize", "maximize"]),
                "target": s("какое окно; пусто = активное, «все» = свернуть все")}, ["action"]),
        ]

    def call(self, name: str, args: dict) -> Optional[ToolResult]:
        target = str(args.get("target") or "").strip().lower()
        target = _target_words(target) if target not in ("все", "всё") else target
        if name == "close_window":
            return self.close(target)
        if name == "window_action":
            return self.show(target, "maximize" if args.get("action") == "maximize" else "minimize")
        return None
