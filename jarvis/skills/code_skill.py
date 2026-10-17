"""Code helper for Arduino / ESP32 / ESP8266 / STM32."""
from __future__ import annotations

import re
from typing import Optional

from ..coding import CodeHelper, detect_platform
from .base import Confirm, Skill, ToolResult, fn, s

CODE_RE = re.compile(r"\b(напиши|сделай|создай|сгенерируй|набросай)\b.*\b(скетч\w*|прошивк\w*|код\w*|программ\w*)\b")
MODE_ON = re.compile(r"\b(режим (программиста|кода|кодинга)|включи режим (программиста|кода))\b")
MODE_OFF = re.compile(r"\b(выйди из режима|выключи режим (программиста|кода)|хватит кодить|обычный режим)\b")
EDIT_RE = re.compile(r"^(измени|добавь|исправь|переделай|убери|допиши|теперь)\b")
COMPILE_RE = re.compile(r"\b(скомпилируй|компилируй|собери (скетч|прошивку|код))\b")
UPLOAD_RE = re.compile(r"\b(загрузи|залей|прошей|зашей)\b.*\b(плат\w*|контроллер\w*|прошивк\w*|скетч|ардуин\w*|esp\w*)?")


class CodeSkill(Skill):
    name = "code"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.helper = CodeHelper(ctx.cfg.get("code", {}), ctx.llm)
        self.mode = False

    def _gen(self, request: str) -> ToolResult:
        def work():
            reply, _ = self.helper.generate(request)
            return reply
        return ToolResult("Окей, пишу код, дай минутку.", then=work)

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if MODE_OFF.search(t) and self.mode:
            self.mode = False
            return ToolResult("Вышел из режима программиста.")
        if MODE_ON.search(t):
            self.mode = True
            return ToolResult("Режим программиста. Говори, что написать: скетч для ардуино, ESP32 или STM32.")
        if COMPILE_RE.search(t) or (self.helper.current and UPLOAD_RE.search(t) and re.match(r"^(загрузи|залей|прошей|зашей)", t)):
            upload = bool(re.search(r"загрузи|залей|прошей|зашей", t))
            what = "скомпилировать и загрузить на плату" if upload else "скомпилировать"
            return ToolResult("", confirm=Confirm(
                f"Точно {what} проект {self.helper.current.name if self.helper.current else ''}? Скажи да или нет.",
                on_yes=lambda: ToolResult(self.helper.compile_upload(upload))))
        is_code = CODE_RE.search(t) and (detect_platform(t) or re.search(r"скетч|прошивк|микроконтроллер", t))
        if is_code or (self.mode and len(t.split()) > 2) or (self.helper.current and self.mode and EDIT_RE.match(t)):
            return self._gen(text)
        return None

    def tools(self):
        return [fn("write_firmware", "Написать код/скетч для Arduino, ESP32, ESP8266 или STM32 и сохранить в файл.",
                   {"request": s("что должен делать код, включая платформу")}, ["request"])]

    def call(self, name, args):
        if name == "write_firmware":
            return self._gen(str(args.get("request", "")))
        return None
