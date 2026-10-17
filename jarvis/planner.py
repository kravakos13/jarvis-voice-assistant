"""Diary / task planner with reminders (SQLite + background scheduler)."""
from __future__ import annotations

import builtins
import datetime as _dt
import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import platform_utils as pu

log = logging.getLogger(__name__)

WEEKDAYS_ACC = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]

NUM_WORDS = {
    "ноль": 0, "один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
    "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40,
    "пятьдесят": 50, "полчаса": 30, "пару": 2, "пара": 2, "полтора": 1.5, "полторы": 1.5,
}


def words_to_digits(text: str) -> str:
    """'через двадцать пять минут' -> 'через 25 минут' (enough for times/durations)."""
    tokens = text.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        w = tokens[i].lower().strip(",.")
        if w in NUM_WORDS and w != "полчаса":
            val = NUM_WORDS[w]
            if val in (20, 30, 40, 50) and i + 1 < len(tokens):
                nxt = tokens[i + 1].lower().strip(",.")
                if nxt in NUM_WORDS and 1 <= NUM_WORDS[nxt] <= 9:
                    val += NUM_WORDS[nxt]
                    i += 1
            out.append(str(val).replace(".5", ".5"))
        else:
            out.append(tokens[i])
        i += 1
    return " ".join(out)


# ---------------------------------------------------------------- date parsing
_REL_RE = re.compile(r"\bчерез\s+(?:(\d+(?:\.\d+)?)\s+)?(минут\w*|мин|час\w*|дн\w*|день|недел\w*|полчаса)\b")
_DAY_RE = re.compile(r"\b(сегодня|завтра|послезавтра|в\s+(?:понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье))\b")
_TIME_RE = re.compile(r"\b(?:в|на|к)\s+(\d{1,2})(?:[:.](\d{2})|\s+(?:час\w*\s+)?(\d{1,2})\s+минут\w*)?(?:\s+час\w*)?(?:\s+(утра|дня|вечера|ночи))?\b")
_DATE_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS_GEN) + r")\b")
_FILLER_RE = re.compile(
    r"^(?:джарвис\s+)?(?:пожалуйста\s+)?(?:напомни(?:\s+мне)?|поставь\s+(?:напоминание|задачу)|добавь\s+(?:задачу|напоминание|в\s+план\w*)|"
    r"запланируй|создай\s+(?:задачу|напоминание)|задача|напоминание)\s*(?:(?:что\s+нужно|чтобы|что|про|об|о)\b)?\s*",
    re.I)


@dataclass
class ParsedTask:
    text: str
    due: Optional[_dt.datetime]


def _hour_adjust(h: int, part: Optional[str], base: _dt.datetime, day_fixed: bool) -> int:
    if part in ("вечера", "дня") and h < 12:
        return h + 12
    if part == "ночи" and h == 12:
        return 0
    return h


def parse_task(text: str, now: Optional[_dt.datetime] = None) -> ParsedTask:
    """Rule-based Russian date/time extraction. Falls back to dateparser for the rest."""
    now = now or _dt.datetime.now()
    raw = words_to_digits(text.strip())
    low = raw.lower().replace("ё", "е")
    due: Optional[_dt.datetime] = None
    spans: list[tuple[int, int]] = []

    m = _REL_RE.search(low)
    if m:
        unit = m.group(2)
        n = float(m.group(1)) if m.group(1) else 1.0
        if unit.startswith("полчаса"):
            delta = _dt.timedelta(minutes=30)
        elif unit.startswith("мин"):
            delta = _dt.timedelta(minutes=n)
        elif unit.startswith("час"):
            delta = _dt.timedelta(hours=n)
        elif unit.startswith(("дн", "день")):
            delta = _dt.timedelta(days=n)
        else:
            delta = _dt.timedelta(weeks=n)
        due = (now + delta).replace(second=0, microsecond=0)
        spans.append(m.span())
    else:
        day: Optional[_dt.date] = None
        dm = _DAY_RE.search(low)
        if dm:
            w = dm.group(1)
            if w == "сегодня":
                day = now.date()
            elif w == "завтра":
                day = now.date() + _dt.timedelta(days=1)
            elif w == "послезавтра":
                day = now.date() + _dt.timedelta(days=2)
            else:
                target = WEEKDAYS_ACC.index(w.split()[-1])
                ahead = (target - now.weekday()) % 7 or 7
                day = now.date() + _dt.timedelta(days=ahead)
            spans.append(dm.span())
        dd = _DATE_RE.search(low)
        if dd:
            d, mon = int(dd.group(1)), MONTHS_GEN.index(dd.group(2)) + 1
            try:
                cand = _dt.date(now.year, mon, d)
                if cand < now.date():
                    cand = _dt.date(now.year + 1, mon, d)
                day = cand
                spans.append(dd.span())
            except ValueError:
                pass
        tm = _TIME_RE.search(low)
        hour = minute = None
        if tm and int(tm.group(1)) < 24:
            hour = _hour_adjust(int(tm.group(1)), tm.group(4), now, day is not None)
            minute = int(tm.group(2) or tm.group(3) or 0)
            if minute > 59:
                hour = minute = None
            else:
                spans.append(tm.span())
        if day is not None or hour is not None:
            if hour is None:
                hour, minute = 9, 0       # date without time -> 9:00
            minute = minute or 0
            if day is None:
                day = now.date()
                if _dt.datetime.combine(day, _dt.time(hour, minute)) <= now and tm is not None:
                    # "в 8" at 19:00 -> 20:00 today if plausible, else tomorrow
                    if hour < 12 and not tm.group(4) and _dt.datetime.combine(day, _dt.time(hour + 12, minute)) > now:
                        hour += 12
                    else:
                        day += _dt.timedelta(days=1)
            due = _dt.datetime.combine(day, _dt.time(hour, minute))

    if due is None:
        try:
            from dateparser.search import search_dates
            found = search_dates(raw, languages=["ru"], settings={"RELATIVE_BASE": now, "PREFER_DATES_FROM": "future"})
            if found:
                frag, dt = found[0]
                due = dt.replace(second=0, microsecond=0)
                i = low.find(frag.lower())
                if i >= 0:
                    spans.append((i, i + len(frag)))
        except Exception as e:
            log.debug("dateparser: %s", e)

    # remove date fragments and filler words -> task text
    rest = raw
    for a, b in sorted(spans, reverse=True):
        rest = rest[:a] + " " + rest[b:]
    rest = re.sub(r"\s+", " ", rest).strip(" ,.")
    rest = _FILLER_RE.sub("", rest).strip(" ,.")
    rest = re.sub(r"^(?:напомни(?:\s+мне)?|что|чтобы|о|про)\s+", "", rest, flags=re.I).strip(" ,.")
    return ParsedTask(rest or text.strip(), due)


def parse_task_llm(text: str, llm, now: Optional[_dt.datetime] = None) -> ParsedTask:
    """Fallback: ask the LLM for JSON {"task": str, "due": "YYYY-MM-DD HH:MM" | null}."""
    now = now or _dt.datetime.now()
    prompt = (
        f"Сейчас {now:%Y-%m-%d %H:%M} ({['пн','вт','ср','чт','пт','сб','вс'][now.weekday()]}). "
        "Извлеки из фразы задачу и дату/время напоминания. Ответь только JSON вида "
        '{"task": "текст задачи", "due": "YYYY-MM-DD HH:MM"} или "due": null, если времени нет.\n'
        f"Фраза: {text}")
    try:
        out = llm.complete([{"role": "user", "content": prompt}], fmt="json", temperature=0)
        data = json.loads(out)
        due = data.get("due")
        due_dt = _dt.datetime.strptime(due.strip(), "%Y-%m-%d %H:%M") if due else None
        return ParsedTask(str(data.get("task") or text).strip(), due_dt)
    except Exception as e:
        log.warning("LLM-разбор даты не удался: %s", e)
        return ParsedTask(text, None)


def human_dt(dt: _dt.datetime, now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now()
    if dt.date() == now.date():
        day = "сегодня"
    elif dt.date() == now.date() + _dt.timedelta(days=1):
        day = "завтра"
    elif dt.date() == now.date() + _dt.timedelta(days=2):
        day = "послезавтра"
    else:
        day = f"{dt.day} {MONTHS_GEN[dt.month - 1]}"
    return f"{day} в {dt:%H:%M}"


# ---------------------------------------------------------------- storage
@dataclass
class Task:
    id: int
    text: str
    due: Optional[_dt.datetime]
    done: bool
    reminded: bool


class Planner:
    def __init__(self, cfg: dict, db_path: Optional[Path] = None):
        self.cfg = cfg or {}
        path = db_path or (pu.data_dir("", self.cfg.get("dir", "")) / "planner.sqlite3")
        self.db_path = Path(path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        with self._lock, self.conn:
            self.conn.execute("""CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, due TEXT,
                done INTEGER NOT NULL DEFAULT 0, reminded INTEGER NOT NULL DEFAULT 0,
                created TEXT NOT NULL)""")
            self.conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        try:
            from .syncdb import ensure_sync_schema
            with self._lock:
                ensure_sync_schema(self.conn, "tasks")
        except Exception as e:
            log.warning("planner sync schema: %s", e)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ CRUD
    @staticmethod
    def _row(r) -> Task:
        return Task(r[0], r[1], _dt.datetime.fromisoformat(r[2]) if r[2] else None, bool(r[3]), bool(r[4]))

    def add(self, text: str, due: Optional[_dt.datetime]) -> Task:
        with self._lock, self.conn:
            cur = self.conn.execute("INSERT INTO tasks(text, due, created) VALUES(?,?,?)",
                                    (text, due.isoformat(timespec="minutes") if due else None,
                                     _dt.datetime.now().isoformat(timespec="seconds")))
            return Task(int(cur.lastrowid or 0), text, due, False, False)

    def get(self, task_id: int) -> Optional[Task]:
        with self._lock:
            r = self.conn.execute("SELECT id,text,due,done,reminded FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._row(r) if r else None

    def list(self, day: Optional[_dt.date] = None, include_done: bool = False) -> builtins.list[Task]:
        q = "SELECT id,text,due,done,reminded FROM tasks"
        cond, args = [], []
        if not include_done:
            cond.append("done=0")
        if day is not None:
            cond.append("substr(due,1,10)=?")
            args.append(day.isoformat())
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY due IS NULL, due, id"
        with self._lock:
            return [self._row(r) for r in self.conn.execute(q, args).fetchall()]

    def find(self, ref: str) -> Optional[Task]:
        """Find an open task by number or by words in its text."""
        ref = ref.strip().lower()
        m = re.search(r"\d+", words_to_digits(ref))
        if m:
            t = self.get(int(m.group()))
            if t:
                return t
        words = [w for w in re.findall(r"\w+", ref) if len(w) > 2 and w not in ("задачу", "задача", "номер")]
        best, score = None, 0
        for t in self.list():
            low = t.text.lower()
            s = sum(1 for w in words if w[:5] in low)
            if s > score:
                best, score = t, s
        return best

    def set_done(self, task_id: int, done: bool = True) -> bool:
        with self._lock, self.conn:
            return self.conn.execute("UPDATE tasks SET done=? WHERE id=?", (int(done), task_id)).rowcount > 0

    def delete(self, task_id: int) -> bool:
        with self._lock, self.conn:
            return self.conn.execute("DELETE FROM tasks WHERE id=?", (task_id,)).rowcount > 0

    def due_reminders(self, now: Optional[_dt.datetime] = None) -> builtins.list[Task]:
        now = now or _dt.datetime.now()
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,text,due,done,reminded FROM tasks WHERE done=0 AND reminded=0 AND due IS NOT NULL AND due<=?",
                (now.isoformat(timespec="minutes"),)).fetchall()
        return [self._row(r) for r in rows]

    def mark_reminded(self, task_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute("UPDATE tasks SET reminded=1 WHERE id=?", (task_id,))

    def meta_get(self, key: str) -> Optional[str]:
        with self._lock:
            r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def meta_set(self, key: str, value: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))

    # ------------------------------------------------------------ spoken summaries
    def describe(self, tasks: builtins.list[Task], now: Optional[_dt.datetime] = None) -> str:
        if not tasks:
            return ""
        parts = []
        for t in tasks:
            when = f" — {t.due:%H:%M}" if t.due else ""
            parts.append(f"номер {t.id}: {t.text}{when}")
        return "; ".join(parts) + "."

    def summary_for(self, day: _dt.date, label: str) -> str:
        tasks = self.list(day)
        if not tasks:
            return f"На {label} задач нет. Можно расслабиться."
        return f"На {label} {len(tasks)} {_plural(len(tasks))}: " + self.describe(tasks)

    # ------------------------------------------------------------ scheduler
    def start_scheduler(self, on_reminder: Callable[[Task], None],
                        on_morning: Optional[Callable[[str], None]] = None, interval_s: float = 15.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        morning = str(self.cfg.get("morning_summary_time") or "").strip()

        def loop():
            while not self._stop.is_set():
                try:
                    now = _dt.datetime.now()
                    for t in self.due_reminders(now):
                        self.mark_reminded(t.id)
                        on_reminder(t)
                    if morning and on_morning:
                        hh, mm = (int(x) for x in morning.split(":"))
                        today = now.date().isoformat()
                        if now.time() >= _dt.time(hh, mm) and self.meta_get("morning_summary") != today:
                            self.meta_set("morning_summary", today)
                            if now.hour < 13:  # don't announce "morning" summary in the evening
                                on_morning("Доброе утро! " + self.summary_for(now.date(), "сегодня"))
                except Exception as e:
                    log.error("Планировщик: %s", e)
                self._stop.wait(interval_s)

        self._thread = threading.Thread(target=loop, name="jarvis-planner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def _plural(n: int) -> str:
    n100, n10 = n % 100, n % 10
    if 11 <= n100 <= 19:
        return "задач"
    if n10 == 1:
        return "задача"
    if 2 <= n10 <= 4:
        return "задачи"
    return "задач"


def reminder_text(t: Task) -> str:
    return f"Напоминаю: {t.text}."


def notify_reminder(t: Task) -> None:
    pu.notify("Джарвис: напоминание", t.text)
