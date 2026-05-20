"""Telegram multi-bot transport primitives."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Protocol
from urllib import parse, request

from .models import Message, MessageType
from .policy import PolicyEngine
from .storage import Storage

TELEGRAM_PRIVATE_ROOM = "telegram:private"
logger = logging.getLogger("agentrelay.telegram_transport")


class TelegramClient(Protocol):
    async def send_message(self, token: str, chat_id: str, text: str) -> dict:
        ...

    async def get_updates(self, token: str, offset: int | None = None, timeout: int = 30) -> list[dict]:
        ...


class TelegramHTTPClient:
    async def send_message(self, token: str, chat_id: str, text: str) -> dict:
        return await self._post(token, "sendMessage", {"chat_id": chat_id, "text": text})

    async def get_updates(self, token: str, offset: int | None = None, timeout: int = 30) -> list[dict]:
        payload: dict[str, object] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        response = await self._post(token, "getUpdates", payload)
        result = response.get("result", [])
        return result if isinstance(result, list) else []

    async def _post(self, token: str, method: str, payload: dict[str, object]) -> dict:
        return await asyncio.to_thread(self._post_sync, token, method, payload)

    @staticmethod
    def _post_sync(token: str, method: str, payload: dict[str, object]) -> dict:
        url = f"https://api.telegram.org/bot{token}/{method}"
        data = parse.urlencode(payload).encode("utf-8")
        req = request.Request(url, data=data, method="POST")
        with request.urlopen(req, timeout=40) as response:  # noqa: S310 - Telegram API endpoint.
            return json.loads(response.read().decode("utf-8"))


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

    async def send_group(self, source: str, room: str, content: str) -> dict:
        decision = await PolicyEngine(self.storage).evaluate(source, "*", room)
        if not decision.allowed:
            return {"status": "blocked", "error": decision.reason, "rule_id": decision.rule_id}

        source_bot = await self.storage.get_telegram_bot(source)
        group = await self.storage.get_telegram_group(room)
        if source_bot is None or not source_bot.enabled:
            return {"status": "failed", "error": "SOURCE_BOT_NOT_REGISTERED"}
        if group is None or not group.enabled:
            return {"status": "failed", "error": "GROUP_NOT_REGISTERED"}

        token = self._resolve_token(source_bot.token_ref)
        result = await self.client.send_message(token, group.chat_id, content)
        return {"status": "sent", "telegram": result}

    async def normalize_update(self, receiver_identity: str, update: dict) -> Message:
        raw_message = update.get("message") or update.get("edited_message") or {}
        sender = await self._sender_identity(raw_message.get("from") or {})
        chat = raw_message.get("chat") or {}
        chat_id = str(chat.get("id"))
        registered_group = await self.storage.get_telegram_group_by_chat_id(chat_id)
        if chat.get("type") == "private":
            room = TELEGRAM_PRIVATE_ROOM
        elif registered_group is not None and registered_group.enabled:
            room = registered_group.room
        else:
            raise ValueError("TELEGRAM_GROUP_NOT_REGISTERED")
        target = receiver_identity if room == TELEGRAM_PRIVATE_ROOM else None
        return Message(
            sender=sender,
            room=room,
            content=str(raw_message.get("text") or raw_message.get("caption") or ""),
            type=MessageType.DM if target else MessageType.TEXT,
            target=target,
            thread=str(raw_message["message_id"]) if raw_message.get("message_id") is not None else None,
        )

    async def _sender_identity(self, sender: dict) -> str:
        username = str(sender.get("username") or "").removeprefix("@")
        if username:
            registered = await self.storage.get_telegram_bot_by_username(username)
            if registered is not None and registered.enabled:
                return registered.identity_id
        raise ValueError("TELEGRAM_SENDER_NOT_REGISTERED")

    @staticmethod
    def _resolve_token(token_ref: str) -> str:
        prefix, sep, value = token_ref.partition(":")
        if prefix == "env" and sep:
            token = os.getenv(value)
            if token:
                return token
            raise RuntimeError(f"missing token env var: {value}")
        raise RuntimeError("unsupported telegram token_ref; use env:VARIABLE")


class TelegramBotRunner:
    def __init__(
        self,
        relay,
        client: TelegramClient | None = None,
        *,
        poll_interval: float = 1.0,
        update_timeout: int = 30,
    ):
        self.relay = relay
        self.storage = relay.storage
        self.client = client or TelegramHTTPClient()
        self.poll_interval = poll_interval
        self.update_timeout = update_timeout
        self._offsets: dict[str, int] = {}

    async def run(self) -> None:
        try:
            while True:
                await self.poll_once()
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            raise

    async def poll_once(self) -> None:
        bots = [bot for bot in await self.storage.list_telegram_bots() if bot.enabled]
        await asyncio.gather(*(self._poll_bot(bot) for bot in bots), return_exceptions=True)

    async def _poll_bot(self, bot) -> None:
        transport = TelegramTransport(self.storage, self.client)
        try:
            token = transport._resolve_token(bot.token_ref)
        except RuntimeError:
            logger.warning("telegram bot token unavailable", extra={"identity_id": bot.identity_id})
            return
        try:
            updates = await asyncio.wait_for(
                self.client.get_updates(
                    token,
                    offset=self._offsets.get(bot.identity_id),
                    timeout=self.update_timeout,
                ),
                timeout=self.update_timeout + 10,
            )
        except TimeoutError:
            logger.warning("telegram poll timeout", extra={"identity_id": bot.identity_id})
            return
        except Exception:
            logger.exception("telegram getUpdates failed", extra={"identity_id": bot.identity_id})
            return
        for update in updates:
            await self._handle_update(transport, bot.identity_id, update)

    async def _handle_update(
        self, transport: TelegramTransport, receiver_identity: str, update: dict
    ) -> None:
        update_id = update.get("update_id")
        try:
            msg = await transport.normalize_update(receiver_identity, update)
        except (KeyError, TypeError, ValueError):
            logger.warning("telegram update rejected", extra={"receiver_identity": receiver_identity})
            self._advance_offset(receiver_identity, update_id)
            return
        if not msg.content:
            self._advance_offset(receiver_identity, update_id)
            return
        await self.relay.process(
            {
                "type": msg.type.value if isinstance(msg.type, MessageType) else msg.type,
                "content": msg.content,
                "target": msg.target,
                "thread": msg.thread,
                "skip_transport_delivery": True,
            },
            msg.sender,
            msg.room,
        )
        self._advance_offset(receiver_identity, update_id)

    def _advance_offset(self, receiver_identity: str, update_id: object) -> None:
        if isinstance(update_id, int):
            self._offsets[receiver_identity] = max(
                update_id + 1,
                self._offsets.get(receiver_identity, 0),
            )
