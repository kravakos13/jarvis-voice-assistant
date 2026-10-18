"""1.0.7: «включи второе видео» — phrase parsing, YouTube list parsing (fixtures), picking with verification."""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from jarvis import ytpick as Y
from jarvis.wake import normalize
from jarvis.windows import Window

FX = Path(__file__).parent / "fixtures"
SEARCH_HTML = (FX / "yt_search.html").read_text(encoding="utf-8")
SEARCH_URL = "https://www.youtube.com/results?search_query=%D0%BA%D0%BE%D1%82%D0%B8%D0%BA%D0%B8"


def p(text):
    return Y.parse_pick(normalize(text))


# ---------------------------------------------------------------- phrases
@pytest.mark.parametrize("text,n", [
    ("включи второе видео", 2), ("Включи 2 видео", 2), ("включи 2-е видео", 2), ("включи 2е видео", 2),
    ("включи видео номер 2", 2), ("номер три", 3), ("номер 4", 4), ("давай пятое", 5), ("включи первое", 1),
    ("поставь последнее видео", -1), ("включи крайнее видео", -1), ("включи предпоследний ролик", -2),
    ("открой третий ролик", 3), ("четвёртый ролик включи", 4), ("включи десятое видео сверху", 10),
    ("включи втарое видео", 2), ("включи третие видео", 3), ("включи питое видео", 5), ("включи шестое видео", 6),
    ("вруби седьмое видео", 7), ("включи восьмое", 8), ("девятое видео", 9), ("включи двенадцатое видео", 12),
    ("включи второе видео из списка", 2), ("ну включи мне пожалуйста второе видео", 2), ("включи #3", 3),
    ("второй результат", 2), ("включи второй трек", 2),
])
def test_pick_phrases(text, n):
    r = p(text)
    assert r is not None and r.kind == "index" and r.n == n, text


@pytest.mark.parametrize("text,kind", [("следующее видео", "next"), ("включи следующий ролик", "next"),
                                      ("предыдущее видео", "prev"), ("включи прошлое видео", "prev")])
def test_next_prev(text, kind):
    assert p(text).kind == kind


@pytest.mark.parametrize("text", [
    "включи первый канал", "открой второе окно", "включи реле 2", "включи свет", "второе", "следующий",
    "дальше", "включи видео котики", "поставь будильник на 6", "сколько будет два плюс два", "закрой второе видео",
    "включи пятое время года", "два видео", "включи музыку", "",
])
def test_not_a_pick(text):
    assert p(text) is None, text


def test_next_without_noun_stays_media_key():
    assert p("следующий") is None and p("следующий трек").music


# ---------------------------------------------------------------- parsing YouTube pages
def test_search_numbering_skips_ads_shorts_shelves_channels_and_dupes():
    vids = Y.videos_from_html(SEARCH_HTML)
    assert [v["id"] for v in vids] == ["VID00000001", "VID00000002", "VID00000003", "VID00000004", "VID00000005"]
    assert vids[0]["title"] == "Котики: первое видео" and vids[0]["channel"] == "Канал VID00000001"
    assert vids[3]["title"] == "Котики: четвёртое видео"        # new lockupViewModel layout


def test_channel_and_playlist_pages():
    ch = Y.videos_from_data(json.loads((FX / "yt_channel.json").read_text(encoding="utf-8")))
    assert [v["id"] for v in ch] == ["CH000000001", "CH000000002", "CH000000003"]
    pl = Y.videos_from_data(json.loads((FX / "yt_playlist.json").read_text(encoding="utf-8")))
    assert len(pl) == 5 and pl[1]["title"] == "Трек плейлиста 2"


def test_initial_data_variants_and_garbage():
    assert Y.initial_data('<script>window["ytInitialData"] = {"a": [1]};</script>') == {"a": [1]}
    assert Y.initial_data("<html>no data</html>") is None
    assert Y.initial_data("var ytInitialData = {broken") is None
    assert Y.videos_from_html("<html></html>") == []


@pytest.mark.parametrize("url,kind,fetch", [
    (SEARCH_URL, "search", SEARCH_URL),
    ("https://www.youtube.com/@kotiki", "channel", "https://www.youtube.com/@kotiki/videos"),
    ("https://www.youtube.com/@kotiki/featured", "channel", "https://www.youtube.com/@kotiki/videos"),
    ("https://www.youtube.com/@kotiki/videos", "channel", "https://www.youtube.com/@kotiki/videos"),
    ("https://www.youtube.com/channel/UC123/videos", "channel", "https://www.youtube.com/channel/UC123/videos"),
    ("https://www.youtube.com/playlist?list=PL1", "playlist", "https://www.youtube.com/playlist?list=PL1"),
    ("https://www.youtube.com/watch?v=abc&list=PL1", "watch_list", "https://www.youtube.com/playlist?list=PL1"),
    ("https://www.youtube.com/watch?v=abc", "watch", "https://www.youtube.com/watch?v=abc"),
    ("https://www.youtube.com/", None, "https://www.youtube.com/"),
    ("https://vk.com/video", None, "https://vk.com/video"),
])
def test_page_kinds(url, kind, fetch):
    assert Y.page_kind(url) == kind and Y.list_url(url) == fetch


def test_fetch_list_falls_back_to_ytdlp_and_never_raises():
    assert Y.fetch_list(SEARCH_URL, get_html=lambda u: SEARCH_HTML, ytdlp=lambda u: [])[0]["id"] == "VID00000001"
    alt = [{"id": "Y1", "title": "из yt-dlp", "channel": ""}]

    def down(u):
        raise OSError("offline")
    assert Y.fetch_list(SEARCH_URL, get_html=down, ytdlp=lambda u: alt) == alt
    assert Y.fetch_list(SEARCH_URL, get_html=lambda u: "<html/>", ytdlp=lambda u: alt) == alt
    assert Y.fetch_list(SEARCH_URL, get_html=down, ytdlp=lambda u: (_ for _ in ()).throw(RuntimeError())) == []


# ---------------------------------------------------------------- picking
class FakeBackend:
    def __init__(self, wins, fg=None):
        self.wins, self.fg = wins, fg

    def windows(self):
        return list(self.wins)

    def foreground(self):
        return self.fg if self.fg is not None else (self.wins[0].hwnd if self.wins else 0)

    def activate(self, hwnd):
        return True


class FakeApp:
    def __init__(self, backend=None, retitle=None):
        self.opened, self.backend, self.retitle = [], backend, retitle

    def open(self, url=None):
        self.opened.append(url)
        title = self.retitle(url) if callable(self.retitle) else self.retitle
        if title and self.backend:                   # the app window shows the video
            self.backend.wins = [Window(w.hwnd, title, w.pid, w.exe) for w in self.backend.wins] or \
                [Window(77, title, 1, "chrome.exe")]


def picker(wins=(), url_of=None, retitle=None, fetch=None, screen=None, invoke=None, audio=lambda: True):
    be = FakeBackend(list(wins))
    media = types.SimpleNamespace(youtube=FakeApp(be, retitle), ytmusic=FakeApp(be, retitle))
    nav = []
    bar = dict(url_of or {})

    def navigate(hwnd, url):
        nav.append((hwnd, url))
        bar[hwnd] = url                               # the tab now shows the video
        return True
    pk = Y.Picker(media, backend=be, address_bar=lambda h: bar.get(h), navigate=navigate,
                  fetch=fetch or (lambda u: Y.videos_from_html(SEARCH_HTML)), sleep=lambda s: None, verify_s=0.01,
                  screen_links=screen or (lambda h: []), invoke=invoke or (lambda el: False), audio=audio, audio_s=0.01)
    return pk, media, nav, be


TAB = Window(10, "котики - YouTube - Google Chrome", 1, "chrome.exe")
PWA = Window(20, "котики - YouTube", 2, "chrome.exe")


def test_jarvis_search_then_second_video_in_pwa():
    titles = {v["id"]: v["title"] for v in Y.videos_from_html(SEARCH_HTML)}
    pk, media, nav, be = picker([PWA], retitle=lambda url: titles[url.split("v=")[1]] + " - YouTube")
    pk.remember(SEARCH_URL, "котики", background=False)
    msg = pk.open(Y.Pick("index", 2))
    assert msg == "Включаю 2-е: Котики: второе видео."
    assert media.youtube.opened == ["https://www.youtube.com/watch?v=VID00000002"] and not nav
    assert pk.last.current == 1
    assert pk.open(Y.Pick("next")).startswith("Включаю 3-е")                 # «следующее»


def test_manual_navigation_in_chrome_tab_uses_address_bar_and_same_tab():
    fetched = []

    def fetch(u):
        fetched.append(u)
        return Y.videos_from_data(json.loads((FX / "yt_channel.json").read_text(encoding="utf-8")))
    pk, media, nav, be = picker([TAB], url_of={10: "https://www.youtube.com/@kotiki"}, fetch=fetch)
    msg = pk.open(Y.Pick("index", -1))
    assert msg == "Включаю 3-е: Третье видео канала."
    assert fetched == ["https://www.youtube.com/@kotiki"] and nav == [(10, "https://www.youtube.com/watch?v=CH000000003")]
    assert media.youtube.opened == []                                       # same tab, not a new window
    assert pk.open(Y.Pick("prev")).startswith("Включаю 2-е")                # watch page we opened -> list kept


def test_playlist_watch_page_keeps_list_and_position():
    pl = Y.videos_from_data(json.loads((FX / "yt_playlist.json").read_text(encoding="utf-8")))
    pk, media, nav, be = picker([TAB], url_of={10: "https://www.youtube.com/watch?v=PL000000002&list=PLx"},
                                fetch=lambda u: pl)
    assert pk.open(Y.Pick("next")) == "Включаю 3-е: Трек плейлиста 3."
    assert nav[-1][1] == "https://www.youtube.com/watch?v=PL000000003&list=PLx"


def test_no_fake_success_when_nothing_changed():
    pk, media, nav, be = picker([PWA], retitle=None)
    pk.remember(SEARCH_URL, "котики", background=False)
    msg = pk.open(Y.Pick("index", 2))
    assert "не вижу, что оно открылось" in msg and not msg.startswith("Включаю")
    assert pk.last.current is None


def test_honest_failures():
    pk, *_ = picker([])
    assert "Не вижу открытого списка" in pk.open(Y.Pick("index", 2))
    pk, *_ = picker([PWA])
    assert "приложении ютуба" in pk.open(Y.Pick("index", 2))                 # PWA, nothing known
    pk, *_ = picker([TAB], url_of={10: "https://www.youtube.com/watch?v=zzz"})
    assert "не список" in pk.open(Y.Pick("index", 2))
    pk, *_ = picker([TAB], url_of={10: SEARCH_URL}, fetch=lambda u: [])
    assert "Не смог прочитать список" in pk.open(Y.Pick("index", 2))
    pk, *_ = picker([PWA], retitle="x - YouTube")
    pk.remember(SEARCH_URL, "котики", background=False)
    assert pk.open(Y.Pick("index", 9)) == "В списке только 5 видео."
    pk.last.current = 4
    assert pk.open(Y.Pick("next")) == "Это было последнее видео в списке."


def test_media_skill_routes_picks_and_keeps_media_keys(monkeypatch):
    from jarvis.skills import media_skill as MS
    sk = MS.MediaSkill.__new__(MS.MediaSkill)
    opened = []
    sk.media = types.SimpleNamespace(picker=types.SimpleNamespace(open=lambda pk: opened.append(pk) or "ok",
                                                                  has_context=lambda: False),
                                     media_key=lambda a, times=1: f"key:{a}")
    r = sk.match("включи второе видео", normalize("включи второе видео"))
    assert r.reply == "" and r.background and r.then() == "ok" and opened[0].n == 2   # no reply before the result
    assert sk.match("следующее видео", "следующее видео").reply == "key:next"           # no list -> media key
    sk.media.picker.has_context = lambda: True
    assert sk.match("следующее видео", "следующее видео").reply == ""
    assert sk.match("следующий трек", "следующий трек").reply == "key:next"             # music: media key
    r = sk.call("play_nth_video", {"n": "3"})
    r.then()
    assert opened[-1].n == 3


def test_smarthome_leaves_picks_alone():
    from jarvis.skills.smarthome_skill import SmartHomeSkill
    sk = SmartHomeSkill.__new__(SmartHomeSkill)
    sk.home = types.SimpleNamespace(devices=[object()])
    sk.switch = lambda name, on: "switched"
    assert sk.match("включи второе видео", "включи второе видео") is None
    assert sk.match("включи реле 2", "включи реле 2") == "switched"


# ---------------------------------------------------------------- 1.0.7: on-screen order (home feed), playback check
from jarvis import uia  # noqa: E402


def link(vid, name, l, t, href=None, w=300, h=170):
    return {"href": href or f"https://www.youtube.com/watch?v={vid}", "name": name, "rect": (l, t, l + w, t + h),
            "el": f"el-{vid}-{name[:3]}"}


def test_screen_order_rows_then_columns_dedup_skip_shorts_ads():
    links = [
        link("B2222222222", "", 400, 100), link("B2222222222", "Второе в ряду", 400, 280, h=40),
        link("A1111111111", "", 50, 102), link("A1111111111", "Первое", 50, 280, h=40),
        link("C3333333333", "Первое во втором ряду", 50, 520),
        link("SHORTS00001", "шортс", 750, 100, href="https://www.youtube.com/shorts/SHORTS00001"),
        link("AD000000001", "Реклама · купи", 750, 100),
        link("ADV00000001", "", 750, 100, href="https://www.googleadservices.com/pagead/aclk?adurl=x"),
        link("HIDDEN00001", "вне экрана", 0, 0, w=0, h=0),
        link("D4444444444", "Второе во втором ряду", 400, 515),
    ]
    items = uia.order_links(links)
    assert [i["id"] for i in items] == ["A1111111111", "B2222222222", "C3333333333", "D4444444444"]
    assert items[1]["title"] == "Второе в ряду" and items[0]["el"] == "el-A1111111111-"   # thumbnail element


def test_home_feed_bare_ordinal_clicks_the_nth_on_screen():
    home = Window(30, "YouTube", 3, "chrome.exe")                        # PWA, personalised home feed
    feed = uia.order_links([link("A1111111111", "Первое", 50, 100), link("B2222222222", "Второе из ленты", 400, 100)])
    clicked = []

    def invoke(el):
        clicked.append(el)
        be.wins = [Window(30, "Второе из ленты - YouTube", 3, "chrome.exe")]
        return True
    pk, media, nav, be = picker([home], screen=lambda h: feed if h == 30 else [], invoke=invoke)
    msg = pk.open(Y.parse_pick(normalize("включи второе.")))
    assert msg == "Включаю 2-е: Второе из ленты."
    assert clicked == [feed[1]["el"]] and media.youtube.opened == [] and not nav   # clicked in the same window
    assert pk.last.screen and pk.last.current == 1
    # still on that video: «следующее» continues the same on-screen list instead of the sidebar
    pk.screen_links = lambda h: [{"id": "SIDEBAR0001", "title": "сбоку", "el": "x", "rect": (0, 0, 1, 1)}]
    assert pk.current_list()[0].items[0]["id"] == "A1111111111"


def test_says_vkluchayu_only_with_sound():
    feed = uia.order_links([link("A1111111111", "Первое", 50, 100)])
    home = Window(30, "YouTube", 3, "chrome.exe")

    def run(audio):
        def invoke(el):
            be.wins = [Window(30, "Первое - YouTube", 3, "chrome.exe")]
            return True
        nonlocal_be = picker([home], screen=lambda h: feed, invoke=invoke, audio=audio)
        pk, media, nav, be = nonlocal_be
        return pk, be
    pk, be = run(lambda: True)
    be.wins = [home]
    assert pk.open(Y.Pick("index", 1)).startswith("Включаю 1-е")
    pk, be = run(lambda: False)
    be.wins = [home]
    assert "звука не слышу" in pk.open(Y.Pick("index", 1))
    pk, be = run(lambda: None)
    be.wins = [home]
    assert pk.open(Y.Pick("index", 1)) == "Открыл 1-е видео: Первое."


def test_real_world_vkluchi_vtoroe_is_not_the_relay():
    """1.0.6 bug: YouTube home open, «включи второе.» -> «Не достучался до устройства реле 2»."""
    from jarvis.skills.smarthome_skill import SmartHomeSkill
    sk = SmartHomeSkill.__new__(SmartHomeSkill)
    sk.home = types.SimpleNamespace(devices=[object()])
    sk.switch = lambda name, on: "relay"
    for ph in ("включи второе.", "Включи второе", "включи 2", "давай второе", "включи втарое"):
        assert sk.match(ph, normalize(ph)) is None, ph
    for ph in ("включи реле 2", "выключи свет", "включи розетку", "включи второе реле"):
        assert sk.match(ph, normalize(ph)) == "relay", ph
