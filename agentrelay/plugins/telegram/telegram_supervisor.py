"""Telegram group supervisor for human-in-loop agent discussions."""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from html import escape

from agentrelay.models import (
    Identity,
    IdentityKind,
    IdentityTransport,
    MessageType,
    PolicyEffect,
    PolicyRule,
    TrustLevel,
)
from agentrelay.relay import Relay
from .telegram_transport import TelegramClient, TelegramHTTPClient

logger = logging.getLogger("agentrelay.telegram_supervisor")


@dataclass
class SupervisorState:
    room: str
    chat_id: str
    paused: bool = False
    active_thread: str | None = None
    active_topic: str | None = None
    participants: list[str] = field(default_factory=list)
    message_counts: dict[str, int] = field(default_factory=dict)
    last_sender: str | None = None
    last_sender_streak: int = 0
    started_at: float = field(default_factory=time.time)


class TelegramSupervisorRunner:
    """Observe one Telegram group with one supervisor bot token.

    This mode does not consume existing agent bot tokens. It watches the shared
    group, records bot messages into the relay, and can optionally delete
    messages that violate local guardrails when the supervisor bot is an admin.
    """

    def __init__(
        self,
        relay: Relay,
        *,
        token: str,
        chat_id: str,
        room: str = "telegram:lab",
        client: TelegramClient | None = None,
        poll_interval: float = 1.0,
        update_timeout: int = 30,
        moderate: bool = False,
        cooldown_seconds: int = 30,
        per_agent_cap: int = 3,
    ):
        self.relay = relay
        self.token = token
        self.state = SupervisorState(room=room, chat_id=str(chat_id))
        self.client = client or TelegramHTTPClient()
        self.poll_interval = poll_interval
        self.update_timeout = update_timeout
        self.moderate = moderate
        self.cooldown_seconds = cooldown_seconds
        self.per_agent_cap = per_agent_cap
        self._offset: int | None = None
        self._last_seen: dict[str, float] = {}

    async def run(self) -> None:
        try:
            await self._set_commands()
            await self._announce(
                "AgentRelay supervisor online. Use /discuss, /pause, /resume, /summary."
            )
            while True:
                await self.poll_once()
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            raise

    async def poll_once(self) -> None:
        try:
            updates = await asyncio.wait_for(
                self.client.get_updates(self.token, offset=self._offset, timeout=self.update_timeout),
                timeout=self.update_timeout + 10,
            )
        except TimeoutError:
            logger.warning("telegram supervisor poll timeout")
            return
        except Exception:
            logger.exception("telegram supervisor getUpdates failed")
            return
        for update in updates:
            await self._handle_update(update)

    async def _handle_update(self, update: dict) -> None:
        update_id = update.get("update_id")
        try:
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            if str(chat.get("id")) != self.state.chat_id:
                return
            text = str(message.get("text") or message.get("caption") or "")
            if not text:
                return
            sender = self._sender_id(message.get("from") or {})
            if text.startswith("/"):
                await self._handle_command(text, sender)
            else:
                await self._handle_group_message(message, sender, text)
        finally:
            if isinstance(update_id, int):
                self._offset = max(update_id + 1, self._offset or 0)

    def _sender_id(self, sender: dict) -> str:
        username = str(sender.get("username") or "").removeprefix("@")
        if username:
            return f"telegram.{username}"
        sender_id = sender.get("id")
        return f"telegram.user.{sender_id}" if sender_id is not None else "telegram.unknown"

    async def _handle_command(self, text: str, sender: str) -> None:
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command == "/pause":
            self.state.paused = True
            await self._announce("Paused. Agent messages will be logged and flagged.")
        elif command == "/resume":
            self.state.paused = False
            await self._announce("Resumed.")
        elif command == "/summary":
            await self._announce(await self._summary_text())
        elif command == "/discuss":
            await self._start_discussion(text.partition(" ")[2], sender)

    async def _start_discussion(self, raw: str, sender: str) -> None:
        if not raw.strip():
            await self._announce('Usage: /discuss "topic" --agents @a,@b,@c')
            return
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            await self._announce(f"Parse error: {escape(str(exc))}")
            return
        agents: list[str] = []
        topic_parts: list[str] = []
        i = 0
        while i < len(parts):
            if parts[i] == "--agents" and i + 1 < len(parts):
                agents = [self._normalize_participant(a) for a in parts[i + 1].split(",") if a.strip()]
                i += 2
            else:
                topic_parts.append(parts[i])
                i += 1
        topic = " ".join(topic_parts).strip()
        if not topic:
            await self._announce('Usage: /discuss "topic" --agents @a,@b,@c')
            return
        thread = topic[:64]
        self.state.active_thread = thread
        self.state.active_topic = topic
        self.state.participants = agents
        self.state.message_counts.clear()
        self.state.last_sender = None
        self.state.last_sender_streak = 0
        self.state.paused = False
        await self._allow_sender(sender)
        for participant in agents:
            await self._allow_sender(participant)
        await self.relay.storage.store_thread(thread, self.state.room, topic)
        await self.relay.process(
            {
                "content": topic,
                "thread": thread,
                "mentions": agents,
                "skip_transport_delivery": True,
            },
            sender,
            self.state.room,
        )
        participants = ", ".join(agents) if agents else "any visible agents"
        await self._announce(
            f"Discussion started: <b>{escape(topic)}</b>\nParticipants: {escape(participants)}"
        )

    async def _handle_group_message(self, message: dict, sender: str, text: str) -> None:
        message_id = message.get("message_id")
        await self._allow_sender(sender)
        blocked_reason = self._guardrail_violation(sender)
        if blocked_reason:
            await self.relay.process(
                {
                    "content": f"supervisor_block: {blocked_reason}: {text}",
                    "thread": self.state.active_thread,
                    "skip_transport_delivery": True,
                },
                sender,
                self.state.room,
            )
            await self._announce(f"Guardrail: {escape(sender)} blocked: {escape(blocked_reason)}")
            if self.moderate and isinstance(message_id, int):
                await self._delete(message_id)
            return

        self.state.message_counts[sender] = self.state.message_counts.get(sender, 0) + 1
        self._record_sender(sender)
        await self.relay.process(
            {
                "content": text,
                "thread": self.state.active_thread,
                "type": MessageType.TEXT.value,
                "skip_transport_delivery": True,
            },
            sender,
            self.state.room,
        )

    def _guardrail_violation(self, sender: str) -> str | None:
        now = time.time()
        if self.state.paused:
            return "paused"
        if self.state.participants and sender not in self.state.participants:
            return "not_in_discussion"
        if self.state.message_counts.get(sender, 0) >= self.per_agent_cap:
            return "per_agent_cap"
        last_seen = self._last_seen.get(sender)
        if last_seen and now - last_seen < self.cooldown_seconds:
            return "cooldown"
        if self.state.last_sender == sender and self.state.last_sender_streak >= 2:
            return "turn_streak"
        return None

    def _record_sender(self, sender: str) -> None:
        now = time.time()
        self._last_seen[sender] = now
        if self.state.last_sender == sender:
            self.state.last_sender_streak += 1
        else:
            self.state.last_sender = sender
            self.state.last_sender_streak = 1

    async def _summary_text(self) -> str:
        messages = []
        if self.state.active_thread:
            messages = await self.relay.storage.fetch_history(self.state.room, 20)
            messages = [msg for msg in messages if msg.thread == self.state.active_thread]
        counts = ", ".join(
            f"{sender}={count}" for sender, count in sorted(self.state.message_counts.items())
        ) or "none"
        lines = [
            f"Room: {escape(self.state.room)}",
            f"Paused: {self.state.paused}",
            f"Topic: {escape(self.state.active_topic or '-')}",
            f"Counts: {escape(counts)}",
        ]
        if messages:
            lines.append("Recent:")
            lines.extend(f"- {escape(msg.sender)}: {escape(msg.content[:160])}" for msg in messages[-5:])
        return "\n".join(lines)

    async def _allow_sender(self, sender: str) -> None:
        if await self.relay.storage.get_identity(sender) is None:
            await self.relay.storage.upsert_identity(
                Identity(
                    id=sender,
                    display_name=sender,
                    kind=IdentityKind.VPS,
                    transport=IdentityTransport.TELEGRAM,
                    capabilities=["telegram", "supervised"],
                    trust_level=TrustLevel.TRUSTED,
                )
            )
        await self.relay.storage.upsert_policy(
            PolicyRule(
                source=sender,
                target="*",
                room=self.state.room,
                effect=PolicyEffect.ALLOW,
                reason="telegram supervisor observed group participant",
            )
        )

    async def _announce(self, text: str) -> None:
        try:
            await self.client.send_message(self.token, self.state.chat_id, text[:3900])
        except Exception:
            logger.exception("telegram supervisor announcement failed")

    async def _set_commands(self) -> None:
        commands = [
            {"command": "discuss", "description": "Start supervised agent discussion"},
            {"command": "pause", "description": "Pause supervised agent replies"},
            {"command": "resume", "description": "Resume supervised discussion"},
            {"command": "summary", "description": "Show discussion summary"},
        ]
        try:
            await self.client.set_my_commands(self.token, commands)
        except Exception:
            logger.exception("telegram supervisor command registration failed")

    async def _delete(self, message_id: int) -> None:
        try:
            await self.client.delete_message(self.token, self.state.chat_id, message_id)
        except Exception:
            logger.exception("telegram supervisor delete failed", extra={"message_id": message_id})

    @staticmethod
    def _normalize_participant(value: str) -> str:
        return f"telegram.{value.strip().removeprefix('@')}"


def supervisor_from_env(relay: Relay, client: TelegramClient | None = None) -> TelegramSupervisorRunner:
    token = os.getenv("AGENTRELAY_SUPERVISOR_TG_TOKEN")
    chat_id = os.getenv("AGENTRELAY_SUPERVISOR_TG_CHAT")
    if not token or not chat_id:
        raise RuntimeError("AGENTRELAY_SUPERVISOR_TG_TOKEN and AGENTRELAY_SUPERVISOR_TG_CHAT are required")
    return TelegramSupervisorRunner(
        relay,
        token=token,
        chat_id=chat_id,
        room=os.getenv("AGENTRELAY_SUPERVISOR_ROOM", "telegram:lab"),
        client=client,
        moderate=os.getenv("AGENTRELAY_SUPERVISOR_MODERATE", "").lower() in {"1", "true", "yes"},
        cooldown_seconds=int(os.getenv("AGENTRELAY_SUPERVISOR_COOLDOWN", "30")),
        per_agent_cap=int(os.getenv("AGENTRELAY_SUPERVISOR_AGENT_CAP", "3")),
    )
