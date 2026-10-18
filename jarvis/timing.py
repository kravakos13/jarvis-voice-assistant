"""Per-request latency trace: end of speech -> STT -> routing -> LLM -> first sound.

One Trace per command. Stages are recorded as they happen, from any thread (TTS runs on its own).
On finish a single log line is written, e.g.

    ⏱ «открой ютуб» быстрый путь (media): распознавание 0.31 с · маршрут 1 мс · до звука 0.52 с · всего 1.4 с
    ⏱ «расскажи анекдот» ИИ: распознавание 0.35 с · маршрут 2 мс · ИИ 1-й токен 0.61 с (промпт 41 ток. 0.08 с,
       генерация 28 ток/с) · до звука 1.30 с · всего 4.9 с

and the trace is kept in a small ring buffer for diagnostics ("Скорость ответа")."""
from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque
from typing import Callable, Optional

log = logging.getLogger("jarvis.timing")

LABELS = {"stt": "распознавание", "route": "маршрут", "llm_first": "ИИ 1-й токен", "llm_done": "ИИ всего",
          "first_audio": "до звука", "total": "всего"}


class Trace:
    def __init__(self, text: str = "", source: str = "text", clock: Callable[[], float] = time.monotonic,
                 t0: Optional[float] = None):
        self.clock = clock
        self.t0 = clock() if t0 is None else t0
        self.text, self.source = text, source
        self.path = ""                     # skill name (fast path) or "llm"
        self.marks: dict[str, float] = {}  # stage -> seconds since t0 (first time only)
        self.llm_stats: dict = {}
        self._lock = threading.Lock()

    def mark(self, stage: str) -> None:
        with self._lock:
            if stage not in self.marks:
                self.marks[stage] = self.clock() - self.t0

    def add(self, stage: str, seconds: float) -> None:
        with self._lock:
            self.marks[stage] = max(0.0, float(seconds))

    def get(self, stage: str) -> Optional[float]:
        return self.marks.get(stage)

    def summary(self) -> str:
        def f(x: float) -> str:
            return f"{x * 1000:.0f} мс" if x < 0.1 else f"{x:.2f} с"
        parts = []
        for k in ("stt", "route", "llm_first", "llm_done", "first_audio", "total"):
            if k in self.marks:
                s = f"{LABELS[k]} {f(self.marks[k])}"
                if k == "llm_first" and self.llm_stats:
                    st = self.llm_stats
                    extra = [f"промпт {st.get('prompt_tokens', 0)} ток. {st.get('prompt_s', 0):.2f} с"]
                    if st.get("load_s", 0) > 0.5:
                        extra.insert(0, f"загрузка модели {st['load_s']:.1f} с")
                    if st.get("tok_s"):
                        extra.append(f"генерация {st['tok_s']:.0f} ток/с")
                    s += f" ({', '.join(extra)})"
                parts.append(s)
        where = "ИИ" if self.path == "llm" else (f"быстрый путь ({self.path})" if self.path else "—")
        return f"«{self.text[:40]}» {where}: " + " · ".join(parts)


class Timings:
    """Ring buffer of finished traces + aggregate for diagnostics."""

    def __init__(self, keep: int = 50):
        self.items: deque = deque(maxlen=keep)

    def finish(self, tr: Trace) -> Trace:
        tr.mark("total")
        self.items.append(tr)
        log.info("⏱ %s", tr.summary())
        return tr

    def report(self) -> Optional[str]:
        """Short Russian summary of recent commands (median), or None if nothing measured yet."""
        if not self.items:
            return None
        fast = [t for t in self.items if t.path and t.path != "llm" and t.get("first_audio") is not None]
        slow = [t for t in self.items if t.path == "llm" and t.get("first_audio") is not None]
        stt = [t.get("stt") for t in self.items if t.get("stt") is not None]
        out = []
        if stt:
            out.append(f"распознавание ~{statistics.median(stt):.2f} с")
        if fast:
            out.append(f"простые команды ~{statistics.median(t.get('first_audio') for t in fast):.1f} с до ответа")
        if slow:
            out.append(f"вопросы к ИИ ~{statistics.median(t.get('first_audio') for t in slow):.1f} с до ответа")
            tok = [t.llm_stats.get("tok_s") for t in slow if t.llm_stats.get("tok_s")]
            if tok:
                out.append(f"скорость ИИ ~{statistics.median(tok):.0f} ток/с")
            loads = [t for t in slow if t.llm_stats.get("load_s", 0) > 2]
            if loads:
                out.append(f"модель загружалась заново {len(loads)} раз(а) из {len(slow)}")
        return f"За последние {len(self.items)} команд: " + ", ".join(out) + "." if out else None
