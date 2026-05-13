"""In-process Telegram bot for AgentRelay monitoring and control."""
from __future__ import annotations

import asyncio
import logging
import shlex
import time
from html import escape
from typing import TYPE_CHECKING

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .models import GovernanceConfig, Message, MessageType, ThreadState

if TYPE_CHECKING:
    from .relay import Relay

logger = logging.getLogger("agentrelay.telegram")

GOVERN_ALIASES = {
    "cooldown": "cooldown_seconds",
    "cooldown_seconds": "cooldown_seconds",
    "rate_limit": "rate_limit",
    "per_thread_cap": "per_thread_cap",
    "thread_cap": "per_thread_cap",
    "per_agent_thread_cap": "per_agent_thread_cap",
    "agent_thread_cap": "per_agent_thread_cap",
    "inactivity_timeout": "inactivity_timeout",
    "timeout": "inactivity_timeout",
}


class TelegramBot:
    """Async polling Telegram control plane for one Relay instance."""

    def __init__(self, relay: "Relay", token: str, chat_id: str):
        self.relay = relay
        self.chat_id = str(chat_id)
        self._muted: dict[str, float] = {}
        self.app = Application.builder().token(token).build()
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("rooms", self.cmd_rooms))
        self.app.add_handler(CommandHandler("agents", self.cmd_agents))
        self.app.add_handler(CommandHandler("history", self.cmd_history))
        self.app.add_handler(CommandHandler("discuss", self.cmd_discuss))
        self.app.add_handler(CommandHandler("govern", self.cmd_govern))
        self.app.add_handler(CommandHandler("close", self.cmd_close))
        self.app.add_handler(CommandHandler("mute", self.cmd_mute))

    def _authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        return bool(chat and str(chat.id) == self.chat_id)

    async def _reply(self, update: Update, text: str) -> None:
        if not update.message:
            return
        await update.message.reply_text(text[:3900], parse_mode=ParseMode.HTML)

    async def _safe_reply(self, update: Update, text: str) -> None:
        try:
            await self._reply(update, text)
        except Exception:
            logger.exception("telegram command reply failed")

    async def _guard(self, update: Update) -> bool:
        if self._authorized(update):
            return True
        logger.warning("ignored telegram command from unauthorized chat", extra={"chat_id": update.effective_chat.id if update.effective_chat else None})
        return False

    def _args(self, context: ContextTypes.DEFAULT_TYPE) -> list[str]:
        return list(context.args or [])

    async def forward(self, msg: Message) -> None:
        """Relay hook: forward public room messages/system/gov events to Telegram."""
        if msg.type == MessageType.DM or msg.target:
            return
        until = self._muted.get(msg.sender)
        if until and until > time.time():
            return
        if until:
            self._muted.pop(msg.sender, None)

        if msg.type in {MessageType.JOIN, MessageType.LEAVE, MessageType.SYSTEM} or msg.sender == "system":
            text = f"[sys] {escape(msg.content)}"
        elif msg.content.startswith("governance_block:"):
            text = f"[gov] {escape(msg.sender)} blocked: {escape(msg.content)}"
        else:
            room_thread = msg.room if not msg.thread else f"{msg.room}/{msg.thread}"
            dm = f" → {msg.target}" if msg.target else ""
            text = f"[{escape(room_thread)}] {escape(msg.sender)}{escape(dm)}: {escape(msg.content)}"
        try:
            await self.app.bot.send_message(chat_id=self.chat_id, text=text[:3900], parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("telegram forwarding failed")

    async def run(self) -> None:
        """Run async polling until cancelled."""
        await self.app.initialize()
        await self.app.bot.set_my_commands([
            BotCommand("status", "Server uptime, agents, rooms, metrics"),
            BotCommand("rooms", "List active rooms"),
            BotCommand("agents", "List connected agents"),
            BotCommand("history", "Show recent messages — /history <room> [limit]"),
            BotCommand("discuss", "Start a topic — /discuss \"topic\" --agents a,b"),
            BotCommand("govern", "View or change governance settings"),
            BotCommand("close", "Close a thread — /close <thread_id> [room]"),
            BotCommand("mute", "Mute an agent — /mute <agent> <seconds>"),
        ])
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]
        logger.info("telegram bot started")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        finally:
            await self.app.updater.stop()  # type: ignore[union-attr]
            await self.app.stop()
            await self.app.shutdown()
            logger.info("telegram bot stopped")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        uptime = int(time.time() - self.relay.started_at)
        metrics = self.relay.governance.metrics()
        await self._reply(
            update,
            "\n".join(
                [
                    f"<b>AgentRelay</b> up {uptime}s",
                    f"Agents: {len(self.relay._agents)}",
                    f"Rooms: {len(self.relay._ws)}",
                    f"Messages received: {self.relay.counters['messages_received']}",
                    f"Messages sent: {self.relay.counters['messages_sent']}",
                    f"Rejected: {self.relay.counters['messages_rejected']}",
                    f"Governance: {escape(str(metrics))}",
                ]
            ),
        )

    async def cmd_rooms(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        rooms = self.relay.rooms()
        if not rooms:
            return await self._reply(update, "No active rooms.")
        await self._reply(update, "\n".join(f"{escape(r.id)} — {len(r.agents)} agent(s)" for r in rooms))

    async def cmd_agents(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        args = self._args(context)
        room = args[0] if args else None
        rows = []
        for agent in self.relay._agents.values():
            rooms = sorted(self.relay._agent_rooms.get(agent.id, []))
            if room and room not in rooms:
                continue
            rows.append(f"{escape(agent.id)} — {escape(','.join(rooms) or agent.room)}")
        await self._reply(update, "\n".join(rows) if rows else "No connected agents.")

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        try:
            args = self._args(context)
            if not args:
                return await self._reply(update, "Usage: /history &lt;room&gt; [limit]")
            room = args[0]
            if len(args) > 1 and not args[1].isdigit():
                return await self._reply(update, "Usage: /history &lt;room&gt; [limit]")
            limit = int(args[1]) if len(args) > 1 else 10
            messages = await self.relay.storage.fetch_history(room, max(1, min(limit, 50)))
            if not messages:
                return await self._reply(update, f"No history for {escape(room)}.")
            lines = []
            for msg in messages[-limit:]:
                prefix = f"#{msg.seq} {msg.sender}"
                if msg.thread:
                    prefix += f"/{msg.thread}"
                lines.append(f"{escape(prefix)}: {escape(msg.content)}")
            await self._reply(update, "\n".join(lines))
        except Exception:
            logger.exception("telegram history command failed")
            await self._safe_reply(update, "History failed. Check room name/limit and try again.")

    async def cmd_discuss(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        raw = " ".join(self._args(context))
        if not raw:
            return await self._reply(update, 'Usage: /discuss "topic" --agents a,b,c [--room R]')
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            return await self._reply(update, f"Parse error: {escape(str(exc))}")
        room = "general"
        agents: list[str] = []
        topic_parts: list[str] = []
        i = 0
        while i < len(parts):
            if parts[i] == "--agents" and i + 1 < len(parts):
                agents = [a.strip() for a in parts[i + 1].split(",") if a.strip()]
                i += 2
            elif parts[i] == "--room" and i + 1 < len(parts):
                room = parts[i + 1]
                i += 2
            else:
                topic_parts.append(parts[i])
                i += 1
        topic = " ".join(topic_parts).strip()
        if not topic or not agents:
            return await self._reply(update, 'Usage: /discuss "topic" --agents a,b,c [--room R]')
        thread = topic[:64]
        await self.relay.storage.store_thread(thread, room, topic)
        msg = {"content": topic, "thread": thread, "mentions": agents}
        result = await self.relay.process(msg, "telegram", room)
        await self._reply(update, f"Discuss thread <b>{escape(thread)}</b> in {escape(room)}: {escape(str(result))}")

    async def cmd_govern(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        args = self._args(context)
        if not args:
            cfg = self.relay.governance.config
            return await self._reply(
                update,
                "\n".join(
                    [
                        f"cooldown_seconds={cfg.cooldown_seconds}",
                        f"rate_limit={cfg.rate_limit}",
                        f"per_thread_cap={cfg.per_thread_cap}",
                        f"per_agent_thread_cap={cfg.per_agent_thread_cap}",
                        f"inactivity_timeout={cfg.inactivity_timeout}",
                        f"metrics={escape(str(self.relay.governance.metrics()))}",
                    ]
                ),
            )
        if args[0] == "reset":
            self.relay.governance.update_config(GovernanceConfig())
            return await self._reply(update, "Governance reset.")
        if len(args) != 2 or args[0] not in GOVERN_ALIASES or not args[1].isdigit():
            return await self._reply(update, "Usage: /govern [param value|reset]")
        field = GOVERN_ALIASES[args[0]]
        cfg = self.relay.governance.config.model_copy(update={field: int(args[1])})
        self.relay.governance.update_config(cfg)
        await self._reply(update, f"Updated {escape(field)}={int(args[1])}")

    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        args = self._args(context)
        if not args:
            return await self._reply(update, "Usage: /close &lt;thread_id&gt; [room]")
        thread = args[0]
        room = args[1] if len(args) > 1 else "general"
        try:
            await self.relay.storage.update_thread_state(thread, ThreadState.CLOSED, room)
            self.relay.governance.cleanup_thread(room, thread)
            await self._reply(update, f"Closed thread {escape(thread)} in {escape(room)}")
        except Exception:
            logger.exception("telegram close command failed", extra={"thread": thread, "room": room})
            await self._safe_reply(update, f"Could not close thread {escape(thread)} in {escape(room)}.")

    async def cmd_mute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        args = self._args(context)
        if len(args) != 2 or not args[1].isdigit():
            return await self._reply(update, "Usage: /mute &lt;agent_id&gt; &lt;seconds&gt;")
        self._muted[args[0]] = time.time() + int(args[1])
        await self._reply(update, f"Muted {escape(args[0])} for {int(args[1])}s")
