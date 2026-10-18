"""Conversation mode (1.0.8, «Режим беседы»): «давай поболтаем», «расскажи что-нибудь», «как дела»,
«расскажи интересный факт / историю / анекдот» (with the name) start it. While it is on, phrases WITHOUT the name
are answered too (like dictation), the LLM keeps the multi-turn context and answers livelier (2-5 sentences,
sometimes a short counter-question). Ends on «хватит болтать», «пока», «спасибо, всё», «тихо» or after ~22 s of
silence -> strict wake-word mode again. Commands («пауза», «открой ютуб») keep working inside it.

Background talk is filtered: Whisper's hallucination filters apply as always, plus low-confidence or one-word
noise is ignored silently. The mic stays muted while Jarvis speaks (no barge-in: without echo cancellation a
loudspeaker setup would hear itself)."""
from __future__ import annotations

import logging
import re
import time
from typing import Callable, Optional

from .base import Skill, ToolResult

log = logging.getLogger(__name__)

START_RE = re.compile(
    r"^(?:(?:ну|а|слушай|давай)\s+)*(?:давай\s+)?(?:поболтаем|поговорим|побеседуем|потрещим|поболтать|поговорить)"
    r"(?:\s+(?:со мной|немного|чуть|просто|о чем[- ]нибудь|о чем то|за жизнь))*$"
    r"|^(?:поговори|поболтай)\s+со мной(?:\s+немного)?$"
    r"|^расскажи(?:\s+мне)?\s+(?:что[- ]нибудь|что то|чего[- ]нибудь)(?:\s+(?:интересное|интересного|смешное|"
    r"новенькое))?$"
    r"|^расскажи(?:\s+мне)?(?:\s+(?:какой|какую|какой[- ]нибудь|какую[- ]нибудь|один|одну))?\s+"
    r"(?:интересный факт|факт|интересную историю|историю|анекдот|шутку|байку|прикол)(?:\s+.{0,40})?$"
    r"|^как (?:дела|ты|жизнь|поживаешь|настроение|сам)(?:\s+(?:вообще|сегодня|у тебя))*$"
    r"|^(?:включи |режим )?(?:режим беседы|беседа)$")
STOP_RE = re.compile(r"^(?:(?:ну|ладно|ок|окей)\s+)*(?:хватит болтать|хватит|пока|пока пока|спасибо все|спасибо это все|"
                     r"все спасибо|тихо|помолчи|замолчи|закончим|давай закончим|конец беседы|выключи режим беседы|"
                     r"stop|стоп болтать|на этом все|все хватит)$")
SHORT_OK = {"да", "нет", "ага", "угу", "конечно", "давай", "ещё", "еще", "почему", "зачем", "серьезно", "серьёзно",
            "правда", "круто", "класс", "смешно", "интересно", "ого", "ну", "дальше", "продолжай", "расскажи"}
STYLE = ("Сейчас режим беседы: болтай живо и по-дружески, 2–5 предложений, с юмором; иногда (не каждый раз) "
         "закончи коротким встречным вопросом, чтобы разговор шёл дальше. Только по-русски.")
IDLE_S = 22.0
MIN_LOGPROB = -0.8


class ChatSkill(Skill):
    name = "chat"

    def __init__(self, ctx, clock: Callable[[], float] = time.monotonic):
        super().__init__(ctx)
        a = (ctx.cfg.get("assistant") or {}).get("conversation") or {}
        self.enabled = bool(a.get("enabled", True)) if isinstance(a, dict) else bool(a)
        self.idle_s = float((a.get("idle_s") if isinstance(a, dict) else None) or IDLE_S)
        self.clock = clock
        self.active = False
        self.last = 0.0

    # ---- modal API used by the router / wake flow
    @property
    def awaiting(self) -> bool:
        if self.active and self.clock() - self.last > self.idle_s:
            self._stop("тишина")
        return self.active

    @property
    def awaiting_timeout(self) -> Optional[float]:
        return max(1.0, self.idle_s - (self.clock() - self.last)) if self.active else None

    def after_turn(self) -> None:
        """Called when Jarvis finished answering: the silence timer starts after his answer, not the question."""
        if self.active:
            self.last = self.clock()

    def _style(self, on: bool) -> None:
        llm = self.ctx.llm
        if llm is not None:
            llm.style_hint = STYLE if on else ""  # type: ignore[attr-defined]

    def _start(self) -> None:
        self.active, self.last = True, self.clock()
        self._style(True)
        log.info("Режим беседы: включён")

    def _stop(self, why: str) -> None:
        self.active = False
        self._style(False)
        log.info("Режим беседы: выключен (%s)", why)

    def handle_modal(self, text: str, t: str) -> Optional[ToolResult]:
        self.last = self.clock()
        if STOP_RE.match(t):
            self._stop("по просьбе")
            return ToolResult("Ладно, если что — зови.")
        conf_fn = self.ctx.extra.get("stt_conf")
        conf = conf_fn() if callable(conf_fn) else None
        words = t.split()
        if conf is not None and (conf[0] < MIN_LOGPROB or conf[1] > 0.5):
            log.debug("Беседа: неуверенно распознано, пропускаю %r (conf=%s)", text, conf)
            return ToolResult("")
        if len(words) == 1 and words[0] not in SHORT_OK and len(words[0]) < 5:
            log.debug("Беседа: короткий шум, пропускаю %r", text)
            return ToolResult("")
        return None                       # normal routing: fast-path commands, else the LLM with context

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if self.enabled and not self.active and START_RE.match(t):
            self._start()
        return None                       # the LLM answers the opening phrase itself (with STYLE)
