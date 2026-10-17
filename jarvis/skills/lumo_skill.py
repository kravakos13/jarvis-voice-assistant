"""'Джарвис, передай Люмо ...' — only on explicit command, always with confirmation."""
from __future__ import annotations

import re
import threading
from typing import Optional

from .. import lumo
from .base import Confirm, Skill, ToolResult

SEND_RE = re.compile(r"^(?:передай|перешли|отправь|скинь)\s+(?:это\s+|эту задачу\s+)?(?:люмо|лумо|lumo)\b[\s,:]*(.*)$|"
                     r"^(?:передай|перешли|отправь|скинь)\s+(.+?)\s+(?:люмо|лумо|lumo)$")
SCREEN_RE = re.compile(r"\b(с экраном|со скриншотом|вместе с экраном|и экран|скрин экрана)\b")
FILE_RE = re.compile(r"\b(с файлом|и файл|файл тоже)\b")
SENT_TEXT = "Передал Люмо."


class LumoSkill(Skill):
    name = "lumo"

    def _history(self) -> list:
        a = self.ctx.extra.get("assistant")
        return list(getattr(getattr(a, "llm", None), "history", []) or [])

    def prepare(self, task: str, with_screen: bool = False, files: Optional[list] = None) -> ToolResult:
        if not lumo.configured(self.ctx.cfg):
            opener = self.ctx.extra.get("open_settings")      # set by the GUI; absent in text mode
            if opener:
                try:
                    opener("integrations")
                except Exception:
                    opener = None
            return ToolResult(lumo.not_configured_message(self.ctx.cfg, opening=bool(opener)))
        hist = self._history()
        if not task:     # 'отправь это Люмо' -> the last exchange
            last = next((m["content"] for m in reversed(hist) if m.get("role") == "assistant"), "")
            task = f"Посмотри это: {last}" if last else ""
        if not task and not with_screen and not files:
            return ToolResult("А что передать Люмо? Скажи, например: «передай Люмо найди дешёвые билеты».")
        try:
            from ..screen import foreground_window
            exe, title, _ = foreground_window()
        except Exception:
            exe, title = "", ""
        att_desc = (" с экраном" if with_screen else "") + (f" и файлом {files[0].split('/')[-1].split(chr(92))[-1]}" if files else "")
        short = task if len(task) <= 90 else task[:87] + "…"

        def go():
            def work():
                try:
                    atts = []
                    if with_screen:
                        atts.append(lumo.screenshot_attachment())
                    for f in files or []:
                        atts.append(lumo.file_attachment(f))
                    turns = int(self.ctx.cfg.get("lumo", {}).get("context_turns", 6))
                    lumo.send(self.ctx.cfg, lumo.build_payload(task or "(см. вложения)", hist, (exe, title), atts, turns))
                    self.ctx.say(SENT_TEXT, "normal")
                except Exception as e:
                    self.ctx.say(f"Не получилось передать Люмо: {e}", "normal")
            threading.Thread(target=work, name="jarvis-lumo", daemon=True).start()
            return ToolResult("Отправляю.")
        return ToolResult("", confirm=Confirm(f"Передаю Люмо: «{short}»{att_desc}. Отправить?", go))

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        m = SEND_RE.match(t)
        if not m:
            return None
        spoken = (m.group(1) if m.group(1) is not None else m.group(2) or "").strip()
        with_screen = bool(SCREEN_RE.search(spoken))
        spoken = SCREEN_RE.sub("", spoken)
        files = None
        if FILE_RE.search(spoken):
            spoken = FILE_RE.sub("", spoken)
            files = (self.ctx.extra.get("dropped_files") or [])[:1] or None
            if not files:
                return ToolResult("Перетащи файл в окно Джарвиса и повтори команду.")
        spoken = re.sub(r"^(это|эту задачу)\b", "", spoken).strip(" ,.")
        # keep the original casing from the raw text where possible
        idx = text.lower().find(spoken[:20]) if spoken else -1
        task = text[idx: idx + len(spoken)] if idx >= 0 else spoken
        return self.prepare(task, with_screen, files)
