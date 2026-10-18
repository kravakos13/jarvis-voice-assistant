"""Spoken key names -> key combos (1.0.8): «нажми энтер», «нажми пять», «нажми контрол цэ», «альт таб»,
«нажми эф пять», «нажми вниз три раза», «нажми ctrl shift esc», «вин дэ».

Latin letters are named the Russian way («цэ», «дэ», «эс», «би», «эй»…); Russian-only letters («нажми я»)
are typed as characters. Whisper variants of key names are accepted (энтер/ентер/интер, контрол/контрл,
эскейп/эскейб …)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

VERBS = re.compile(r"^(?:нажми|нажмите|нажми-ка|нажимай|нажать|нажал|нажала|нажмем|нажмём|нажму|нажмешь|жми|жмякни|"
                   r"жмакни|жамкни|клацни|клацай|тыцни|тыкни|ткни|кликни|клавиша|клавишу|кнопка|кнопку|"
                   r"нажми клавишу|нажми кнопку)$")
SINGLE = {
    "enter": "энтер ентер интер энтэр ентэр enter ввод return ретурн",
    "space": "пробел пробела пробельчик space спейс спэйс",
    "esc": "эскейп эскейб искейп эскап ескейп эскейпа escape esc эск отмена отмену",
    "tab": "таб таба тап tab табуляция",
    "backspace": "бэкспейс бекспейс бэкспэйс backspace стерка стирка удали удалить",
    "delete": "делит делете дилит delete del удаление",
    "insert": "инсерт insert",
    "up": "вверх наверх up",
    "down": "вниз down",
    "left": "влево налево left",
    "right": "вправо направо right",
    "home": "хоум хом home",
    "end": "энд енд end",
    "pageup": "пейджап pageup",
    "pagedown": "пейдждаун pagedown",
    "printscreen": "принтскрин printscreen",
    "capslock": "капслок capslock",
    "ctrl": "контрол контрл контроль кантрол контр ктрл ctrl control кнтрл контролл",
    "shift": "шифт шивт шифтом shift",
    "alt": "альт альтом алт олт alt",
    "win": "вин виндовс виндоус windows win винду",
}
KEY_WORDS = {w: k for k, ws in SINGLE.items() for w in ws.split()}
MULTI_KEYS = {"пейдж ап": "pageup", "пэйдж ап": "pageup", "page up": "pageup", "страница вверх": "pageup",
              "пейдж даун": "pagedown", "пэйдж даун": "pagedown", "page down": "pagedown", "страница вниз": "pagedown",
              "принт скрин": "printscreen", "print screen": "printscreen", "капс лок": "capslock",
              "caps lock": "capslock", "стрелка вверх": "up", "стрелку вверх": "up", "стрелка вниз": "down",
              "стрелку вниз": "down", "стрелка влево": "left", "стрелку влево": "left", "стрелка вправо": "right",
              "стрелку вправо": "right", "дубль вэ": "w", "дабл ю": "w"}
MULTI = sorted(MULTI_KEYS, key=len, reverse=True)
DIGITS = {"ноль": "0", "нуль": "0", "один": "1", "одну": "1", "единицу": "1", "единица": "1", "два": "2", "двойку": "2",
          "три": "3", "тройку": "3", "четыре": "4", "четверку": "4", "пять": "5", "пятерку": "5", "шесть": "6",
          "семь": "7", "восемь": "8", "девять": "9"}
F_NUM = dict(DIGITS, **{"десять": "10", "одиннадцать": "11", "двенадцать": "12"})
# Latin letters by their Russian-style names (and English names in Cyrillic)
LATIN = {
    "a": "а эй", "b": "бэ би бе", "c": "цэ це си", "d": "дэ де ди", "e": "е", "f": "эф", "g": "гэ ге джи жи",
    "h": "ха аш эйч", "i": "ай", "j": "йот джей жэй", "k": "ка кей", "l": "эль эл", "m": "эм", "n": "эн",
    "o": "о оу", "p": "пэ пе пи", "q": "ку кью", "r": "эр ар", "s": "эс", "t": "тэ те ти", "u": "у ю",
    "v": "вэ ве ви", "w": "даблъю дабл-ю", "x": "икс экс", "y": "игрек уай вай", "z": "зэт зет зед зи",
}
LETTER_WORDS = {w: k for k, ws in LATIN.items() for w in ws.split()}
RU_ONLY = set("бвгджзйклмнпрстфхцчшщъыьэюя")       # a lone Russian letter not used as a Latin name -> typed
TIMES_RE = re.compile(r"\s+(?:(\d+)|(два|две|три|четыре|пять|шесть|семь|восемь|девять|десять))\s+(?:раза|раз)$"
                      r"|\s+(дважды|трижды)$")
NUMW = {"два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
        "десять": 10}
MAX_TIMES = 20


@dataclass
class KeyPress:
    combo: str = ""          # "ctrl+c", "enter", "f5" … ('' when text is set)
    times: int = 1
    text: str = ""           # Russian-only letter to type as a character
    verb: bool = False       # «нажми» was said


def _times(t: str) -> tuple[str, int]:
    m = TIMES_RE.search(t)
    if not m:
        return t, 1
    n = int(m.group(1)) if m.group(1) else NUMW.get(m.group(2) or "", 0) or (2 if m.group(3) == "дважды" else 3)
    return t[: m.start()].strip(), max(1, min(n, MAX_TIMES))


def parse(norm: str, need_verb: bool = True) -> Optional[KeyPress]:
    t = " ".join((norm or "").lower().replace("ё", "е").replace("+", " плюс ").replace("-", " ").split())
    t = re.sub(r"^(?:ну|давай|а|джарвис)\s+", "", t)
    t, times = _times(t)
    toks = t.split()
    verb = False
    if toks and VERBS.match(toks[0]):
        verb = True
        toks = toks[1:]
        if toks and toks[0] in ("клавишу", "кнопку", "на"):
            toks = toks[1:]
    if not toks:
        return None
    keys: list[str] = []
    text = ""
    i = 0
    while i < len(toks):
        rest = " ".join(toks[i:])
        hit = next((m for m in MULTI if rest == m or rest.startswith(m + " ")), None)
        if hit:
            keys.append(MULTI_KEYS[hit])
            i += len(hit.split())
            continue
        w = toks[i]
        if w in ("плюс", "и", "со") or (w == "с" and keys):
            i += 1
            continue
        if w in ("эф", "ф", "f") and i + 1 < len(toks) and (toks[i + 1] in F_NUM or toks[i + 1].isdigit()):
            n = int(F_NUM.get(toks[i + 1], toks[i + 1]))
            if not 1 <= n <= 12:
                return None
            keys.append(f"f{n}")
            i += 2
            continue
        if m := re.fullmatch(r"[fф](\d{1,2})", w):
            n = int(m.group(1))
            if not 1 <= n <= 12:
                return None
            keys.append(f"f{n}")
            i += 1
            continue
        if w in KEY_WORDS:
            keys.append(KEY_WORDS[w])
        elif w in DIGITS:
            keys.append(DIGITS[w])
        elif re.fullmatch(r"\d", w):
            keys.append(w)
        elif w in LETTER_WORDS:
            keys.append(LETTER_WORDS[w])
        elif re.fullmatch(r"[a-z]", w):
            keys.append(w)
        elif len(w) == 1 and w in RU_ONLY and len(toks) == 1:
            text = w
        else:
            return None
        i += 1
    if text:
        return KeyPress(text=text, times=times, verb=verb) if verb or not need_verb else None
    if not keys:
        return None
    mods = [k for k in keys if k in ("ctrl", "shift", "alt", "win")]
    main = [k for k in keys if k not in ("ctrl", "shift", "alt", "win")]
    if len(main) > 1 or (not main and len(mods) != 1):
        return None                                      # «нажми а бэ» is not a key; a lone modifier is ok
    if not verb and (need_verb or not mods):
        return None                                      # without «нажми» only modifier combos («альт таб»)
    order = [m for m in ("ctrl", "shift", "alt", "win") if m in mods]
    combo = "+".join(order + main)
    return KeyPress(combo=combo, times=times, verb=verb)

