from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agentrelay.models import GovernanceConfig, Message, MessageType
from agentrelay.relay import Relay
from agentrelay.telegram import TelegramBot


class FakeMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str, **_: object) -> None:
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, chat_id: str = "123"):
        self.effective_chat = SimpleNamespace(id=int(chat_id))
        self.message = FakeMessage()


def fake_context(*args: str):
    return SimpleNamespace(args=list(args))


@pytest.fixture
def relay():
    with tempfile.TemporaryDirectory() as td:
        yield Relay(
            token="test-token",
            db_path=str(Path(td) / "relay.db"),
            governance=GovernanceConfig(cooldown_seconds=0),
        )


@pytest.fixture
def bot(relay: Relay):
    return TelegramBot(relay, "123456:ABCDEF", "123")


@pytest.mark.asyncio
async def test_telegram_forward_formats_message_and_skips_dm(bot: TelegramBot):
    send_message = AsyncMock()
    with patch.object(type(bot.app.bot), "send_message", send_message):
        await bot.forward(Message(sender="agent-a", room="general", thread="topic-1", content="hello"))
        await bot.forward(
            Message(
                sender="agent-a",
                room="general",
                content="secret",
                type=MessageType.DM,
                target="agent-b",
            )
        )
    send_message.assert_awaited_once()
    assert "[general/topic-1] agent-a: hello" in send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_telegram_commands_status_rooms_agents_govern_mute(bot: TelegramBot, relay: Relay):
    relay._agents["agent-a"] = SimpleNamespace(id="agent-a", room="general")
    relay._agent_rooms["agent-a"].add("general")
    relay._ws["general"] = {}

    update = FakeUpdate()
    await bot.cmd_status(update, fake_context())
    await bot.cmd_rooms(update, fake_context())
    await bot.cmd_agents(update, fake_context())
    await bot.cmd_govern(update, fake_context())
    await bot.cmd_govern(update, fake_context("cooldown", "5"))
    await bot.cmd_mute(update, fake_context("agent-a", "30"))

    joined = "\n".join(update.message.replies)
    assert "AgentRelay" in joined
    assert "general" in joined
    assert "agent-a" in joined
    assert relay.governance.config.cooldown_seconds == 5
    assert bot._muted["agent-a"] > 0


@pytest.mark.asyncio
async def test_telegram_history_discuss_and_close(bot: TelegramBot, relay: Relay):
    await relay.storage.init()
    update = FakeUpdate()
    await bot.cmd_discuss(update, fake_context("Test topic", "--agents", "a,b", "--room", "general"))
    await bot.cmd_history(update, fake_context("general", "5"))
    await bot.cmd_history(update, fake_context("general", "bad-limit"))
    await bot.cmd_close(update, fake_context("Test topic"))

    replies = "\n".join(update.message.replies)
    assert "Discuss thread" in replies
    assert "Test topic" in replies
    assert "Usage: /history" in replies
    assert "Closed thread" in replies
    await relay.storage.close()


def test_relay_starts_telegram_task_when_env_configured(monkeypatch: pytest.MonkeyPatch, relay: Relay):
    created = []

    class FakeTelegramBot:
        def __init__(self, relay_arg, token: str, chat_id: str):
            created.append((relay_arg, token, chat_id))

        async def forward(self, msg):
            return None

        async def run(self):
            await asyncio.Event().wait()

    monkeypatch.setenv("AGENTRELAY_TG_TOKEN", "123456:ABCDEF")
    monkeypatch.setenv("AGENTRELAY_TG_CHAT", "123")
    monkeypatch.setattr("agentrelay.telegram.TelegramBot", FakeTelegramBot)

    async def exercise():
        await relay.startup()
        try:
            assert created and created[0][1:] == ("123456:ABCDEF", "123")
            assert relay._tg_hook is not None
        finally:
            await relay.shutdown()

    asyncio.run(exercise())
