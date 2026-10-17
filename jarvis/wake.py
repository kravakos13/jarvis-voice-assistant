"""Wake word detection on transcribed text.

Simplest robust offline approach: every phrase is transcribed by Whisper,
then we look for one of the name variants near the beginning of the phrase.
Variants (declensions, common mis-transcriptions, Latin spelling) are
generated automatically from the single configured assistant name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_PUNCT_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}


def normalize(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def translit(text: str) -> str:
    return "".join(_TRANSLIT.get(c, c) for c in text.lower())


def declensions(word: str) -> set[str]:
    """Rough Russian noun declension for a (masculine/feminine) name."""
    w = word.lower()
    out = {w}
    if not re.search(r"[а-я]", w):
        return out
    if w[-1] in "оеиуэю":                      # indeclinable: Люмо, Алексо...
        return out
    if w.endswith("а"):
        stem = w[:-1]
        out |= {stem + e for e in ("ы", "и", "е", "у", "ой", "ою")}
    elif w.endswith("я"):
        stem = w[:-1]
        out |= {stem + e for e in ("и", "е", "ю", "ей", "ею")}
    elif w.endswith(("й", "ь")):
        stem = w[:-1]
        out |= {stem + e for e in ("я", "ю", "ем", "е", "и", "ей")}
    else:                                       # consonant: Джарвис -> Джарвиса, Джарвису...
        out |= {w + e for e in ("а", "у", "ом", "е")}
    return out


# Typical Whisper confusions for Russian names
_SOUND_SWAPS = [("дж", "ж"), ("ж", "дж"), ("с", "з"), ("з", "с"), ("а", "е"), ("е", "а"), ("ю", "у"),
                ("у", "ю"), ("и", "е"), ("о", "а"), ("ер", "ар"), ("ар", "ер"), ("в", "ф")]


def generate_variants(name: str, extra: list[str] | None = None) -> list[str]:
    base = normalize(name)
    variants: set[str] = set()
    for w in [base] + [normalize(x) for x in (extra or [])]:
        if not w:
            continue
        variants |= declensions(w)
        # single sound swaps (only for Cyrillic names)
        if re.search(r"[а-я]", w):
            for a, b in _SOUND_SWAPS:
                if a in w and not (a == "ж" and "дж" in w):
                    variants.add(w.replace(a, b, 1))
            variants.add(translit(w))
    variants.add(translit(base))
    return sorted(v for v in variants if len(v) >= 3)


@dataclass
class WakeResult:
    detected: bool
    command: str = ""   # text after the wake word (may be empty)


class WakeWordDetector:
    def __init__(self, wake_words: list[str], max_position: int = 3):
        words = {normalize(w) for w in wake_words if normalize(w)}
        if not words:
            raise ValueError("wake_words list is empty")
        self.wake_words = sorted(words, key=len, reverse=True)   # longest first
        # wake word must be among the first N words (e.g. "эй, джарвис, ...")
        self.max_position = max_position

    @classmethod
    def from_name(cls, name: str, extra: list[str] | None = None) -> "WakeWordDetector":
        return cls(generate_variants(name, extra))

    def detect(self, text: str) -> WakeResult:
        original_tokens = (text or "").split()
        tokens = [normalize(t) for t in original_tokens]
        for i, tok in enumerate(tokens[: self.max_position + 1]):
            if not tok:
                continue
            for w in self.wake_words:
                if tok == w or (len(w) >= 4 and tok.startswith(w) and len(tok) - len(w) <= 2):
                    rest = " ".join(original_tokens[i + 1:])
                    rest = rest.lstrip(" ,.!?:;-—").strip()
                    return WakeResult(True, rest)
        return WakeResult(False, "")
