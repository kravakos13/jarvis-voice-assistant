"""1.0.7: data-driven hotkey commands (commands.yaml), browser scope, ordinal tab/link, help list."""
import types

import pytest

from jarvis import keyinput, uia
from jarvis.router import normalize
from jarvis.skills import hotkeys_skill as H
from jarvis.windows import Window

CHROME = Window(5, "Котики - Google Поиск - Google Chrome", 9, "chrome.exe")
FIREFOX = Window(6, "Котики — Mozilla Firefox", 10, "firefox.exe")
NOTEPAD = Window(7, "Безымянный - Блокнот", 11, "notepad.exe")


def make(win, links=None):
    keys, opened, clicked = [], [], []
    b = types.SimpleNamespace(foreground=lambda: win.hwnd if win else 0, activate=lambda h: True)
    wm = types.SimpleNamespace(b=b, active=lambda: win)
    ctx = types.SimpleNamespace(cfg={}, extra={"wm": wm})
    sk = H.HotkeysSkill(ctx, press=lambda c: keys.append(c) or True, opener=opened.append,
                        links=lambda h: links or [], invoke=lambda el: clicked.append(el) or True)
    return sk, keys, opened, clicked


def run(sk, text):
    r = sk.match(text, normalize(text))
    return None if r is None else (r.then() if r.then else r.reply)


@pytest.mark.parametrize("phrase,combo", [
    ("новая вкладка", "ctrl+t"), ("Открой новую вкладку.", "ctrl+t"), ("верни вкладку", "ctrl+shift+t"),
    ("следующая вкладка", "ctrl+tab"), ("предыдущая вкладка", "ctrl+shift+tab"), ("вторая вкладка", "ctrl+2"),
    ("перейди на пятую вкладку", "ctrl+5"), ("последняя вкладка", "ctrl+9"),
    ("обнови", "f5"), ("листай вниз", "pagedown"), ("листай вверх", "pageup"),
    ("в начало", "ctrl+home"), ("в конец", "ctrl+end"), ("на весь экран", "f11"), ("инкогнито", "ctrl+shift+n"),
    ("увеличь масштаб", "ctrl+plus"), ("уменьши масштаб", "ctrl+minus"), ("копируй", "ctrl+c"),
    ("вставь", "ctrl+v"), ("выдели всё", "ctrl+a"), ("отмени", "ctrl+z"), ("сохрани", "ctrl+s"),
    ("покажи рабочий стол", "win+d"), ("ну давай обнови страницу пожалуйста", "f5"),
])
def test_browser_hotkeys(monkeypatch, phrase, combo):
    monkeypatch.setattr(keyinput, "focus_target", lambda wm: 5)
    sk, keys, _, _ = make(CHROME)
    assert run(sk, phrase) == "" and keys == [combo], phrase


def test_firefox_incognito_and_browser_scope(monkeypatch):
    monkeypatch.setattr(keyinput, "focus_target", lambda wm: 6)
    sk, keys, _, _ = make(FIREFOX)
    run(sk, "инкогнито")
    assert keys == ["ctrl+shift+p"]
    sk, keys, _, _ = make(NOTEPAD)                       # browser-only commands are not ours in Notepad
    for ph in ("назад", "обнови", "новая вкладка", "вторая вкладка", "инкогнито"):
        assert sk.match(ph, normalize(ph)) is None, ph
    assert run(sk, "сохрани") == "" and keys == ["ctrl+s"]   # generic ones work anywhere


def test_nothing_without_a_target_window():
    sk, keys, _, _ = make(None)
    assert sk.match("копируй", "копируй") is None and keys == []


def test_open_site_and_ninth_tab_limit(monkeypatch):
    monkeypatch.setattr(keyinput, "focus_target", lambda wm: 5)
    sk, keys, opened, _ = make(CHROME)
    assert run(sk, "открой сайт хабр") == "Открываю хабр." and "ducky" in opened[-1]
    run(sk, "открой сайт ru.wikipedia.org")
    assert opened[-1] == "https://ru.wikipedia.org"
    keys.clear()
    run(sk, "двенадцатая вкладка")                      # 1.0.9: >8 = Ctrl+1, then Ctrl+Tab ×11
    assert keys == ["ctrl+1"] + ["ctrl+tab"] * 11


GOOGLE = [
    {"href": "https://www.google.com/search?q=x&tbm=isch", "name": "Картинки", "rect": (100, 150, 180, 170)},
    {"href": "https://www.googleadservices.com/pagead/aclk?x", "name": "Реклама · Котокорм", "rect": (100, 200, 700, 230)},
    {"href": "https://ru.wikipedia.org/wiki/Кошка", "name": "Кошка — Википедия ru.wikipedia.org", "rect": (100, 300, 700, 330), "el": "wiki"},
    {"href": "https://ru.wikipedia.org/wiki/Кошка", "name": "Кошка", "rect": (100, 340, 300, 360), "el": "dup"},
    {"href": "https://kotiki.ru/", "name": "Котики — фото и видео kotiki.ru", "rect": (100, 420, 700, 450), "el": "kot"},
    {"href": "https://www.youtube.com/watch?v=abc", "name": "Котики 10 часов — YouTube", "rect": (100, 520, 700, 550), "el": "yt"},
]


def test_result_links_order_and_click(monkeypatch):
    items = uia.result_links(GOOGLE)
    assert [i["el"] for i in items] == ["wiki", "kot", "yt"]
    sk, _, _, clicked = make(CHROME, links=items)
    assert run(sk, "открой вторую ссылку") == "Открываю: Котики — фото и видео kotiki.ru."
    assert clicked == ["kot"]
    assert run(sk, "открой последнюю ссылку").startswith("Открываю: Котики 10 часов")
    assert run(sk, "открой пятую ссылку") == "На экране всего 3 ссылок."


def test_user_commands_file_overrides_and_extends(tmp_path):
    f = tmp_path / "commands.yaml"
    f.write_text('- {id: reload, scope: browser, phrases: ["перезагрузи"], keys: ctrl+shift+r, reply: ""}\n'
                 '- {id: mine, section: Мои, scope: any, phrases: ["скажи привет телеграму"], keys: ctrl+k, example: x}\n'
                 '- {id: broken}\n', encoding="utf-8")
    cmds = {c["id"]: c for c in H.load_commands(extra=f)}
    assert cmds["reload"]["keys"] == "ctrl+shift+r" and "mine" in cmds and "broken" not in cmds
    from jarvis.helpinfo import sections
    assert ("Мои", ["x"]) in sections(list(cmds.values()))


def test_help_voice_and_settings():
    from jarvis.helpinfo import as_html
    html = as_html()
    for p in ("открой второй файл", "напиши", "новая вкладка", "Перезапустить прослушку", "включи второе"):
        assert p in html, p
    from jarvis.skills.core import CoreSkill
    opened = []
    sk = CoreSkill.__new__(CoreSkill)
    sk.ctx = types.SimpleNamespace(extra={"open_settings": opened.append}, cfg={})
    r = sk.match("что ты умеешь", "что ты умеешь")
    assert "Что я умею" in r.reply and opened == ["help"]
