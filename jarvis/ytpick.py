"""«Включи второе видео»: open the N-th video of the YouTube list on screen (1.0.7).

Where the list comes from:
  1. Jarvis searched itself («найди на ютубе …», «включи видео …», «включи песню …») -> the ordered result
     list is fetched right away (plain HTTP, ytInitialData of the results page; yt-dlp as fallback) and cached.
  2. The user navigated by hand in a Chrome/Edge TAB -> the address bar is read via UI Automation, that page
     (search results / channel videos / playlist) is fetched and numbered the same way.
  3. A YouTube web-app (PWA) window has no address bar -> the last list Jarvis knows is used if the window
     still shows it (title) or it is fresh; otherwise Jarvis honestly says it cannot see the page.
  0. Whatever page is open (also the personalised home feed): the video links rendered in the window are
     read via UI Automation and numbered by position (rows top->bottom, left->right); this is tried first.
Numbering = regular videos in the order shown. Not counted: ads, Shorts shelves, horizontal "shelves"
(«Похожие», «Последние от …»), channels, playlists/mixes, people. Video n is opened in the same tab (address bar)
or through the same YouTube app; «Включаю» is said only after the window really shows the video.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

log = logging.getLogger(__name__)

YT = "https://www.youtube.com"
YTM = "https://music.youtube.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/129.0.0.0 Safari/537.36")

# ------------------------------------------------------------------------------------------- phrase parsing
ORDINAL_STEMS = [                       # longest first; Whisper typos included
    ("одиннадцат", 11), ("одинадцат", 11), ("двенадцат", 12), ("тринадцат", 13), ("четырнадцат", 14),
    ("пятнадцат", 15), ("шестнадцат", 16), ("семнадцат", 17), ("восемнадцат", 18), ("девятнадцат", 19),
    ("двадцат", 20), ("предпоследн", -2), ("последн", -1), ("крайн", -1),
    ("перв", 1), ("перьв", 1), ("втор", 2), ("втар", 2), ("фтор", 2), ("трет", 3), ("трейт", 3), ("трец", 3),
    ("четверт", 4), ("четвёрт", 4), ("читверт", 4), ("четвирт", 4), ("пят", 5), ("пiят", 5), ("пит", 5),
    ("шест", 6), ("шост", 6), ("седьм", 7), ("седм", 7), ("сьом", 7), ("сём", 7), ("восьм", 8), ("васьм", 8),
    ("восм", 8), ("девят", 9), ("девят", 9), ("десят", 10), ("дисят", 10),
]
ORD_ENDINGS = ("ое", "ее", "ой", "ый", "ий", "ая", "яя", "ую", "юю", "ье", "ья", "ью", "ого", "его", "ом", "ем",
               "ым", "им", "ые", "ие", "ей", "ои", "оя", "ие")
CARDINALS = {"один": 1, "одно": 1, "раз": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
             "семь": 7, "восемь": 8, "девять": 9, "десять": 10}
VERBS = re.compile(r"^(?!вкладк)(?:вкл\w*|влюч\w*|включ\w*|фключ\w*|вруб\w*|постав\w*|запуст\w*|откр\w*|давай|го|кликн\w*|"
                   r"нажм\w*|жми|тыкн\w*|ткни|выбер\w*|покаж\w*|смотр\w*|глянем|play)$")
NOUNS = re.compile(r"^(?:видео\w*|видос\w*|видях\w*|видюх\w*|видик\w*|ролик\w*|ролек\w*|клип\w*|трек\w*|песн\w*|"
                   r"результат\w*|вариант\w*|ссылк\w*|штук\w*|серию|серия)$")
FILLER = {"мне", "нам", "пожалуйста", "плиз", "ну", "там", "это", "этот", "эту", "вот", "ка", "сверху", "снизу",
          "по", "счету", "счёту", "порядку", "из", "списка", "списке", "в", "на", "во", "ютубе", "ютуб", "youtube",
          "тот", "то", "которое", "который", "номер", "номером", "под", "что", "а", "и", "же", "еще", "ещё", "теперь",
          "тогда", "лучше", "просто", "мол", "типа", "короче", "слушай", "е", "й", "ю", "я", "ое", "ой", "результатах",
          "поиске", "поиска", "выдаче", "найденных", "этих", "них"}
NEXT = re.compile(r"^(?:следующ\w*|след|некст|дальше)$")
PREV = re.compile(r"^(?:предыдущ\w*|прошл\w*|прежн\w*)$")


@dataclass
class Pick:
    kind: str            # "index" | "next" | "prev"
    n: int = 0           # 1-based; -1 last, -2 one before last
    music: bool = False  # «трек/песню» -> prefer a YouTube Music list


def _ordinal(tok: str) -> Optional[int]:
    m = re.fullmatch(r"(\d{1,2})(?:[-]?(?:е|ое|й|ой|ий|ю|ую|я|ая|ый))?", tok)
    if m:
        return int(m.group(1)) or None
    if len(tok) < 4 or not tok.endswith(ORD_ENDINGS):
        return None
    for stem, n in ORDINAL_STEMS:
        if tok.startswith(stem):
            return n
    return None


def parse_pick(norm: str) -> Optional[Pick]:
    """«включи второе видео», «номер 3», «давай пятое», «последний ролик», «следующее видео» -> Pick.
    Strict: every other word must be filler, so «включи первый канал» or «открой второе окно» are not ours."""
    toks = [t for t in (norm or "").replace("-", " ").split() if t]
    if not toks or len(toks) > 9:
        return None
    verb = noun = False
    music = False
    found: Optional[Pick] = None
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("номер", "номером", "№") and i + 1 < len(toks) and toks[i + 1] in CARDINALS:
            if found:
                return None
            found = Pick("index", CARDINALS[toks[i + 1]])
            verb = True                      # «номер три» is explicit enough on its own
            i += 2
            continue
        if VERBS.match(t):
            verb = True
        elif NOUNS.match(t):
            noun = True
            music = music or t.startswith(("трек", "песн"))
        elif NEXT.match(t) or PREV.match(t):
            if found:
                return None
            found = Pick("next" if NEXT.match(t) else "prev")
        elif (n := _ordinal(t)) is not None:
            if found:
                return None
            found = Pick("index", n)
            if i > 0 and toks[i - 1] in ("номер", "номером", "№"):
                verb = True                  # «номер 2»
        elif t not in FILLER:
            return None
        i += 1
    if not found:
        return None
    if found.kind in ("next", "prev") and not noun:
        return None                      # bare «следующий»/«дальше» stays a media key
    if not (verb or noun):
        return None                      # «второе» alone: too ambiguous
    if found.kind == "index" and not noun and toks[0] in CARDINALS:
        return None
    found.music = music
    return found


# ------------------------------------------------------------------------------------------- page parsing
SKIP_KEYS = {"reelShelfRenderer", "shelfRenderer", "richShelfRenderer", "horizontalCardListRenderer",
             "adSlotRenderer", "promotedVideoRenderer", "searchPyvRenderer", "promotedSparklesWebRenderer",
             "shortsLockupViewModel", "reelItemRenderer", "secondaryResults", "header", "topbar",
             "channelRenderer", "playlistRenderer", "radioRenderer", "lockupViewModelShelf",
             "universalWatchCardRenderer", "movieRenderer", "backgroundPromoRenderer", "frameworkUpdates"}
VIDEO_KEYS = ("videoRenderer", "gridVideoRenderer", "playlistVideoRenderer", "playlistPanelVideoRenderer")


def initial_data(html: str) -> Optional[dict]:
    for marker in ('var ytInitialData = ', 'window["ytInitialData"] = ', "ytInitialData = "):
        i = html.find(marker)
        if i >= 0:
            j = html.find("{", i)
            try:
                data, _ = json.JSONDecoder().raw_decode(html[j:])
                return data if isinstance(data, dict) else None
            except ValueError:
                continue
    return None


def _text(v: Any) -> str:
    if isinstance(v, dict):
        if "simpleText" in v:
            return str(v["simpleText"])
        if "runs" in v:
            return "".join(str(r.get("text", "")) for r in v["runs"] if isinstance(r, dict))
        if "content" in v:
            return str(v["content"])
    return str(v or "")


def _is_short(r: dict) -> bool:
    nav = r.get("navigationEndpoint") or {}
    url = ((nav.get("commandMetadata") or {}).get("webCommandMetadata") or {}).get("url", "")
    return "reelWatchEndpoint" in nav or str(url).startswith("/shorts/")


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k in SKIP_KEYS:
                continue
            if k in VIDEO_KEYS and isinstance(v, dict) and v.get("videoId"):
                if not _is_short(v):
                    yield {"id": v["videoId"], "title": _text(v.get("title")),
                           "channel": _text(v.get("ownerText") or v.get("shortBylineText") or v.get("longBylineText"))}
                continue
            if k == "lockupViewModel" and isinstance(v, dict) and v.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
                meta = ((v.get("metadata") or {}).get("lockupMetadataViewModel") or {})
                yield {"id": v.get("contentId"), "title": _text(meta.get("title")), "channel": ""}
                continue
            yield from _walk(v)


def videos_from_data(data: Optional[dict]) -> list[dict]:
    out, seen = [], set()
    for v in _walk(data or {}):
        if v.get("id") and v["id"] not in seen:
            seen.add(v["id"])
            out.append(v)
    return out


def videos_from_html(html: str) -> list[dict]:
    return videos_from_data(initial_data(html))


# ------------------------------------------------------------------------------------------- URLs
def page_kind(url: str) -> Optional[str]:
    """search | channel | playlist | watch_list | watch | None (not a YouTube list page)."""
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = u.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host not in ("youtube.com", "music.youtube.com"):
        return None
    q = urllib.parse.parse_qs(u.query)
    if u.path.startswith("/results") and q.get("search_query"):
        return "search"
    if u.path.startswith("/playlist") and q.get("list"):
        return "playlist"
    if u.path.startswith("/watch"):
        return "watch_list" if q.get("list") else "watch"
    if re.match(r"^/(@[^/]+|c/[^/]+|channel/[^/]+|user/[^/]+)(/videos|/streams|/featured)?/?$", u.path):
        return "channel"
    return None


def list_url(url: str) -> str:
    """The page to fetch for numbering: a channel home -> its «Видео» tab, watch&list -> the playlist page."""
    kind = page_kind(url)
    u = urllib.parse.urlparse(url)
    if kind == "channel" and not u.path.rstrip("/").endswith(("/videos", "/streams")):
        base = re.sub(r"/featured/?$", "", u.path.rstrip("/"))
        return f"{YT}{base}/videos"
    if kind == "watch_list":
        lst = urllib.parse.parse_qs(u.query)["list"][0]
        return f"{YT}/playlist?list={urllib.parse.quote(lst)}"
    return url


def search_url(query: str) -> str:
    return f"{YT}/results?search_query={urllib.parse.quote_plus(query)}"


def watch_url(vid: str, lst: Optional[str] = None, music: bool = False) -> str:
    base = YTM if music else YT
    return f"{base}/watch?v={vid}" + (f"&list={urllib.parse.quote(lst)}" if lst else "")


# ------------------------------------------------------------------------------------------- fetching
def fetch_html(url: str, timeout: float = 8.0) -> str:
    import requests
    r = requests.get(url, timeout=timeout, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"},
                     cookies={"CONSENT": "YES+cb", "SOCS": "CAI"}, params={"hl": "ru"})
    r.raise_for_status()
    return r.text


def fetch_ytdlp(url: str, limit: int = 30) -> list[dict]:
    try:
        import yt_dlp
    except ImportError:
        return []
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist",
            "playlistend": limit}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    out = []
    for e in info.get("entries") or []:
        u = str(e.get("url") or "")
        if e.get("id") and "/shorts/" not in u and e.get("ie_key", "Youtube") in ("Youtube", None, ""):
            out.append({"id": e["id"], "title": e.get("title") or "", "channel": e.get("channel") or ""})
    return out


def fetch_list(url: str, get_html: Callable[[str], str] = fetch_html,
               ytdlp: Callable[[str], list] = fetch_ytdlp) -> list[dict]:
    """Ordered videos of a YouTube list page. Never raises."""
    target = list_url(url)
    try:
        vids = videos_from_html(get_html(target))
        if vids:
            return vids
        log.info("ytpick: в странице %s нет списка видео — пробую yt-dlp", target)
    except Exception as e:                      # noqa: BLE001
        log.info("ytpick: %s: %s — пробую yt-dlp", target, e)
    try:
        return ytdlp(target)
    except Exception as e:                      # noqa: BLE001
        log.warning("ytpick yt-dlp %s: %s", target, e)
        return []


# ------------------------------------------------------------------------------------------- state
@dataclass
class VideoList:
    url: str                       # page the list belongs to
    items: list = field(default_factory=list)
    query: str = ""
    music: bool = False            # YouTube Music list (opened in the YT Music app)
    playlist: Optional[str] = None
    at: float = field(default_factory=time.time)
    current: Optional[int] = None  # 0-based index Jarvis opened last
    hwnd: Optional[int] = None     # the window Jarvis navigated
    shown_title: str = ""          # that window's title right after Jarvis opened the video
    screen: bool = False           # read from the screen (UI Automation), not fetched

    def fresh(self, max_age: float = 1800) -> bool:
        return time.time() - self.at < max_age


def _norm_title(s: str) -> str:
    s = re.sub(r"\s[-—–]\s(youtube( music)?|google chrome|microsoft.? edge)\s*$", "", (s or "").lower().replace("ё", "е"))
    s = re.sub(r"\s[-—–]\s(youtube( music)?)\s*$", "", s)
    return re.sub(r"[^\w]+", " ", s).strip()


def _yt_window(w) -> bool:
    return w.is_browser and "youtube" in (w.title or "").lower()


class Picker:
    """Keeps the last list and opens «N-th» item. Window/UIA/open functions are injectable for tests."""

    def __init__(self, media: Any, backend: Any = None, address_bar: Optional[Callable[[int], Optional[str]]] = None,
                 navigate: Optional[Callable[[int, str], bool]] = None, fetch: Callable[[str], list] = fetch_list,
                 sleep: Callable[[float], None] = time.sleep, verify_s: float = 8.0,
                 screen_links: Optional[Callable[[int], list]] = None, invoke: Optional[Callable[[Any], bool]] = None,
                 audio: Optional[Callable[[], Optional[bool]]] = None, audio_s: float = 5.0):
        from . import uia
        self.media = media
        self._backend = backend
        self.address_bar = address_bar or uia.address_bar
        self.navigate = navigate or (lambda hwnd, url: uia.navigate(hwnd, url, self.backend))
        self.fetch = fetch
        self.sleep = sleep
        self.verify_s = verify_s
        self.screen_links = screen_links or uia.video_links
        self.invoke = invoke or uia.invoke
        self.audio = audio or uia.browser_audio_playing
        self.audio_s = audio_s
        self.last: Optional[VideoList] = None
        self._lock = threading.Lock()
        self._loading: Optional[threading.Event] = None

    @property
    def backend(self):
        if self._backend is None:
            from .windows import default_backend
            self._backend = default_backend() or False
        return self._backend or None

    # ---------------------------------------------------------------- caching what Jarvis itself opened
    def remember(self, url: str, query: str = "", items: Optional[list] = None, music: bool = False,
                 current: Optional[int] = None, background: bool = True) -> None:
        """Store the list of a page Jarvis just opened; fetch it (in the background) if items are not given."""
        lst = VideoList(url=url, items=list(items or []), query=query, music=music, current=current)
        with self._lock:
            self.last = lst
        if items is not None:
            return
        ev = threading.Event()
        self._loading = ev

        def load():
            try:
                lst.items = self.fetch(url)
                log.info("ytpick: список «%s»: %d видео", query or url, len(lst.items))
            finally:
                ev.set()
        if background:
            threading.Thread(target=load, name="ytpick-fetch", daemon=True).start()
        else:
            load()

    def has_context(self) -> bool:
        return bool(self.last and self.last.fresh() and self.last.current is not None)

    # ---------------------------------------------------------------- which list is on screen
    def _windows(self) -> list:
        b = self.backend
        if not b:
            return []
        try:
            wins = [w for w in b.windows() if _yt_window(w)]
            fg = b.foreground()
        except Exception as e:                  # noqa: BLE001
            log.debug("ytpick windows: %s", e)
            return []
        wins.sort(key=lambda w: w.hwnd != fg)   # foreground first, then z-order
        return wins

    def current_list(self, music: bool = False) -> tuple[Optional[VideoList], Optional[Any], str]:
        """(list, window, why-not). Prefers what is really on screen over the cache."""
        if self._loading is not None:
            self._loading.wait(10)              # «найди …» then at once «включи второе»
        last = self.last if (self.last and self.last.fresh()) else None
        for w in self._windows():
            if last and last.items and last.hwnd == w.hwnd and last.current is not None and \
                    last.shown_title and w.title == last.shown_title:
                return last, w, ""              # still on the video Jarvis opened: «следующее» = same list
            screen = self.screen_links(w.hwnd)   # exactly what is rendered: also the personalised home feed
            if screen:
                lst = VideoList(url=f"screen:{w.hwnd}", items=screen, hwnd=w.hwnd, screen=True,
                                music="youtube music" in w.title.lower())
                with self._lock:
                    self.last = lst
                return lst, w, ""
            if w.is_browser_tab_window:
                url = self.address_bar(w.hwnd)
                kind = page_kind(url) if url else None
                if url and kind in ("search", "channel", "playlist", "watch_list"):
                    if last and last.url == url and last.items:
                        last.hwnd = w.hwnd
                        return last, w, ""
                    items = self.fetch(url)
                    if not items:
                        return None, w, "fetch"
                    q: dict[str, list[str]] = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                    lst = VideoList(url=url, items=items, query=(q.get("search_query") or [""])[0],
                                    playlist=(q.get("list") or [""])[0] or None, hwnd=w.hwnd)
                    if kind == "watch_list":
                        vid = (q.get("v") or [""])[0]
                        lst.current = next((i for i, v in enumerate(items) if v["id"] == vid), None)
                    with self._lock:
                        self.last = lst
                    return lst, w, ""
                if kind == "watch" and last and last.hwnd == w.hwnd and last.items:
                    return last, w, ""          # we opened this video from the list: «следующее» works
                if url is not None:
                    return None, w, "not_list"  # a single video / home page / something else
            # PWA (or an address bar UIA could not read): no URL -> only what Jarvis knows
            if last and last.items:
                t = _norm_title(w.title)
                shown = {_norm_title(last.query)} | {_norm_title(v["title"]) for v in last.items}
                if t in shown or last.hwnd == w.hwnd or last.fresh(600):
                    last.hwnd = last.hwnd or w.hwnd
                    return last, w, ""
            return None, w, "pwa"
        if last and last.items:
            return last, None, ""
        if last and not last.items:
            return None, None, "fetch"
        return None, None, "none"

    # ---------------------------------------------------------------- opening
    def _index(self, pick: Pick, lst: VideoList) -> Optional[int]:
        n = len(lst.items)
        if pick.kind == "next":
            i = (lst.current if lst.current is not None else -1) + 1
        elif pick.kind == "prev":
            i = (lst.current if lst.current is not None else 1) - 1
        elif pick.n < 0:
            i = n + pick.n
        else:
            i = pick.n - 1
        return i if 0 <= i < n else None

    def _shows(self, w_hwnd: Optional[int], video: dict, before: dict) -> bool:
        """The video is on screen: the tab's address bar has its id, or a YouTube window shows its title."""
        b = self.backend
        want = _norm_title(video.get("title", ""))[:25]
        deadline = time.time() + self.verify_s
        while True:
            if w_hwnd is not None:
                url = self.address_bar(w_hwnd)
                if url and video["id"] in url:
                    return True
            if b:
                try:
                    for w in b.windows():
                        if not _yt_window(w):
                            continue
                        t = _norm_title(w.title)
                        if want and t.startswith(want):
                            return True
                        if w_hwnd is None and video.get("_opened_new") and before.get(w.hwnd) != w.title:
                            return True             # a new YouTube app window, or the app window navigated
                except Exception:               # noqa: BLE001
                    pass
            if time.time() >= deadline:
                return False
            self.sleep(0.4)

    def open(self, pick: Pick) -> str:
        lst, win, why = self.current_list(pick.music)
        if lst is None:
            return {
                "pwa": "Не вижу, какая страница открыта в приложении ютуба. Скажи, например: «найди на ютубе котиков», "
                       "а потом «включи второе».",
                "not_list": "Сейчас на ютубе открыт не список видео. Открой поиск, канал или плейлист — и скажи номер.",
                "fetch": "Не смог прочитать список видео с ютуба (нет интернета?). Попробуй ещё раз.",
            }.get(why, "Не вижу открытого списка видео на ютубе. Скажи, например: «найди на ютубе котиков», "
                       "а потом «включи второе».")
        i = self._index(pick, lst)
        if i is None:
            if pick.kind == "next":
                return "Это было последнее видео в списке."
            if pick.kind == "prev":
                return "Это первое видео в списке."
            return f"В списке только {len(lst.items)} видео."
        video = dict(lst.items[i])
        url = watch_url(video["id"], lst.playlist, music=lst.music)
        b = self.backend
        before = {w.hwnd: w.title for w in self._windows()} if b else {}
        tab = win is not None and win.is_browser_tab_window
        ok = False
        if video.get("el") is not None and win is not None:
            ok = self.invoke(video["el"])              # click it, like the user would (same window)
            video["_opened_new"] = ok                   # any change of a YouTube window title then counts
            tab = ok or tab
        if not ok and tab and win is not None:
            ok = self.navigate(win.hwnd, url)           # same tab
        if not ok:
            try:
                app = self.media.ytmusic if lst.music else self.media.youtube
                app.open(url)                           # same YouTube app (Chrome may reuse its window)
                ok, tab = True, False
                video["_opened_new"] = True
            except Exception as e:                      # noqa: BLE001
                log.warning("ytpick open: %s", e)
                return "Не получилось открыть видео."
        title = video.get("title") or "без названия"
        if b and not self._shows(win.hwnd if (tab and win is not None) else None, video, before):
            return f"Отправил ютубу {i + 1}-е видео «{title}», но не вижу, что оно открылось."
        lst.current = i
        lst.at = time.time()
        if win is not None and (tab or video.get("el") is not None):
            lst.hwnd = win.hwnd
        lst.shown_title = self._title_of(lst.hwnd)
        playing = self._playing()
        if playing:
            return f"Включаю {i + 1}-е: {title}."
        if playing is None:                     # cannot measure sound here: say only what was verified
            return f"Открыл {i + 1}-е видео: {title}."
        return f"Открыл {i + 1}-е видео «{title}», но звука не слышу — может, оно на паузе или звук выключен."

    def _title_of(self, hwnd: Optional[int]) -> str:
        b = self.backend
        if not b or hwnd is None:
            return ""
        try:
            return next((w.title for w in b.windows() if w.hwnd == hwnd), "")
        except Exception:                       # noqa: BLE001
            return ""

    def _playing(self) -> Optional[bool]:
        """Wait (up to audio_s) for browser sound: the only real sign that playback started."""
        deadline = time.time() + self.audio_s
        while True:
            r = self.audio()
            if r is None or r:
                return r
            if time.time() >= deadline:
                return False
            self.sleep(0.4)
