from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

import pytest

from agentrelay.cli import main
from agentrelay.models import (
    PolicyEffect,
    PolicyRule,
    TelegramBotRegistration,
    TelegramGroupRegistration,
)
from agentrelay.relay import Relay
from agentrelay.storage import Storage
from agentrelay.telegram_transport import TelegramBotRunner, TelegramTransport


class FakeTelegramClient:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.updates: list[dict] = []
        self.update_calls: list[tuple[str, int | None]] = []

    async def send_message(self, token: str, chat_id: str, text: str) -> dict:
        self.sent.append((token, chat_id, text))
        return {"ok": True, "result": {"message_id": 42}}

    async def get_updates(self, token: str, offset: int | None = None, timeout: int = 30) -> list[dict]:
        self.update_calls.append((token, offset))
        return list(self.updates)


def test_telegram_bot_registry_roundtrip_and_cli(capsys):
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "relay.db")
        main(
            [
                "telegram",
                "--db-path",
                db_path,
                "add-bot",
                "openclaw.aarya",
                "--username",
                "AaryaAgentBot",
                "--token-env",
                "AARYA_BOT_TOKEN",
                "--bot-to-bot",
                "--json",
            ]
        )
        saved = json.loads(capsys.readouterr().out)
        assert saved["identity_id"] == "openclaw.aarya"
        assert saved["username"] == "AaryaAgentBot"
        assert saved["token_ref"] == "env:AARYA_BOT_TOKEN"
        assert saved["bot_to_bot_enabled"] is True

        main(["telegram", "--db-path", db_path, "list-bots", "--json"])
        listed = json.loads(capsys.readouterr().out)
        assert [bot["identity_id"] for bot in listed] == ["openclaw.aarya"]

        main(
            [
                "telegram",
                "--db-path",
                db_path,
                "add-group",
                "telegram:lab",
                "--chat-id",
                "-100123",
                "--title",
                "AgentRelay Lab",
                "--json",
            ]
        )
        group = json.loads(capsys.readouterr().out)
        assert group["room"] == "telegram:lab"
        assert group["chat_id"] == "-100123"

        main(["telegram", "--db-path", db_path, "list-groups", "--json"])
        groups = json.loads(capsys.readouterr().out)
        assert [registered["room"] for registered in groups] == ["telegram:lab"]


@pytest.mark.asyncio
async def test_telegram_transport_sends_private_bot_to_bot_message(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        try:
            await storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="openclaw.bheem",
                    username="BheemAgentBot",
                    token_ref="env:BHEEM_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="openclaw.aarya",
                    username="AaryaAgentBot",
                    token_ref="env:AARYA_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            monkeypatch.setenv("BHEEM_BOT_TOKEN", "bheem-token")
            client = FakeTelegramClient()
            result = await TelegramTransport(storage, client).send_private(
                "openclaw.bheem",
                "openclaw.aarya",
                "handoff ready",
            )
            assert result["status"] == "sent"
            assert client.sent == [("bheem-token", "@AaryaAgentBot", "handoff ready")]
        finally:
            await storage.close()


@pytest.mark.asyncio
async def test_telegram_transport_blocks_policy_before_delivery(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        try:
            await storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="openclaw.bheem",
                    username="BheemAgentBot",
                    token_ref="env:BHEEM_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="openclaw.aarya",
                    username="AaryaAgentBot",
                    token_ref="env:AARYA_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await storage.upsert_policy(
                PolicyRule(
                    id="deny-telegram",
                    source="openclaw.bheem",
                    target="openclaw.aarya",
                    room="telegram:private",
                    effect=PolicyEffect.DENY,
                )
            )
            monkeypatch.setenv("BHEEM_BOT_TOKEN", "bheem-token")
            client = FakeTelegramClient()
            result = await TelegramTransport(storage, client).send_private(
                "openclaw.bheem",
                "openclaw.aarya",
                "blocked",
            )
            assert result["status"] == "blocked"
            assert result["error"] == "POLICY_DENY"
            assert client.sent == []
        finally:
            await storage.close()


@pytest.mark.asyncio
async def test_telegram_inbound_update_normalizes_registered_bot_sender():
    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        try:
            await storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="openclaw.bheem",
                    username="BheemAgentBot",
                    token_ref="env:BHEEM_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            msg = await TelegramTransport(storage, FakeTelegramClient()).normalize_update(
                "openclaw.aarya",
                {
                    "message": {
                        "message_id": 7,
                        "text": "status?",
                        "from": {"is_bot": True, "username": "BheemAgentBot"},
                        "chat": {"id": 1234, "type": "private"},
                    }
                },
            )
            assert msg.sender == "openclaw.bheem"
            assert msg.target == "openclaw.aarya"
            assert msg.room == "telegram:private"
            assert msg.content == "status?"
        finally:
            await storage.close()


@pytest.mark.asyncio
async def test_telegram_group_update_normalizes_to_registered_room():
    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        try:
            await storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="openclaw.bheem",
                    username="BheemAgentBot",
                    token_ref="env:BHEEM_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await storage.upsert_telegram_group(
                TelegramGroupRegistration(
                    room="telegram:lab",
                    chat_id="-100123",
                    title="AgentRelay Lab",
                )
            )
            msg = await TelegramTransport(storage, FakeTelegramClient()).normalize_update(
                "openclaw.aarya",
                {
                    "message": {
                        "message_id": 8,
                        "text": "review this",
                        "from": {"is_bot": True, "username": "BheemAgentBot"},
                        "chat": {"id": -100123, "type": "supergroup", "title": "AgentRelay Lab"},
                    }
                },
            )
            assert msg.sender == "openclaw.bheem"
            assert msg.target is None
            assert msg.room == "telegram:lab"
            assert msg.content == "review this"
        finally:
            await storage.close()


@pytest.mark.asyncio
async def test_telegram_runner_polls_registered_bots_into_relay_without_echo(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        client = FakeTelegramClient()
        client.updates = [
            {
                "update_id": 10,
                "message": {
                    "message_id": 7,
                    "text": "status?",
                    "from": {"is_bot": True, "username": "BheemAgentBot"},
                    "chat": {"id": 1234, "type": "private"},
                },
            }
        ]
        await relay.startup()
        try:
            await relay.storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="openclaw.aarya",
                    username="AaryaAgentBot",
                    token_ref="env:AARYA_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await relay.storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="openclaw.bheem",
                    username="BheemAgentBot",
                    token_ref="env:BHEEM_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            monkeypatch.setenv("AARYA_BOT_TOKEN", "aarya-token")
            monkeypatch.setenv("BHEEM_BOT_TOKEN", "bheem-token")
            await TelegramBotRunner(relay, client).poll_once()

            history = await relay.storage.fetch_history("telegram:private")
            assert [msg.content for msg in history] == ["status?"]
            assert history[0].sender == "openclaw.bheem"
            assert history[0].target == "openclaw.aarya"
            assert client.update_calls == [("aarya-token", None), ("bheem-token", None)]
            assert client.sent == []
        finally:
            await relay.shutdown()


@pytest.mark.asyncio
async def test_relay_sends_outbound_dm_to_registered_telegram_bot(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        client = FakeTelegramClient()
        relay.set_telegram_transport_client(client)
        await relay.startup()
        try:
            await relay.storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="openclaw.aarya",
                    username="AaryaAgentBot",
                    token_ref="env:AARYA_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await relay.storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="openclaw.bheem",
                    username="BheemAgentBot",
                    token_ref="env:BHEEM_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            monkeypatch.setenv("AARYA_BOT_TOKEN", "aarya-token")
            result = await relay.process(
                {"content": "report ready", "target": "openclaw.bheem"},
                "openclaw.aarya",
                "general",
            )
            assert result["status"] == "sent"
            assert result["delivered_to"] == 1
            assert client.sent == [("aarya-token", "@BheemAgentBot", "report ready")]
        finally:
            await relay.shutdown()


@pytest.mark.asyncio
async def test_relay_sends_room_message_to_registered_telegram_group(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        client = FakeTelegramClient()
        relay.set_telegram_transport_client(client)
        await relay.startup()
        try:
            await relay.storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="openclaw.aarya",
                    username="AaryaAgentBot",
                    token_ref="env:AARYA_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await relay.storage.upsert_telegram_group(
                TelegramGroupRegistration(
                    room="telegram:lab",
                    chat_id="-100123",
                    title="AgentRelay Lab",
                )
            )
            monkeypatch.setenv("AARYA_BOT_TOKEN", "aarya-token")
            result = await relay.process(
                {"content": "group-visible update"},
                "openclaw.aarya",
                "telegram:lab",
            )
            assert result["status"] == "sent"
            assert result["delivered_to"] == 1
            assert client.sent == [("aarya-token", "-100123", "group-visible update")]
        finally:
            await relay.shutdown()


@pytest.mark.asyncio
async def test_telegram_runner_polls_bots_concurrently(monkeypatch):
    class SlowTelegramClient(FakeTelegramClient):
        async def get_updates(
            self, token: str, offset: int | None = None, timeout: int = 30
        ) -> list[dict]:
            self.update_calls.append((token, offset))
            await asyncio.sleep(0.2)
            return []

    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        client = SlowTelegramClient()
        await relay.startup()
        try:
            await relay.storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="openclaw.aarya",
                    username="AaryaAgentBot",
                    token_ref="env:AARYA_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await relay.storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="openclaw.bheem",
                    username="BheemAgentBot",
                    token_ref="env:BHEEM_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            monkeypatch.setenv("AARYA_BOT_TOKEN", "aarya-token")
            monkeypatch.setenv("BHEEM_BOT_TOKEN", "bheem-token")

            started = time.perf_counter()
            await TelegramBotRunner(relay, client).poll_once()
            elapsed = time.perf_counter() - started

            assert sorted(client.update_calls) == [("aarya-token", None), ("bheem-token", None)]
            assert elapsed < 0.35
        finally:
            await relay.shutdown()


@pytest.mark.asyncio
async def test_skip_transport_blocked_for_regular_agent(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        client = FakeTelegramClient()
        relay.set_telegram_transport_client(client)
        await relay.startup()
        try:
            await relay.storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="openclaw.aarya",
                    username="AaryaAgentBot",
                    token_ref="env:AARYA_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await relay.storage.upsert_telegram_group(
                TelegramGroupRegistration(
                    room="telegram:lab",
                    chat_id="-100123",
                    title="AgentRelay Lab",
                )
            )
            monkeypatch.setenv("AARYA_BOT_TOKEN", "aarya-token")
            result = await relay.process(
                {"content": "group-visible update", "skip_transport_delivery": True},
                "openclaw.aarya",
                "telegram:lab",
            )
            assert result["status"] == "sent"
            assert result["delivered_to"] == 1
            # Telegram delivery should still happen because sender is not telegram./system
            assert client.sent == [("aarya-token", "-100123", "group-visible update")]
        finally:
            await relay.shutdown()


@pytest.mark.asyncio
async def test_skip_transport_blocked_for_system_like_agent(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        client = FakeTelegramClient()
        relay.set_telegram_transport_client(client)
        await relay.startup()
        try:
            await relay.storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="systematic",
                    username="SystematicBot",
                    token_ref="env:SYSTEMATIC_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await relay.storage.upsert_telegram_group(
                TelegramGroupRegistration(
                    room="telegram:lab",
                    chat_id="-100123",
                    title="AgentRelay Lab",
                )
            )
            monkeypatch.setenv("SYSTEMATIC_BOT_TOKEN", "systematic-token")
            result = await relay.process(
                {"content": "still visible", "skip_transport_delivery": True},
                "systematic",
                "telegram:lab",
            )
            assert result["status"] == "sent"
            assert result["delivered_to"] == 1
            assert client.sent == [("systematic-token", "-100123", "still visible")]
        finally:
            await relay.shutdown()
