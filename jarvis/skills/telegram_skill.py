"""Telegram voice commands. Sending ALWAYS requires spoken confirmation."""
from __future__ import annotations

import re
from typing import Optional

from ..launcher import phonetic, score
from ..telegram_client import TelegramService
from .base import Confirm, Skill, ToolResult

TG = r"(?:телеграм\w*|телеге|телегу|телега|телеграмме|тг)"
NEWS_RE = re.compile(rf"\b(что нового|новые сообщения|непрочитанн\w*|кто писал|кто мне писал|прочитай сообщения)\b.*(?:{TG})?")
FROM_RE = re.compile(r"\b(?:прочитай|покажи|что пишет|что написал\w*)\b(?: последние)?(?: сообщения| переписку)?\s+(?:от|с)?\s*(.+?)(?:\s+(?:в|из)\s+" + TG + r")?$")
SEND_RE = re.compile(rf"^(?:напиши|отправь|ответь|скажи)\s+(?:в {TG}\s+)?(.+?)(?:\s+в {TG})?(?:[,:]\s*|\s+(?:что|чтобы)\s+)(.+)$")
SEND_NO_TEXT_RE = re.compile(rf"^(?:напиши|отправь сообщение|ответь)\s+(?:в {TG}\s+)?(\S+(?:\s\S+)?)(?:\s+в {TG})?$")


class TelegramSkill(Skill):
    name = "telegram"

    def __init__(self, ctx, service: Optional[TelegramService] = None):
        super().__init__(ctx)
        self.cfg = ctx.cfg.get("telegram", {})
        self.svc = service or TelegramService(self.cfg)
        self.whitelist = [x.lower() for x in self.cfg.get("notify_whitelist", [])]
        self.muted = False
        if service is None:
            notify = bool(self.cfg.get("notify_new_messages", False))
            self.svc.start(self._on_new if notify else None)

    def _on_new(self, m):
        if self.muted:
            return
        if self.whitelist and not any(w in m.chat.lower() or w in m.sender.lower() for w in self.whitelist):
            return
        self.ctx.say(f"Новое сообщение в телеграме от {m.sender}: {m.text[:200]}")

    def find_dialog(self, name: str):
        best, best_s = None, 0.0
        qk = phonetic(name)
        for d in self.svc.dialogs():
            sc = score(qk, phonetic(d.name))
            if sc > best_s:
                best, best_s = d, sc
        return best if best_s >= 0.68 else None

    def _confirm_send(self, name: str, text: str) -> ToolResult:
        d = self.find_dialog(name)
        if not d:
            return ToolResult(f"Не нашёл в телеграме контакт «{name}».")
        def do():
            self.svc.send(d.id, text)
            return ToolResult("Отправил.")
        return ToolResult("", confirm=Confirm(f"Отправить {d.name} в телеграм: «{text}»? Да или нет?", do,
                                              "Не отправляю."))

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        has_tg = re.search(TG, t)
        try:
            m = SEND_RE.match(t)
            if m and has_tg:
                # take message text from the original phrase to keep punctuation/case
                body = text[len(text) - len(m.group(2)):] if len(m.group(2)) <= len(text) else m.group(2)
                return self._confirm_send(m.group(1), body.strip())
            m = SEND_NO_TEXT_RE.match(t)
            if m and has_tg:
                who = m.group(1)
                return ToolResult("Что написать?", expect=lambda said: self._confirm_send(who, said))
            if NEWS_RE.search(t) and (has_tg or "сообщени" in t):
                msgs = self.svc.unread(int(self.cfg.get("max_chats", 5)), int(self.cfg.get("per_chat", 3)),
                                       bool(self.cfg.get("mark_read", False)))
                if not msgs:
                    return ToolResult("Новых сообщений в телеграме нет.")
                parts, last_chat = [], None
                for x in msgs:
                    prefix = f"В чате {x.chat}. " if x.chat != last_chat else ""
                    parts.append(f"{prefix}{x.sender}: {x.text[:200]}")
                    last_chat = x.chat
                return ToolResult(" ".join(parts))
            m = FROM_RE.search(t)
            if m and has_tg:
                d = self.find_dialog(m.group(1))
                if not d:
                    return ToolResult(f"Не нашёл чат «{m.group(1)}».")
                msgs = self.svc.history(d.id, 5)
                return ToolResult(f"Последние сообщения с {d.name}: " + " ".join(f"{x.sender}: {x.text[:200]}." for x in msgs))
            if re.search(r"\b(выключи|отключи)\b.*\bуведомлени\w*\b.*" + TG, t):
                self.muted = True
                return ToolResult("Уведомления телеграма выключил.")
            if re.search(r"\bвключи\b.*\bуведомлени\w*\b.*" + TG, t):
                self.muted = False
                return ToolResult("Уведомления телеграма включил." + ("" if self.cfg.get("notify_new_messages") else
                                  " Но в настройках они отключены: telegram.notify_new_messages."))
        except RuntimeError as e:
            return ToolResult(str(e))
        return None
