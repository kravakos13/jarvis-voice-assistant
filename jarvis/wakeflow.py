"""Strict wake-word flow (assistant.mode = "wake").

    SLEEPING --"Джарвис"--> "Слушаю" -> LISTENING one request -> THINKING (execute + answer) -> SLEEPING
    SLEEPING --"Джарвис, <команда>"--> THINKING directly (no "Слушаю")

While SLEEPING every phrase without the name is ignored. Nothing is listened to after the answer
unless the user turned on assistant.follow_up_seconds (off by default) or a skill explicitly waits
for an answer (e.g. "Отправить?" confirmations, translator conversation mode).
If nothing is said within request_timeout_s after "Слушаю", Jarvis goes back to sleep silently.

Pure logic: audio, Whisper, TTS and execution are injected, so the flow is unit tested
without a microphone (tests/test_wakeflow.py)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Any, Callable

log = logging.getLogger("jarvis.wake")

SLEEPING, LISTENING, THINKING = "sleeping", "listening", "thinking"


@dataclass
class WakeFlow:
    detect: Callable[[str], Any]                         # WakeWordDetector.detect -> .detected/.command
    listen: Callable[..., Any]                           # listen(timeout=, silence_ms=, max_s=) -> audio|None
    transcribe: Callable[[Any], str]
    say: Callable[[str], None]                           # blocking: returns after TTS finished
    execute: Callable[[str], None]                       # run one request and speak the answer (blocking)
    emit: Callable[[str], None] = lambda state: None     # GUI/tray state
    awaiting: Callable[[], bool] = lambda: False         # a skill waits for an answer without the name
    awaiting_timeout: Callable[[], Optional[float]] = lambda: None   # e.g. dictation: re-check after its idle limit
    cfg: dict = field(default_factory=dict)              # the "assistant" config section
    follow_allowed: Callable[[], bool] = lambda: True    # False in the game profile
    noname: Callable[[str], bool] = lambda text: False   # 1.0.8: ran it as a command without the name?
    clock: Callable[[], float] = time.monotonic
    follow_until: float = 0.0
    state: str = SLEEPING

    # ---- settings
    @property
    def follow_up(self) -> float:
        try:
            return max(0.0, float(self.cfg.get("follow_up_seconds", 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    def _num(self, key: str, default: float) -> float:
        try:
            return float(self.cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    def _set(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.emit(state)

    # ---- one cycle
    def step(self) -> str:
        """Wait for one phrase and handle it. Returns what happened (used by tests and logs):
        'idle' (nothing heard), 'ignored' (speech without the name), 'direct' (name + command),
        'request' (name, "Слушаю", request), 'no_request' (name, then silence -> back to sleep),
        'reply' (answer to a skill question / follow-up window)."""
        self._set(SLEEPING)
        now = self.clock()
        awaiting = self.awaiting()
        open_window = awaiting or now < self.follow_until
        timeout = self.awaiting_timeout() if awaiting else (self.follow_until - now if open_window else None)
        audio = self.listen(timeout=timeout)
        if audio is None:
            self.follow_until = 0.0
            return "idle"
        text = (self.transcribe(audio) or "").strip()
        if not text:
            return "idle"
        wr = self.detect(text)
        if wr.detected and wr.command.strip():
            self._run(wr.command.strip())
            return "direct"
        if wr.detected:
            return self._ask()
        if open_window:
            self._run(text)
            return "reply"
        try:
            if self.noname(text):
                return "noname"
        except Exception as e:                           # noqa: BLE001
            log.exception("no-name: %s", e)
        log.debug("(без имени, сплю дальше): %s", text)
        self.follow_until = 0.0
        return "ignored"

    def _ask(self) -> str:
        self.say(self.cfg.get("wake_reply") or "Слушаю")
        self._set(LISTENING)
        audio = self.listen(timeout=self._num("request_timeout_s", 6.0),
                            silence_ms=int(self._num("request_silence_ms", 1200)),
                            max_s=self._num("max_request_s", 15.0))
        text = (self.transcribe(audio) or "").strip() if audio is not None else ""
        if text:
            wr = self.detect(text)                       # "Слушаю" -> "Джарвис, открой ютуб": drop the name again
            if wr.detected:
                text = wr.command.strip()
        if not text:
            log.info("После «Слушаю» ничего не сказали — снова сплю")
            self._set(SLEEPING)
            return "no_request"
        self._run(text)
        return "request"

    def _run(self, command: str) -> None:
        self._set(THINKING)
        try:
            self.execute(command)
        finally:
            fu = self.follow_up
            self.follow_until = self.clock() + fu if fu > 0 and self.follow_allowed() else 0.0
            self._set(SLEEPING)


def run(flow: WakeFlow, keep_going: Callable[[], bool]) -> None:
    while keep_going():
        flow.step()
