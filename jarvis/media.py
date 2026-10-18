"""YouTube / YouTube Music (Chrome PWA), music search & media keys."""
from __future__ import annotations

import logging
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Callable, Optional

from . import platform_utils as pu

log = logging.getLogger(__name__)

YT_URL = "https://www.youtube.com"
YTM_URL = "https://music.youtube.com"


class ChromeApp:
    """A Chrome-installed web app (PWA) such as YouTube or YouTube Music."""

    def __init__(self, title: str, default_url: str, cfg: dict, chrome: Optional[str], launcher=pu.spawn):
        self.title = title
        self.default_url = default_url
        self.cfg = cfg or {}
        self.chrome = chrome
        self._spawn = launcher
        self.shortcut = Path(self.cfg.get("shortcut") or (pu.chrome_apps_dir() / f"{title}.lnk"))
        lnk = pu.parse_lnk_chrome_args(self.shortcut) if self.shortcut.exists() else {}
        self.app_id = self.cfg.get("app_id") or lnk.get("app_id") or ""
        self.profile = self.cfg.get("profile_directory") or lnk.get("profile") or "Default"

    def open(self, url: Optional[str] = None) -> str:
        """Open the app (optionally at a specific URL). Returns a description of the method used."""
        if url is None and self.shortcut.exists() and pu.IS_WIN:
            pu.open_path(self.shortcut)                       # exactly like clicking the Start menu tile
            return "shortcut"
        if self.chrome:
            if self.app_id:
                argv = [self.chrome, f"--profile-directory={self.profile}", f"--app-id={self.app_id}"]
                if url:
                    argv.append(f"--app-launch-url-for-shortcuts-menu-item={url}")
                self._spawn(argv)
                return "app-id"
            self._spawn([self.chrome, f"--profile-directory={self.profile}", f"--app={url or self.default_url}"])
            return "app-window"
        webbrowser.open(url or self.default_url)
        return "browser"


class Media:
    def __init__(self, cfg: dict, launcher=pu.spawn, opener: Callable[[str], object] = webbrowser.open):
        self.cfg = cfg or {}
        self.chrome = pu.find_chrome(self.cfg.get("chrome_path", ""))
        self.youtube = ChromeApp("YouTube", YT_URL, self.cfg.get("youtube_app", {}), self.chrome, launcher)
        self.ytmusic = ChromeApp("YouTube Music", YTM_URL, self.cfg.get("ytmusic_app", {}), self.chrome, launcher)
        self._spawn = launcher
        self._open_url = opener
        from .ytpick import Picker
        self.picker = Picker(self)            # «включи второе видео» on the list Jarvis / the user opened

    # ------------------------------------------------------------ search helpers (internet needed)
    @staticmethod
    def search_music_list(query: str, limit: int = 10) -> list[dict]:
        """YouTube Music songs for the query, in order -> [{'id','title','artist'}] (no login)."""
        try:
            from ytmusicapi import YTMusic
            yt = YTMusic(language="ru", location="RU")
            for flt in ("songs", "videos", None):
                res = yt.search(query, filter=flt, limit=limit) if flt else yt.search(query, limit=limit)
                out = []
                for r in res:
                    vid = r.get("videoId")
                    if vid:
                        artists = ", ".join(a.get("name", "") for a in (r.get("artists") or []) if a.get("name"))
                        out.append({"id": vid, "title": r.get("title", query), "artist": artists, "channel": artists})
                if out:
                    return out
        except Exception as e:
            log.warning("ytmusicapi: %s - пробую yt-dlp", e)
        return []

    @staticmethod
    def search_music(query: str) -> Optional[dict]:
        """First YouTube Music song for the query -> {'id','title','artist'} (no login)."""
        hits = Media.search_music_list(query, limit=5)
        return hits[0] if hits else Media.search_youtube(query)

    @staticmethod
    def search_youtube(query: str) -> Optional[dict]:
        """First YouTube video via yt-dlp 'ytsearch1:' -> {'id','title','artist'}."""
        try:
            import yt_dlp
            opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True,
                    "noplaylist": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            entries = info.get("entries") or []
            if entries:
                e = entries[0]
                return {"id": e.get("id"), "title": e.get("title", query), "artist": e.get("channel") or ""}
        except Exception as e:
            log.error("yt-dlp: %s", e)
        return None

    # ------------------------------------------------------------ actions -> spoken replies
    def open_youtube(self) -> str:
        self.youtube.open()
        return "Открываю ютуб."

    def open_ytmusic(self) -> str:
        self.ytmusic.open()
        return "Открываю ютуб музыку."

    def play_music(self, query: str) -> str:
        query = query.strip()
        if not query:
            self.ytmusic.open()
            return "Открываю ютуб музыку."
        hits = self.search_music_list(query)
        hit = hits[0] if hits else self.search_youtube(query)
        if not hit or not hit.get("id"):
            self.ytmusic.open(f"{YTM_URL}/search?q={urllib.parse.quote(query)}")
            return f"Не нашёл точного трека, открыл поиск: {query}."
        self.ytmusic.open(f"{YTM_URL}/watch?v={hit['id']}")
        if hits:                              # «включи второй трек» -> the 2nd song of this search
            self.picker.remember(f"{YTM_URL}/search?q={urllib.parse.quote(query)}", query, hits, music=True, current=0)
        who = f" — {hit['artist']}" if hit.get("artist") else ""
        return f"Включаю {hit['title']}{who}."

    def youtube_search(self, query: str, play_first: bool = False) -> str:
        from .ytpick import fetch_list, search_url
        url = search_url(query)
        if play_first:
            items = fetch_list(url)               # the same ordered list «второе/следующее» will use
            hit = items[0] if items else self.search_youtube(query)
            if hit and hit.get("id"):
                self.youtube.open(f"{YT_URL}/watch?v={hit['id']}")
                self.picker.remember(url, query, items or [hit], current=0)
                return f"Включаю видео: {hit['title']}."
        self.youtube.open(url)
        self.picker.remember(url, query)          # fetched in the background for «включи второе»
        return f"Ищу на ютубе: {query}."

    def google_search(self, query: str) -> str:
        url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
        if self.chrome:
            self._spawn([self.chrome, url])
        else:
            self._open_url(url)
        return f"Гуглю: {query}."

    @staticmethod
    def media_key(action: str, times: int = 1) -> str:
        ok = pu.press_media_key(action, times)
        replies = {"play_pause": "Окей.", "next": "Следующий трек.", "previous": "Предыдущий трек.",
                   "stop": "Стоп.", "volume_up": "Громче.", "volume_down": "Тише.", "mute": "Звук переключил."}
        return replies.get(action, "Окей.") if ok else "Медиаклавиши работают только в Windows."
