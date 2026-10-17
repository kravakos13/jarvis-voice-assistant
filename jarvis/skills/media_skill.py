"""YouTube / YouTube Music / media keys."""
from __future__ import annotations

import re
from typing import Optional

from ..media import Media
from .base import Skill, ToolResult, fn, s

YT = r"(?:ютуб\w*|youtube|ю туб\w*|утуб\w*)"
YTM = r"(?:ютуб музык\w*|youtube music|ютуб мьюзик|ютьюб музык\w*|yt music)"
NOT_MUSIC = re.compile(r"^(звук|автозагрузк\w*|автозапуск\w*|таймер\w*|секундомер\w*|режим\w*|диктовк\w*|компьютер|пк)\b")

MEDIA_CMDS = [
    (re.compile(r"^(пауза|на паузу|поставь на паузу|паузу|останови музыку|стоп музыка|продолжи|продолжай|играй|воспроизведи|сними с паузы)$"), "play_pause"),
    (re.compile(r"^(следующ\w+( трек| песн\w+| видео)?|дальше|переключи( трек| песню)?|некст)$"), "next"),
    (re.compile(r"^(предыдущ\w+( трек| песн\w+| видео)?|верни (трек|песню)|прошл\w+ (трек|песн\w+))$"), "previous"),
]


class MediaSkill(Skill):
    name = "media"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.media = Media(ctx.cfg.get("media", {}))

    def _play(self, query: str) -> ToolResult:
        return ToolResult("Включаю", then=lambda: (self.media.play_music(query), None)[1], background=True)

    def _video(self, query: str) -> ToolResult:
        return ToolResult("Включаю", then=lambda: (self.media.youtube_search(query, play_first=True), None)[1],
                          background=True)

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        for rx, action in MEDIA_CMDS:
            if rx.match(t):
                return ToolResult(self.media.media_key(action))
        if re.match(rf"^(?:открой|запусти|включи) {YTM}$", t):
            return ToolResult("Открываю ютуб музыку.", then=lambda: (self.media.open_ytmusic(), None)[1])
        if re.match(rf"^(?:открой|запусти) {YT}$", t):
            return ToolResult("Открываю ютуб.", then=lambda: (self.media.open_youtube(), None)[1])
        m = re.match(rf"^(?:найди|поищи|покажи)(?: мне)? (?:на|в) {YT} (.+)$", t) or \
            re.match(rf"^(?:найди|поищи)(?: мне)? видео (.+?)(?: на {YT})?$", t)
        if m:
            q = m.group(1)
            return ToolResult(f"Ищу на ютубе: {q}.", then=lambda: (self.media.youtube_search(q), None)[1])
        m = re.match(rf"^(?:включи|поставь|запусти)(?: мне)? (?:на {YT} |видео )(.+)$", t) or \
            re.match(rf"^(?:включи|поставь)(?: мне)? (.+?) на {YT}$", t)
        if m:
            return self._video(m.group(1))
        m = re.match(r"^(?:включи|поставь|сыграй|играй)(?: мне)?(?: песню| трек| музыку| альбом| группу| исполнителя)? ?(.*)$", t)
        if m and t.split()[0] in ("включи", "поставь", "сыграй", "играй"):
            q = m.group(1).strip()
            q = re.sub(rf"\s+(?:на|в) {YTM}$", "", q)
            if NOT_MUSIC.match(q):
                return None
            if q in ("", "музыку", "музыка", "что-нибудь", "что нибудь"):
                return ToolResult("Включаю", then=lambda: (self.media.open_ytmusic(), None)[1])
            return self._play(q)
        return None

    def tools(self) -> list[dict]:
        return [
            fn("play_music", "Включить песню, исполнителя или альбом в YouTube Music.", {"query": s("трек/исполнитель")}, ["query"]),
            fn("play_video", "Найти и включить видео на YouTube.", {"query": s("что найти")}, ["query"]),
            fn("youtube_search", "Показать результаты поиска на YouTube, не включая.", {"query": s("запрос")}, ["query"]),
            fn("media_control", "Управление воспроизведением.",
               {"action": s("действие", ["play_pause", "next", "previous", "volume_up", "volume_down", "mute"])}, ["action"]),
        ]

    def call(self, name: str, args: dict) -> Optional[ToolResult]:
        q = str(args.get("query", "")).strip()
        if name == "play_music":
            return self._play(q)
        if name == "play_video":
            return self._video(q)
        if name == "youtube_search":
            return ToolResult(f"Ищу на ютубе: {q}.", then=lambda: (self.media.youtube_search(q), None)[1])
        if name == "media_control":
            return ToolResult(self.media.media_key(str(args.get("action", "play_pause"))))
        return None
