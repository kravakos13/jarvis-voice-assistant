"""YouTube / YouTube Music / media keys."""
from __future__ import annotations

import re
from typing import Optional

from ..media import Media
from ..ytpick import Pick, parse_pick
from .base import Skill, ToolResult, fn, s

YT = r"(?:ютуб\w*|youtube|ю туб\w*|утуб\w*)"
YTM = r"(?:ютуб музык\w*|youtube music|ютуб мьюзик|ютьюб музык\w*|yt music)"
NOT_MUSIC = re.compile(r"^((по)?громче|(по)?тише|потише|погромче|звук|автозагрузк\w*|автозапуск\w*|таймер\w*|секундомер\w*|режим\w*|диктовк\w*|компьютер|пк)\b")

MEDIA_CMDS = [
    (re.compile(r"^(пауза|на паузу|поставь на паузу|паузу|останови музыку|стоп музыка|продолжи|продолжай|играй|воспроизведи|сними с паузы)$"), "play_pause"),
    (re.compile(r"^(следующ\w+( трек| песн\w+| видео)?|дальше|переключи( трек| песню)?|некст)$"), "next"),
    (re.compile(r"^(предыдущ\w+( трек| песн\w+| видео)?|верни (трек|песню)|прошл\w+ (трек|песн\w+))$"), "previous"),
]


MEDIA_WORDS = r"(?:музык\w*|видео|видос\w*|ролик\w*|песн\w*|трек\w*|ютуб\w*|youtube|плеер|воспроизведени\w*|звук с ютуба)"
STOP_MEDIA_RE = re.compile(rf"^(?:выключи|выруби|останови|остановись|стопни|заглуши|пауза на|поставь на паузу|"
                           rf"стоп|хватит|прекрати)(?: эт\w*)? {MEDIA_WORDS}$")
BARE_STOP_RE = re.compile(r"^(?:выключи|выруби|останови|стопни|заглуши|прекрати)(?: это| эту| его| ее| её)?$")
VOLUME_RE = [(re.compile(r"^(?:вруби|включи|сделай|давай|поставь|дай)(?: мне)? (?:по ?)?громче(?: звук)?$"), "volume_up"),
             (re.compile(r"^(?:вруби|включи|сделай|давай|поставь|дай)(?: мне)? (?:по ?)?тише(?: звук)?$"), "volume_down")]


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

    def _stop(self, bare: bool) -> Optional[ToolResult]:
        """«выключи музыку/видео», bare «выключи»: pause what is playing. play_pause is a toggle, so it is pressed
        only when sound is really playing (or when that cannot be measured and media was named explicitly)."""
        from .. import uia
        playing = uia.media_playing()
        if playing is False:
            return None if bare else ToolResult("Сейчас ничего не играет.")
        if playing is None and bare:
            return None                       # bare «выключи» without proof of media -> not ours (LLM asks)
        return ToolResult("Поставил на паузу." if self.media.media_key("play_pause") != "Медиаклавиши работают только в Windows."
                          else "Медиаклавиши работают только в Windows.")

    def _pick(self, pick: Pick) -> ToolResult:
        # reply only after the video really opened (Picker verifies the window) — no fake «Включаю»
        return ToolResult("", then=lambda: self.media.picker.open(pick), background=True)

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        pick = parse_pick(t)
        if pick and (pick.kind == "index" or (not pick.music and self.media.picker.has_context())):
            return self._pick(pick)
        for rx, action in MEDIA_CMDS:
            if rx.match(t):
                return ToolResult(self.media.media_key(action))
        for rx, action in VOLUME_RE:
            if rx.match(t):
                return ToolResult(self.media.media_key(action))
        if STOP_MEDIA_RE.match(t) or BARE_STOP_RE.match(t):
            return self._stop(bare=bool(BARE_STOP_RE.match(t)))
        if re.match(rf"^(?:открой|запусти|включи) {YTM}$", t):
            return ToolResult("Открываю ютуб музыку.", then=lambda: (self.media.open_ytmusic(), None)[1])
        if re.match(rf"^(?:открой|запусти) {YT}$", t):
            return ToolResult("Открываю ютуб.", then=lambda: (self.media.open_youtube(), None)[1])
        m = re.match(rf"^(?:найди|найти|нашел|ищи|поищи|покажи)(?: мне)? (?:на|в) {YT} (.+)$", t) or \
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
            fn("play_nth_video", "Включить N-е видео из открытого на YouTube списка (поиск, канал, плейлист).",
               {"n": {"type": "integer", "description": "номер с 1; -1 = последнее"}}, ["n"]),
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
        if name == "play_nth_video":
            try:
                n = int(args.get("n", 1))
            except (TypeError, ValueError):
                n = 1
            return self._pick(Pick("index", n if n != 0 else 1))
        if name == "media_control":
            return ToolResult(self.media.media_key(str(args.get("action", "play_pause"))))
        return None
