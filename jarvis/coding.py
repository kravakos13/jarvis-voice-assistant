"""Code helper for Arduino / ESP32 / ESP8266 / STM32 with an Ollama code model.

Generated code is saved to Documents\\Jarvis\\Code\\<project>\\ and opened in the
default editor. Only a short summary is spoken. arduino-cli compile/upload is
offered when it is on PATH and runs only after explicit voice confirmation.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import platform_utils as pu

log = logging.getLogger(__name__)

PLATFORMS = {
    "arduino": {"label": "Arduino", "ext": ".ino", "fqbn": "arduino:avr:uno"},
    "esp32": {"label": "ESP32", "ext": ".ino", "fqbn": "esp32:esp32:esp32"},
    "esp8266": {"label": "ESP8266", "ext": ".ino", "fqbn": "esp8266:esp8266:nodemcuv2"},
    "stm32": {"label": "STM32 (HAL)", "ext": ".c", "fqbn": ""},
    "stm32duino": {"label": "STM32 (Arduino core)", "ext": ".ino", "fqbn": "STMicroelectronics:stm32:GenF1"},
}

CODE_SYSTEM = (
    "Ты опытный embedded-разработчик. Пишешь рабочий, компилируемый код для микроконтроллеров. "
    "Формат ответа строго такой: сначала одна-две фразы по-русски, что делает код и как подключить железо "
    "(пины), затем ОДИН блок кода в тройных обратных кавычках. Комментарии в коде на русском. "
    "Без лишних объяснений после кода."
)

_CODE_BLOCK_RE = re.compile(r"```[ \t]*([\w+#-]*)[^\n]*\n(.*?)```", re.S)


def detect_platform(text: str) -> Optional[str]:
    t = text.lower().replace("-", "").replace(" ", "")
    if "esp32" in t or "есп32" in t or "еспи32" in t:
        return "esp32"
    if "esp8266" in t or "8266" in t or "nodemcu" in t or "нодемсу" in t or "wemos" in t:
        return "esp8266"
    if "stm32" in t or "стм32" in t or "stm" in t or "стм" in t or "bluepill" in t:
        return "stm32duino" if ("ардуин" in t or "arduino" in t) else "stm32"
    if "ардуин" in t or "arduino" in t or "скетч" in t:
        return "arduino"
    return None


def extract_code(answer: str) -> tuple[str, str]:
    """-> (summary_text, code). If no fenced block, whole answer is code."""
    m = _CODE_BLOCK_RE.search(answer)
    if not m:
        return "", answer.strip()
    summary = (answer[:m.start()] + " " + answer[m.end():]).strip()
    summary = re.sub(r"\s+", " ", re.sub(r"[*#`]", "", summary))
    return summary, m.group(2).strip() + "\n"


def short_summary(summary: str, max_sentences: int = 2) -> str:
    parts = re.split(r"(?<=[.!?])\s+", summary.strip())
    return " ".join(parts[:max_sentences]).strip()


@dataclass
class CodeProject:
    name: str
    platform: str
    path: Path
    code: str
    request: str
    history: list = field(default_factory=list)


class CodeHelper:
    def __init__(self, cfg: dict, llm):
        self.cfg = cfg or {}
        self.llm = llm
        self.model = self.cfg.get("model", "qwen2.5-coder:7b")
        self.dir = pu.data_dir("Code", self.cfg.get("dir", ""))
        self.open_after = bool(self.cfg.get("open_in_editor", True))
        self.fqbn = {**{k: v["fqbn"] for k, v in PLATFORMS.items()}, **(self.cfg.get("fqbn") or {})}
        self.port = self.cfg.get("port", "")
        self.current: Optional[CodeProject] = None

    @property
    def arduino_cli(self) -> Optional[str]:
        return shutil.which(self.cfg.get("arduino_cli", "arduino-cli"))

    def _model(self) -> str:
        if self.llm.has_model(self.model):
            return self.model
        log.warning("Модель для кода %s не найдена, использую %s", self.model, self.llm.model)
        return self.llm.model

    def generate(self, request: str, platform: Optional[str] = None, project: Optional[str] = None) -> tuple[str, CodeProject]:
        """Create new code (or modify the current project if it is a follow-up). Returns (spoken reply, project)."""
        platform = platform or detect_platform(request) or (self.current.platform if self.current else "arduino")
        info = PLATFORMS.get(platform, PLATFORMS["arduino"])
        follow_up = self.current is not None and project is None and detect_platform(request) in (None, self.current.platform) \
            and re.search(r"\b(измени|добавь|исправь|переделай|убери|допиши|сделай чтобы|теперь)\b", request.lower())
        messages = [{"role": "system", "content": CODE_SYSTEM}]
        if follow_up:
            messages += self.current.history[-4:]
            messages.append({"role": "user", "content": f"Измени этот код: {request}. Верни полный исправленный код."})
        else:
            messages.append({"role": "user", "content": f"Платформа: {info['label']}. Задача: {request}"})
        answer = self.llm.complete(messages, model=self._model(), temperature=0.2, num_ctx=8192)
        summary, code = extract_code(answer)
        if follow_up:
            proj = self.current
            proj.code = code
        else:
            name = pu.slugify(project or _topic(request), 32)
            folder = self.dir / name
            if folder.exists():
                folder = self.dir / f"{name}_{_dt.datetime.now():%H%M%S}"
                name = folder.name
            folder.mkdir(parents=True, exist_ok=True)
            main = folder / (f"{name}{info['ext']}" if info["ext"] == ".ino" else "main.c")
            proj = CodeProject(name, platform, main, code, request)
        proj.path.write_text(code, encoding="utf-8")
        proj.history += [{"role": "user", "content": request}, {"role": "assistant", "content": answer}]
        self.current = proj
        if self.open_after:
            pu.open_path(proj.path)
        reply = short_summary(summary) or f"Код для {info['label']} готов."
        reply = f"{reply} Сохранил в папку {proj.name}."
        if self.arduino_cli and proj.path.suffix == ".ino":
            reply += " Если хочешь, скажи «скомпилируй» или «загрузи на плату»."
        return reply, proj

    # ------------------------------------------------------------ arduino-cli (explicit confirmation only)
    def _detect_port(self) -> str:
        if self.port:
            return self.port
        try:
            out = subprocess.run([self.arduino_cli, "board", "list", "--format", "json"],
                                 capture_output=True, text=True, timeout=30).stdout
            data = json.loads(out or "[]")
            ports = data.get("detected_ports", data) if isinstance(data, dict) else data
            for p in ports:
                addr = (p.get("port") or {}).get("address") or p.get("address")
                if addr and (p.get("matching_boards") or p.get("boards") or "USB" in str(p)):
                    return addr
        except Exception as e:
            log.warning("arduino-cli board list: %s", e)
        return ""

    def compile_upload(self, upload: bool) -> str:
        if not self.current or self.current.path.suffix != ".ino":
            return "Нечего компилировать: сначала попроси написать скетч."
        cli = self.arduino_cli
        if not cli:
            return "arduino-cli не найден в PATH. Установи его, если хочешь компилировать голосом."
        fqbn = self.fqbn.get(self.current.platform) or ""
        if not fqbn:
            return "Для этой платы не задан FQBN в настройках."
        sketch = str(self.current.path.parent)
        r = subprocess.run([cli, "compile", "--fqbn", fqbn, sketch], capture_output=True, text=True, timeout=600)
        log_path = self.current.path.parent / "build.log"
        log_path.write_text(r.stdout + "\n" + r.stderr, encoding="utf-8")
        if r.returncode != 0:
            return "Компиляция упала. Лог ошибок сохранил в build.log рядом со скетчем."
        if not upload:
            return "Скомпилировалось без ошибок."
        port = self._detect_port()
        if not port:
            return "Скомпилировал, но не нашёл плату. Укажи порт в настройках code.port."
        r = subprocess.run([cli, "upload", "-p", port, "--fqbn", fqbn, sketch], capture_output=True, text=True, timeout=600)
        log_path.write_text(log_path.read_text(encoding="utf-8") + "\n" + r.stdout + r.stderr, encoding="utf-8")
        return f"Залил прошивку на {port}." if r.returncode == 0 else "Загрузка не удалась, подробности в build.log."


def _topic(request: str) -> str:
    words = re.sub(r"\b(напиши|сделай|создай|код|скетч|прошивку|программу|для|на|под|мне|пожалуйста|ардуино|arduino|esp32|esp8266|stm32|есп32|стм32)\b",
                   " ", request.lower())
    words = " ".join(words.split()[:4])
    return words or "sketch"
