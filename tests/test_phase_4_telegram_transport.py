from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agentrelay.cli import main
from agentrelay.models import PolicyEffect, PolicyRule, TelegramBotRegistration
from agentrelay.storage import Storage
from agentrelay.telegram_transport import TelegramTransport


class FakeTelegramClient:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    async def send_message(self, token: str, chat_id: str, text: str) -> dict:
        self.sent.append((token, chat_id, text))
        return {"ok": True, "result": {"message_id": 42}}


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
