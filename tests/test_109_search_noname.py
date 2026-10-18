"""1.0.9: web / YouTube search without the name — only with an explicit marker and a real query."""
from __future__ import annotations

import types

import pytest

from jarvis import fuzzy, noname
from jarvis.skills.core import SEARCH_RE

CONF = (-0.2, 0.05)


@pytest.mark.parametrize("phrase,cmd", [
    ("найди в хроме рецепт борща", "загугли рецепт борща"),
    ("Найди в Chrome рецепт борща", "загугли рецепт борща"),
    ("Нашёл в гугле погоду в Киеве на выходные", "загугли погоду в киеве на выходные"),
    ("загугли курс доллара", "загугли курс доллара"),
    ("погугли что такое квазар", "загугли что такое квазар"),
    ("найди в гугле про чёрные дыры", "загугли черные дыры"),
    ("поищи в интернете как варить рис", "загугли как варить рис"),
    ("ищи в браузере билеты на поезд Киев Львов", "загугли билеты на поезд киев львов"),
    ("найди в яндексе ремонт телефонов", "загугли ремонт телефонов"),
    ("найди на ютубе обзор RTX 5070", "найди на ютубе обзор rtx 5070"),
    ("поищи на youtube как собрать компьютер", "найди на ютубе как собрать компьютер"),
    ("найди в гугле лучшие игры 2026 года для слабого компьютера с видеокартой", None),
])
def test_noname_search(phrase, cmd):
    out = noname.decide(phrase, {}, CONF, True)
    if cmd is None:                         # long queries pass the short-command limit: still a search
        assert out is not None and out.startswith("загугли лучшие игры")
    else:
        assert out == cmd


@pytest.mark.parametrize("phrase", ["найди ключи", "найди в хроме", "загугли", "найди в гугле .",
                                    "и тогда он сказал найди в гугле котиков", "я нашёл это в интернете вчера",
                                    "ищи", "найди мне что-нибудь"])
def test_no_marker_or_query_is_ignored(phrase):
    assert noname.decide(phrase, {}, CONF, True) is None


def test_search_needs_confident_stt_and_has_own_toggle():
    assert noname.decide("найди в хроме рецепт борща", {}, (-1.2, 0.05), True) is None
    cfg = {"noname": {"groups": {"search": False}}}          # = cfg["assistant"] passed by the assistant
    assert noname.decide("найди в хроме рецепт борща", cfg, CONF, True) is None
    assert "search" in noname.GROUP_IDS


def test_canonical_commands_route_to_browser_search_and_youtube():
    assert SEARCH_RE.match("загугли рецепт борща").group(1) == "рецепт борща"
    assert SEARCH_RE.match("найди в хроме рецепт борща").group(1) == "рецепт борща"
    from jarvis.skills.media_skill import MediaSkill
    sk = MediaSkill.__new__(MediaSkill)
    sk.ctx = types.SimpleNamespace(cfg={}, extra={}, llm=None)
    sk.media = types.SimpleNamespace(youtube_search=lambda q: None)
    r = sk.match("найди на ютубе обзор", "найди на ютубе обзор")
    assert r is not None and "Ищу на ютубе: обзор" in r.reply


def test_web_skill_leaves_browser_markers_to_core():
    from jarvis.skills.web_skill import WebSkill
    sk = WebSkill.__new__(WebSkill)
    for p in ("найди в хроме рецепт борща", "найди в браузере билеты", "загугли котиков"):
        assert sk.match(p, p) is None


def test_fuzzy_never_turns_keys_into_play():
    assert list(fuzzy.variants("найди ключи")) == []
    assert "открой третью вкладку" in list(fuzzy.variants("третью вкладку открой"))


