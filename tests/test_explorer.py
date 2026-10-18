"""1.0.7: ordinal open in Windows Explorer."""
import types

import pytest

from jarvis import explorer as E
from jarvis.router import normalize
from jarvis.skills.explorer_skill import ExplorerSkill
from jarvis.windows import Window

EXP = "CabinetWClass"


@pytest.mark.parametrize("text,kind,n,explicit", [
    ("открой второй файл", "file", 2, True), ("открой третью папку", "folder", 3, True),
    ("открой второе", "any", 2, False), ("открой 5", "any", 5, False), ("открой файл номер 3", "file", 3, True),
    ("открой файл номер двенадцать", "file", 12, True), ("открой пятнадцатую папку", "folder", 15, True),
    ("зайди в первую папку", "folder", 1, True), ("открой последний файл", "file", -1, True),
    ("открой 20-й файл", "file", 20, True), ("Открой второй документ.", "file", 2, True),
    ("второй файл", "file", 2, True), ("назад", "back", 0, False), ("вверх", "up", 0, False),
    ("на уровень выше", "up", 0, False),
])
def test_parse(text, kind, n, explicit):
    p = E.parse(normalize(text))
    assert p is not None and (p.kind, p.n) == (kind, n) and (kind in ("back", "up") or p.explicit == explicit)


@pytest.mark.parametrize("text", ["открой второе видео", "открой хром", "второе", "открой файл отчёт",
                                  "включи второй канал", "открой папку загрузки"])
def test_not_ours(text):
    assert E.parse(normalize(text)) is None


ITEMS = [  # Shell order (by name) differs from the view (user sorted by date)
    {"name": "Архив", "path": r"C:\u\Архив", "folder": True},
    {"name": "Фото", "path": r"C:\u\Фото", "folder": True},
    {"name": "отчёт", "path": r"C:\u\отчёт.docx", "folder": False},       # extension hidden in the view
    {"name": "смета", "path": r"C:\u\смета.xlsx", "folder": False},
    {"name": "Музыка", "path": r"C:\u\Музыка", "folder": True},
]
VIEW = ["Фото", "смета", "Музыка", "отчёт", "Архив"]


class Sh:
    def __init__(self, view=VIEW):
        self.view, self.cur, self.opened, self.history = view, r"C:\u", [], []

    def items(self, hwnd, title=""):
        return ITEMS

    def view_names(self, hwnd):
        return self.view

    def folder(self, hwnd, title=""):
        return self.cur

    def navigate(self, hwnd, path, title=""):
        self.history.append(self.cur)
        self.cur = path
        return True

    def back(self, hwnd, title=""):
        if not self.history:
            return False
        self.cur = self.history.pop()
        return True

    def open_file(self, path):
        self.opened.append(path)
        return True


class B:
    def __init__(self, wins, fg):
        self.wins, self.fg = wins, fg

    def windows(self):
        return self.wins

    def foreground(self):
        return self.fg


def setup(wins, fg, sh=None):
    sh = sh or Sh()
    b = B(wins, fg)
    wm = types.SimpleNamespace(b=b, active=lambda: next((w for w in wins if w.hwnd == b.fg), None))
    ex = E.Explorer(wm, backend=sh, sleep=lambda s: None)
    sk = ExplorerSkill(types.SimpleNamespace(cfg={}, extra={}), explorer=ex)
    return sk, sh


EXPW = Window(10, "u", 5, "explorer.exe", EXP)
YT = Window(20, "Котики - YouTube - Google Chrome", 6, "chrome.exe", "Chrome_WidgetWin_1")


def run(sk, text):
    r = sk.match(text, normalize(text))
    return None if r is None else (r.then() if r.then else r.reply)


def test_visible_order_and_type_filters():
    sk, sh = setup([EXPW], 10)
    assert run(sk, "открой второй файл") == "Открываю отчёт." and sh.opened == [r"C:\u\отчёт.docx"]
    assert run(sk, "открой первый файл") == "Открываю смета."
    assert run(sk, "открой вторую папку") == "Открыл папку Музыка." and sh.cur == r"C:\u\Музыка"
    assert run(sk, "назад") == "Назад: u."
    assert run(sk, "открой второе") == "Открываю смета."              # bare: all items, view order
    assert run(sk, "открой последнюю папку") == "Открыл папку Архив."
    assert run(sk, "вверх") == "Поднялся в u."
    assert run(sk, "открой пятый файл") == "Здесь всего 2 файлов."


def test_shell_order_when_uia_unavailable():
    sk, sh = setup([EXPW], 10, Sh(view=[]))
    assert run(sk, "открой первую папку") == "Открыл папку Архив."


def test_bare_ordinal_context_priority():
    sk, _ = setup([YT, EXPW], 20)                       # YouTube in front -> media skill
    assert sk.match("открой второе", "открой второе") is None
    sk, _ = setup([EXPW, YT], 10)                       # Explorer in front -> explorer
    assert run(sk, "открой второе") == "Открываю смета."
    other = Window(30, "Блокнот", 7, "notepad.exe", "Notepad")
    sk, _ = setup([other, EXPW, YT], 30)                # neither in front: most recent in z-order (Explorer)
    assert run(sk, "открой второе") == "Открываю смета."
    sk, _ = setup([other, YT, EXPW], 30)
    assert sk.match("открой второе", "открой второе") is None
    sk, _ = setup([YT, EXPW], 20)                       # noun said: Explorer even if YouTube is in front
    assert run(sk, "открой второй файл") == "Открываю отчёт."
    assert sk.match("назад", "назад") is None           # «назад» only when Explorer is in front


def test_no_explorer_honest():
    sk, _ = setup([YT], 20)
    assert run(sk, "открой второй файл") == "Не вижу открытого проводника."
