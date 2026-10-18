"""Every example phrase in the manual's command tables (jarvis/manual.py -> docs/ИНСТРУКЦИЯ.md) is routed to the
module the manual names, on a realistic fake desktop; the generated tables in the .md are up to date."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
import requests

from jarvis import keyinput, manual
from jarvis import windows as wn
from tests.test_explorer import Sh
from tests.test_windows import JARVIS_PID, FakeBackend, W

ROOT = Path(__file__).resolve().parent.parent


def _offline(*a, **k):
    raise requests.ConnectionError("offline (test)")


@pytest.fixture(scope="module")
def router():
    import subprocess
    import webbrowser

    from jarvis.config import load_config
    from jarvis.router import Router
    from jarvis.skills.base import SkillContext
    mp = pytest.MonkeyPatch()
    mp.setattr(requests.Session, "request", _offline)
    mp.setattr(webbrowser, "open", lambda *a, **k: True)
    mp.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("no spawn in tests")))
    mp.setattr(keyinput, "press", lambda *a, **k: True)
    mp.setattr(keyinput, "type_text", lambda *a, **k: True)
    mp.setattr(keyinput, "focus_target", lambda wm, settle_s=0.15: 5)
    logging.disable(logging.CRITICAL)
    cfg = load_config(use_local=False)
    r = Router(SkillContext(cfg=cfg, llm=None, say=lambda *a, **k: None, notify=lambda *a: None, project_root=ROOT))
    apps = r.by_name["apps"]
    mp.setattr(apps.launcher, "find", lambda name: (None, 0.0) if "стим" not in name else
               (type("E", (), {"name": "Steam"})(), 1.0))
    mp.setattr(apps.launcher, "launch", lambda e: None)
    yield r
    logging.disable(logging.NOTSET)
    mp.undo()


DESKTOPS = {
    "Проводник": [W(5, "Загрузки", 9, "explorer.exe", "CabinetWClass")],
    "Перемотка": [W(5, "Котики - YouTube - Google Chrome", 7, "chrome.exe")],
}
DEFAULT = [W(5, "Поиск - Google Chrome", 7, "chrome.exe"), W(6, "YouTube", 8, "chrome.exe"),
           W(8, "Telegram", 9, "telegram.exe", "Qt51517QWindowIcon")]


def _desktop(r, section):
    wins = [W(1, "Джарвис", JARVIS_PID, "pythonw.exe", "Qt")] + DESKTOPS.get(section, DEFAULT)
    b = FakeBackend(wins, fg=5)
    wm = wn.WindowManager(b, own_pids={JARVIS_PID}, sleep=lambda s: None, verify_s=0.0)
    wm.track()
    r.by_name["windows"].wm = wm
    r.ctx.extra["wm"] = wm
    ex = r.by_name["explorer"]
    from jarvis.explorer import Explorer
    ex._ex = Explorer(wm, backend=Sh(), sleep=lambda s: None)
    for s in r.skills:                                   # no modal leftovers between phrases
        if s.name in ("chat", "dictate") and getattr(s, "active", False):
            s.active = False
    r.pending_confirm = r.pending_expect = None


@pytest.mark.parametrize("section,phrase,skill", [(c[0], c[1], c[2]) for c in manual.COMMANDS])
def test_manual_phrase_routes_to_documented_module(router, section, phrase, skill):
    _desktop(router, section)
    res = router.handle(phrase)
    got = (router.last_skill or "?") if res is not None else "llm"
    assert got == skill, f"«{phrase}»: в инструкции {skill}, на деле {got} ({getattr(res, 'reply', '')!r})"


@pytest.mark.parametrize("phrase", [c[1] for c in manual.COMMANDS if c[3]])
def test_manual_no_name_column_is_true(phrase):
    from jarvis import noname
    assert noname.decide(phrase, {}, (-0.2, 0.05), True) is not None, phrase


@pytest.mark.parametrize("phrase", [c[1] for c in manual.COMMANDS if not c[3]])
def test_manual_name_only_column_is_true(phrase):
    from jarvis import noname
    assert noname.decide(phrase, {}, (-0.2, 0.05), True) is None, phrase


def test_generated_tables_are_current_and_html_renders(tmp_path):
    md = (ROOT / "docs" / "ИНСТРУКЦИЯ.md").read_text(encoding="utf-8")
    a, b = md.index(manual.BEGIN) + len(manual.BEGIN), md.index(manual.END)
    assert md[a:b].strip() == manual.tables_md().strip(), "запусти: python -m jarvis.manual --write"
    out = manual.build_html(dest=tmp_path / "m.html")
    h = out.read_text(encoding="utf-8")
    assert "<table>" in h and "нажми энтер" in h and "<h2" in h
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/ИНСТРУКЦИЯ.md" in readme.split("\n\n")[0] + readme[:600]


def _yaml_examples():
    from jarvis.skills.hotkeys_skill import load_commands
    return [c["example"] for c in load_commands() if c.get("example")]


@pytest.mark.parametrize("phrase", _yaml_examples())
def test_commands_yaml_examples_route(router, phrase):
    """Generated «горячие клавиши» table: every example does something (hotkeys, or windows for close/reopen tab)."""
    _desktop(router, "Браузер")
    res = router.handle(phrase)
    assert res is not None and router.last_skill in ("hotkeys", "windows", "view"), (phrase, router.last_skill)


def test_telegram_rows_match_telegram_skill():
    """Telegram is off by default (not loaded), so its rows are checked against the skill's own patterns."""
    from jarvis.skills import telegram_skill as tg
    send, read = manual.TELEGRAM[0][0], manual.TELEGRAM[1][0]
    m = tg.SEND_RE.match(send)
    assert m and m.group(1).startswith("маме") and "буду через час" in m.group(2)
    assert (m := tg.FROM_RE.search(read)) and "саши" in m.group(1).lower()


def test_toc_links_resolve():
    import re
    h = manual.to_html((ROOT / "docs" / "ИНСТРУКЦИЯ.md").read_text(encoding="utf-8"))
    ids = set(re.findall(r'id="([^"]+)"', h))
    links = re.findall(r'href="#([^"]+)"', h)
    assert len(links) >= 12 and not [x for x in links if x not in ids]


@pytest.mark.parametrize("phrase,skill", [("найди в гугле погода в москве", "core"), ("загугли погода в москве", "core"),
                                          ("найди в хроме погоду на завтра", "core"), ("какая погода", "weather"),
                                          ("найди в интернете погоду в москве", "weather"), ("погода в москве", "weather")])
def test_109_explicit_browser_search_beats_weather(router, phrase, skill):
    _desktop(router, "Интернет")
    res = router.handle(phrase)
    assert res is not None and router.last_skill == skill, (phrase, router.last_skill)
