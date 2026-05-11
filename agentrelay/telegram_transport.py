"""Telegram multi-bot transport primitives."""
from __future__ import annotations

import os
from typing import Protocol

from .models import Message, MessageType
from .policy import PolicyEngine
from .storage import Storage

TELEGRAM_PRIVATE_ROOM = "telegram:private"


class TelegramClient(Protocol):
    async def send_message(self, token: str, chat_id: str, text: str) -> dict:
        ...


class TelegramTransport:
    def __init__(self, storage: Storage, client: TelegramClient):
        self.storage = storage
        self.client = client

    async def send_private(self, source: str, target: str, content: str) -> dict:
        decision = await PolicyEngine(self.storage).evaluate(source, target, TELEGRAM_PRIVATE_ROOM)
        if not decision.allowed:
            return {"status": "blocked", "error": decision.reason, "rule_id": decision.rule_id}

        source_bot = await self.storage.get_telegram_bot(source)
        target_bot = await self.storage.get_telegram_bot(target)
        if source_bot is None or not source_bot.enabled:
            return {"status": "failed", "error": "SOURCE_BOT_NOT_REGISTERED"}
        if target_bot is None or not target_bot.enabled:
            return {"status": "failed", "error": "TARGET_BOT_NOT_REGISTERED"}
        if not source_bot.bot_to_bot_enabled or not target_bot.bot_to_bot_enabled:
            return {"status": "failed", "error": "BOT_TO_BOT_NOT_READY"}

        token = self._resolve_token(source_bot.token_ref)
        result = await self.client.send_message(token, f"@{target_bot.username}", content)
        return {"status": "sent", "telegram": result}

    async def normalize_update(self, receiver_identity: str, update: dict) -> Message:
        raw_message = update.get("message") or update.get("edited_message") or {}
        sender = await self._sender_identity(raw_message.get("from") or {})
        chat = raw_message.get("chat") or {}
        room = TELEGRAM_PRIVATE_ROOM if chat.get("type") == "private" else f"telegram:{chat.get('id')}"
        return Message(
            sender=sender,
            room=room,
            content=str(raw_message.get("text") or raw_message.get("caption") or ""),
            type=MessageType.DM,
            target=receiver_identity,
            thread=str(raw_message["message_id"]) if raw_message.get("message_id") is not None else None,
        )

    async def _sender_identity(self, sender: dict) -> str:
        username = str(sender.get("username") or "").removeprefix("@")
        if username:
            registered = await self.storage.get_telegram_bot_by_username(username)
            if registered is not None:
                return registered.identity_id
            return f"telegram.{username}"
        sender_id = sender.get("id", "unknown")
        return f"telegram.{sender_id}"

    @staticmethod
    def _resolve_token(token_ref: str) -> str:
        prefix, sep, value = token_ref.partition(":")
        if prefix == "env" and sep:
            token = os.getenv(value)
            if token:
                return token
            raise RuntimeError(f"missing token env var: {value}")
        raise RuntimeError("unsupported telegram token_ref; use env:VARIABLE")
