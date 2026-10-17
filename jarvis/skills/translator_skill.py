"""Voice translator: one-off 'переведи на английский ...' and a conversation mode."""
from __future__ import annotations

import re
from typing import Optional

from .base import Skill, ToolResult

LANGS = {  # name variants -> (code, name for the LLM, Piper voice)
    "английский": ("en", "английский", "en_US-ryan-medium"), "англ": ("en", "английский", "en_US-ryan-medium"),
    "немецкий": ("de", "немецкий", "de_DE-thorsten-medium"), "украинский": ("uk", "украинский", "uk_UA-ukrainian_tts-medium"),
    "русский": ("ru", "русский", None),
}
ONE_RE = re.compile(r"^(?:переведи|как будет|как сказать)\s+(?:на\s+)?(английск\w*|немецк\w*|украинск\w*|русск\w*)\s*[:,-]?\s*(.+)$")
ONE2_RE = re.compile(r"^(?:как будет|как сказать|переведи)\s+(.+?)\s+(?:на|по)\s*-?\s*(английск\w*|немецк\w*|украинск\w*)$")
MODE_ON = re.compile(r"\b(режим переводчика|будь переводчиком|переводи разговор|синхронн\w+ перевод)\b(?:\s+на\s+(\w+))?")
MODE_OFF = re.compile(r"\b(стоп перевод|хватит переводить|выйди из режима переводчика|выключи переводчик|конец перевода)\b")


def _lang(word: str):
    for k, v in LANGS.items():
        if word.startswith(k[:5]):
            return v
    return LANGS["английский"]


class TranslatorSkill(Skill):
    name = "translator"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.target: Optional[tuple] = None   # conversation mode target language

    @property
    def awaiting(self) -> bool:
        return self.target is not None

    @property
    def stt_language(self):
        return "auto" if self.target else "default"

    def translate(self, text: str, to_name: str) -> str:
        return self.ctx.llm.complete([
            {"role": "system", "content": "Ты профессиональный переводчик. Переводи естественно, разговорно, сохраняя "
                                          "смысл, тон и мат, если он есть. Выводи ТОЛЬКО перевод, без кавычек и пояснений."},
            {"role": "user", "content": f"Переведи на {to_name}:\n{text}"}], temperature=0.2).strip().strip('"«»')

    def _one(self, text: str, lang: tuple) -> ToolResult:
        code, name, voice = lang
        out = self.translate(text, name)
        return ToolResult(out, voice=voice)

    def handle_modal(self, text: str, t: str) -> Optional[ToolResult]:
        if MODE_OFF.search(t):
            self.target = None
            return ToolResult("Переводчик выключен.")
        cyr = len(re.findall(r"[а-яё]", text.lower())) > len(re.findall(r"[a-z]", text.lower()))
        if cyr and self.target[0] != "uk":
            return self._one(text, self.target)
        return self._one(text, LANGS["русский"])

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if re.search(r"буфер", t):
            return None
        m = MODE_ON.search(t)
        if m:
            self.target = _lang(m.group(2) or "английский")
            return ToolResult(f"Режим переводчика: русский и {self.target[1]}. Говори — я перевожу. "
                              f"Чтобы закончить, скажи «стоп перевод».")
        m = ONE_RE.match(t)
        if m:
            src = text[len(text) - len(m.group(2)):] if len(m.group(2)) <= len(text) else m.group(2)
            return self._one(src.strip(), _lang(m.group(1)))
        m = ONE2_RE.match(t)
        if m:
            return self._one(m.group(1), _lang(m.group(2)))
        return None
