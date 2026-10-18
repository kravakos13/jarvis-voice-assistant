"""Fuzzy repair of short commands (1.0.8, users' log): «нашми энтер» -> «нажми энтер», «и грай» -> «играй»,
«первая вкладка открой» -> «открой первая вкладка», «пеши …» -> «пиши …», «вес назад» -> «назад».
Only for short utterances; the result must still be matched EXACTLY by a skill, so nothing is guessed."""
from __future__ import annotations

from typing import Iterator

MAX_WORDS = 6
VERBS = ("нажми", "играй", "включи", "выключи", "открой", "закрой", "пиши", "напиши", "сверни", "разверни",
         "переключись", "переключи", "вернись", "листай", "обнови", "перемотай", "поставь", "запусти", "продолжи",
         "пауза", "громче", "тише", "назад", "вперед", "вверх", "вниз", "выдели", "копируй", "вставь", "отмени",
         "сохрани", "скопируй")
# verbs that make an utterance a COMMAND: unknown short phrases starting with them get «Не понял команду»
COMMAND_VERBS = {"нажми", "сверни", "разверни", "переключись", "переключи", "вернись", "листай", "обнови",
                 "перемотай", "закрой", "выделите", "выдели", "копируй", "вставь"}
# real words that are 1 edit away from a command verb: never "repair" them («запиши заметку» must stay a note, not
# become «напиши …» and get typed into the window)
REAL_WORDS = {"запиши", "запомни", "напомни", "покажи", "скажи", "найди", "пришли", "допиши", "спиши", "впиши",
              "открою", "закрою", "включу", "выключу", "пишу", "запустил", "обновить", "ставь", "отмена", "пуск",
              "ключи", "ключ", "клички", "кличи"}           # 1.0.9: «найди ключи» is not «включи найди»
TAIL_VERBS = ("открой", "закрой", "включи", "выключи", "переключи", "переключись", "сверни", "разверни", "нажми")


def dist(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def near_verb(w: str) -> str:
    """The command verb a (mis)heard word stands for, or ''."""
    if w in REAL_WORDS:
        return ""
    if w in VERBS or len(w) < 4:
        return w if w in VERBS else ""
    lim = 1 if len(w) < 7 else 2
    best = min(VERBS, key=lambda v: dist(w, v))
    return best if dist(w, best) <= lim and abs(len(best) - len(w)) <= lim else ""


def variants(t: str) -> Iterator[str]:
    toks = (t or "").split()
    if not toks or len(toks) > MAX_WORDS:
        return
    seen = {t}

    def out(v: str):
        if v and v not in seen:
            seen.add(v)
            return v
        return None
    if len(toks) >= 2 and (v := near_verb(toks[0] + toks[1])) and toks[0] + toks[1] != toks[0]:
        if r := out(" ".join([v] + toks[2:])):                     # «и грай» -> «играй»
            yield r
    v = near_verb(toks[0])
    if v and v != toks[0]:
        if r := out(" ".join([v] + toks[1:])):                     # «нашми энтер» -> «нажми энтер»
            yield r
    last = near_verb(toks[-1])
    first_is_verb = toks[0] in VERBS or toks[0] in REAL_WORDS or bool(near_verb(toks[0]))
    if len(toks) >= 2 and last in TAIL_VERBS and not first_is_verb:
        if r := out(" ".join([last] + toks[:-1])):                 # «первая вкладка открой»
            yield r
    if len(toks) >= 2 and len(toks[0]) <= 3 and not near_verb(toks[0]):
        if r := out(" ".join(toks[1:])):                           # «вес назад» -> «назад»
            yield r


def looks_like_command(t: str) -> bool:
    toks = (t or "").split()
    if not toks or len(toks) > MAX_WORDS:
        return False
    return any((near_verb(w) or w) in COMMAND_VERBS for w in (toks[0], toks[-1]))
