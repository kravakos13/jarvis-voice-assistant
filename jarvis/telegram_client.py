"""Telegram user client (Telethon) running in its own asyncio thread with sync wrappers.

One-time login: python scripts/telegram_login.py (asks for the code from Telegram).
The session file stays on this PC (data dir). Nothing is ever sent without the
skill asking for voice confirmation first.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from . import platform_utils as pu

log = logging.getLogger(__name__)


@dataclass
class TgMessage:
    chat: str
    sender: str
    text: str


@dataclass
class TgDialog:
    id: int
    name: str
    unread: int


class TelegramService:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.api_id = int(self.cfg.get("api_id") or 0)
        self.api_hash = str(self.cfg.get("api_hash") or "")
        self.session = str(pu.data_dir("", self.cfg.get("dir", "")) / "telegram")
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.client = None
        self._ready = threading.Event()
        self._error: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self.api_id and self.api_hash)

    def start(self, on_new_message: Optional[Callable[[TgMessage], None]] = None) -> None:
        if not self.configured:
            self._error = "Telegram не настроен: впиши api_id и api_hash в config.yaml и выполни scripts/telegram_login.py"
            return
        def runner():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                from telethon import TelegramClient, events
                self.client = TelegramClient(self.session, self.api_id, self.api_hash, loop=self.loop)
                self.loop.run_until_complete(self.client.connect())
                if not self.loop.run_until_complete(self.client.is_user_authorized()):
                    self._error = "Telegram: нужен вход. Запусти scripts/telegram_login.py"
                    self._ready.set()
                    return
                if on_new_message:
                    @self.client.on(events.NewMessage(incoming=True))
                    async def handler(ev):  # noqa
                        try:
                            sender = await ev.get_sender()
                            chat = await ev.get_chat()
                            on_new_message(TgMessage(_name(chat), _name(sender), ev.raw_text or "[вложение]"))
                        except Exception as e:
                            log.debug("tg handler: %s", e)
                self._ready.set()
                self.loop.run_forever()
            except Exception as e:
                self._error = f"Telegram: {e}"
                self._ready.set()
        threading.Thread(target=runner, name="jarvis-telegram", daemon=True).start()

    def _run(self, coro, timeout: float = 30):
        if not self._ready.wait(15):
            raise RuntimeError("Telegram ещё подключается")
        if self._error:
            raise RuntimeError(self._error)
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # ------------------------------------------------------------ API
    def dialogs(self, limit: int = 60) -> list[TgDialog]:
        async def go():
            return [TgDialog(d.id, d.name or "без имени", d.unread_count) async for d in self.client.iter_dialogs(limit=limit)]
        return self._run(go())

    def unread(self, max_chats: int = 5, per_chat: int = 3, mark_read: bool = False) -> list[TgMessage]:
        async def go():
            out = []
            async for d in self.client.iter_dialogs(limit=60):
                if d.unread_count <= 0 or (d.archived if hasattr(d, "archived") else False):
                    continue
                msgs = await self.client.get_messages(d.entity, limit=min(per_chat, d.unread_count))
                for m in reversed(msgs):
                    sender = await m.get_sender()
                    out.append(TgMessage(d.name, _name(sender), m.message or "[вложение]"))
                if mark_read:
                    await self.client.send_read_acknowledge(d.entity)
                max_left = max_chats - len({x.chat for x in out})
                if max_left <= 0:
                    break
            return out
        return self._run(go())

    def history(self, dialog_id: int, limit: int = 5) -> list[TgMessage]:
        async def go():
            msgs = await self.client.get_messages(dialog_id, limit=limit)
            out = []
            for m in reversed(msgs):
                sender = await m.get_sender()
                out.append(TgMessage("", "я" if m.out else _name(sender), m.message or "[вложение]"))
            return out
        return self._run(go())

    def send(self, dialog_id: int, text: str) -> None:
        self._run(self.client.send_message(dialog_id, text))

    def login_interactive(self) -> None:  # used by scripts/telegram_login.py
        from telethon.sync import TelegramClient
        with TelegramClient(self.session, self.api_id, self.api_hash) as c:
            me = c.get_me()
            print(f"Вход выполнен: {me.first_name} (@{me.username}). Сессия: {self.session}.session")


def _name(entity) -> str:
    if entity is None:
        return "кто-то"
    for attr in ("title",):
        if getattr(entity, attr, None):
            return getattr(entity, attr)
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    return (first + " " + last).strip() or getattr(entity, "username", "") or "кто-то"
