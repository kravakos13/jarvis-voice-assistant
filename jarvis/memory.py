"""Long-term memory: facts about the user + rolling conversation summaries (SQLite).

Retrieval: cosine similarity over Ollama embeddings (nomic-embed-text) when
available, otherwise keyword overlap. Relevant items are injected into the
system prompt for every request.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import platform_utils as pu

log = logging.getLogger(__name__)

_STOP = set("и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от "
            "меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж "
            "вам ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без "
            "будто чего раз тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один "
            "почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец два об другой хоть после "
            "над больше тот через эти нас про всего них какая много разве три эту моя впрочем хорошо свою этой перед "
            "иногда лучше чуть том нельзя такой им более всегда конечно всю между это мое моё мои".split())


_ENDINGS = re.compile(r"(ами|ями|ого|его|ому|ему|ыми|ими|ах|ях|ов|ев|ей|ой|ий|ый|ая|яя|ое|ее|ую|юю|ом|ем|ам|ям|а|я|о|е|ы|и|у|ю|ь)$")


def _stem(w: str) -> str:
    """Very small Russian stemmer: 'кота', 'коту', 'кот' -> 'кот'."""
    st = _ENDINGS.sub("", w)
    return (st if len(st) >= 3 else w)[:6]


def keywords(text: str) -> set[str]:
    words = re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е"))
    return {_stem(w) for w in words if len(w) > 2 and w not in _STOP}


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class MemoryItem:
    id: int
    kind: str          # fact | summary
    text: str
    created: str
    score: float = 0.0


class Memory:
    def __init__(self, cfg: dict, llm=None, db_path: Optional[Path] = None):
        self.cfg = cfg or {}
        self.llm = llm
        self.embed_model = self.cfg.get("embed_model", "nomic-embed-text")
        self.use_embeddings = bool(self.cfg.get("use_embeddings", True))
        self.top_k = int(self.cfg.get("top_k", 5))
        self.min_score = float(self.cfg.get("min_score", 0.45))
        path = db_path or (pu.data_dir("", self.cfg.get("dir", "")) / "memory.sqlite3")
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock, self.conn:
            self.conn.execute("""CREATE TABLE IF NOT EXISTS memory(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, text TEXT NOT NULL,
                created TEXT NOT NULL, embedding TEXT)""")
        try:
            from .syncdb import ensure_sync_schema
            with self._lock:
                ensure_sync_schema(self.conn, "memory")
        except Exception as e:
            log.warning("memory sync schema: %s", e)
        self._embed_ok: Optional[bool] = None

    # ------------------------------------------------------------ embeddings
    def _embed(self, text: str) -> Optional[list[float]]:
        if not self.use_embeddings or self.llm is None or self._embed_ok is False:
            return None
        try:
            vec = self.llm.embed([text], self.embed_model)[0]
            self._embed_ok = True
            return vec
        except Exception as e:
            if self._embed_ok is None:
                log.info("Эмбеддинги недоступны (%s) - память ищет по ключевым словам. "
                         "Для умного поиска: ollama pull %s", e, self.embed_model)
            self._embed_ok = False
            return None

    # ------------------------------------------------------------ CRUD
    def add(self, text: str, kind: str = "fact") -> MemoryItem:
        text = text.strip()
        emb = self._embed(text)
        now = _dt.datetime.now().isoformat(timespec="seconds")
        with self._lock, self.conn:
            cur = self.conn.execute("INSERT INTO memory(kind,text,created,embedding) VALUES(?,?,?,?)",
                                    (kind, text, now, json.dumps(emb) if emb else None))
        return MemoryItem(int(cur.lastrowid or 0), kind, text, now)

    def update(self, item_id: int, text: str) -> bool:
        emb = self._embed(text)
        with self._lock, self.conn:
            return self.conn.execute("UPDATE memory SET text=?, embedding=? WHERE id=?",
                                     (text, json.dumps(emb) if emb else None, item_id)).rowcount > 0

    def delete(self, item_id: int) -> bool:
        with self._lock, self.conn:
            return self.conn.execute("DELETE FROM memory WHERE id=?", (item_id,)).rowcount > 0

    def clear(self, kind: Optional[str] = None) -> int:
        with self._lock, self.conn:
            if kind:
                return self.conn.execute("DELETE FROM memory WHERE kind=?", (kind,)).rowcount
            return self.conn.execute("DELETE FROM memory").rowcount

    def all(self, kind: Optional[str] = None) -> list[MemoryItem]:
        q = "SELECT id,kind,text,created FROM memory" + (" WHERE kind=?" if kind else "") + " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(q, (kind,) if kind else ()).fetchall()
        return [MemoryItem(*r) for r in rows]

    # ------------------------------------------------------------ retrieval
    def search(self, query: str, k: Optional[int] = None) -> list[MemoryItem]:
        k = k or self.top_k
        with self._lock:
            rows = self.conn.execute("SELECT id,kind,text,created,embedding FROM memory").fetchall()
        if not rows:
            return []
        qv = self._embed(query) if any(r[4] for r in rows) else None
        qk = keywords(query)
        scored = []
        for r in rows:
            item = MemoryItem(r[0], r[1], r[2], r[3])
            if qv is not None and r[4]:
                item.score = cosine(qv, json.loads(r[4]))
                ok = item.score >= self.min_score
            else:
                kw = keywords(r[2])
                inter = len(qk & kw)
                item.score = inter / (len(qk) or 1)
                ok = inter > 0
            if ok:
                scored.append(item)
        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:k]

    def find(self, ref: str) -> Optional[MemoryItem]:
        m = re.search(r"\d+", ref)
        if m:
            for it in self.all():
                if it.id == int(m.group()):
                    return it
        hits = self.search(ref, 1)
        return hits[0] if hits else None

    def context_for(self, query: str) -> str:
        """Text block injected into the system prompt."""
        facts = [m for m in self.all("fact")][-30:] if len(self.all("fact")) <= 15 else []
        hits = self.search(query)
        seen = {f.id for f in facts}
        items = facts + [h for h in hits if h.id not in seen]
        if not items:
            return ""
        lines = [("- " if it.kind == "fact" else "- (из прошлого разговора) ") + it.text for it in items]
        return "Что ты помнишь о пользователе и прошлых разговорах (используй, если к месту):\n" + "\n".join(lines)

    # ------------------------------------------------------------ summaries
    def summarize(self, messages: list[dict]) -> Optional[str]:
        if self.llm is None or len(messages) < 4:
            return None
        convo = "\n".join(f"{'Пользователь' if m['role'] == 'user' else 'Ассистент'}: {m['content'][:500]}"
                          for m in messages if m.get("role") in ("user", "assistant"))
        try:
            text = self.llm.complete([{"role": "user", "content":
                "Сожми этот разговор в 1-3 коротких предложения: о чём говорили и что важно запомнить о пользователе "
                "(предпочтения, планы, факты). Без вступлений.\n\n" + convo}], temperature=0.2)
        except Exception as e:
            log.debug("summary: %s", e)
            return None
        if text:
            self.add(text, "summary")
        return text
