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
)
from agentrelay.plugins.telegram.models import (
    TelegramBotRegistration,
    TelegramGroupRegistration,
)
from agentrelay.plugins.telegram.telegram_supervisor import TelegramSupervisorRunner
from agentrelay.plugins.telegram.telegram_transport import TelegramBotRunner, TelegramTransport
from agentrelay.relay import Relay
from agentrelay.storage import Storage


class FakeTelegramClient:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.updates: list[dict] = []
        self.update_calls: list[tuple[str, int | None]] = []

    async def send_message(self, token: str, chat_id: str, text: str) -> dict:
        self.sent.append((token, chat_id, text))
        return {"ok": True, "result": {"message_id": 42}}

    async def set_my_commands(self, token: str, commands: list[dict[str, str]]) -> dict:
        self.sent.append((token, "commands", ",".join(command["command"] for command in commands)))
        return {"ok": True, "result": True}

    async def delete_message(self, token: str, chat_id: str, message_id: int) -> dict:
        self.sent.append((token, chat_id, f"delete:{message_id}"))
        return {"ok": True, "result": True}

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
                "acme.alice",
                "--username",
                "AliceAgentBot",
                "--token-env",
                "ALICE_BOT_TOKEN",
                "--bot-to-bot",
                "--json",
            ]
        )
        saved = json.loads(capsys.readouterr().out)
        assert saved["identity_id"] == "acme.alice"
        assert saved["username"] == "AliceAgentBot"
        assert saved["token_ref"] == "env:ALICE_BOT_TOKEN"
        assert saved["bot_to_bot_enabled"] is True

        main(["telegram", "--db-path", db_path, "list-bots", "--json"])
        listed = json.loads(capsys.readouterr().out)
        assert [bot["identity_id"] for bot in listed] == ["acme.alice"]

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


def test_telegram_lab_init_registers_bots_group_and_policies(capsys):
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "relay.db")
        main(
            [
                "init",
                "telegram-lab",
                "--db-path",
                db_path,
                "--room",
                "telegram:lab",
                "--group-chat-id",
                "-100123",
                "--group-title",
                "AgentRelay Lab",
                "--bot",
                "agent.alpha=@AlphaAgentBot:ALPHA_BOT_TOKEN",
                "--bot",
                "agent.beta=@BetaAgentBot:BETA_BOT_TOKEN",
                "--json",
            ]
        )
        summary = json.loads(capsys.readouterr().out)
        assert summary["room"] == "telegram:lab"
        assert summary["bots"] == ["agent.alpha", "agent.beta"]
        assert summary["policies"] == 2
        assert summary["next_command"] == "agentrelay start --telegram-transport"

        main(["telegram", "--db-path", db_path, "list-bots", "--json"])
        bots = json.loads(capsys.readouterr().out)
        assert [bot["identity_id"] for bot in bots] == ["agent.alpha", "agent.beta"]
        assert all(bot["bot_to_bot_enabled"] for bot in bots)

        main(["telegram", "--db-path", db_path, "list-groups", "--json"])
        groups = json.loads(capsys.readouterr().out)
        assert groups[0]["room"] == "telegram:lab"
        assert groups[0]["chat_id"] == "-100123"

        main(["policy", "--db-path", db_path, "list", "--json"])
        policies = json.loads(capsys.readouterr().out)
        routes = {
            (rule["source"], rule["target"], rule["room"], rule["effect"]) for rule in policies
        }
        assert routes == {
            ("agent.alpha", "agent.beta", "telegram:lab", "allow"),
            ("agent.beta", "agent.alpha", "telegram:lab", "allow"),
        }


def test_telegram_lab_init_keeps_external_bots_sandboxed_and_disabled(capsys):
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "relay.db")
        main(
            [
                "init",
                "telegram-lab",
                "--db-path",
                db_path,
                "--bot",
                "external.alpha=@AlphaAgentBot:ALPHA_BOT_TOKEN",
                "--bot",
                "agent.beta=@BetaAgentBot:BETA_BOT_TOKEN",
                "--json",
            ]
        )
        summary = json.loads(capsys.readouterr().out)
        assert summary["policies"] == 0

        main(["identities", "--db-path", db_path, "show", "external.alpha"])
        identity = json.loads(capsys.readouterr().out)
        assert identity["kind"] == "external"
        assert identity["trust_level"] == "sandbox"
        assert identity["enabled"] is False

        main(["telegram", "--db-path", db_path, "list-bots", "--all", "--json"])
        bots = {bot["identity_id"]: bot for bot in json.loads(capsys.readouterr().out)}
        assert bots["external.alpha"]["enabled"] is False
        assert bots["agent.beta"]["enabled"] is True


def test_telegram_lab_init_rejects_malformed_bot_spec(capsys):
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "relay.db")
        with pytest.raises(SystemExit):
            main(
                [
                    "init",
                    "telegram-lab",
                    "--db-path",
                    db_path,
                    "--bot",
                    "bad-format",
                ]
            )
        assert "BOT must look like identity=@BotUsername:ENV_VAR" in capsys.readouterr().err


def test_telegram_lab_init_accepts_db_path_before_profile(capsys):
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "relay.db")
        main(
            [
                "init",
                "--db-path",
                db_path,
                "telegram-lab",
                "--bot",
                "agent.alpha=@AlphaAgentBot:ALPHA_BOT_TOKEN",
                "--bot",
                "agent.beta=@BetaAgentBot:BETA_BOT_TOKEN",
                "--json",
            ]
        )
        assert json.loads(capsys.readouterr().out)["policies"] == 2

        main(["telegram", "--db-path", db_path, "list-bots", "--json"])
        bots = json.loads(capsys.readouterr().out)
        assert [bot["identity_id"] for bot in bots] == ["agent.alpha", "agent.beta"]


@pytest.mark.asyncio
async def test_telegram_transport_sends_private_bot_to_bot_message(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        try:
            await storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="acme.bob",
                    username="BobAgentBot",
                    token_ref="env:BOB_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="acme.alice",
                    username="AliceAgentBot",
                    token_ref="env:ALICE_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            monkeypatch.setenv("BOB_BOT_TOKEN", "bob-token")
            client = FakeTelegramClient()
            result = await TelegramTransport(storage, client).send_private(
                "acme.bob",
                "acme.alice",
                "handoff ready",
            )
            assert result["status"] == "sent"
            assert client.sent == [("bob-token", "@AliceAgentBot", "handoff ready")]
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
                    identity_id="acme.bob",
                    username="BobAgentBot",
                    token_ref="env:BOB_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="acme.alice",
                    username="AliceAgentBot",
                    token_ref="env:ALICE_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await storage.upsert_policy(
                PolicyRule(
                    id="deny-telegram",
                    source="acme.bob",
                    target="acme.alice",
                    room="telegram:private",
                    effect=PolicyEffect.DENY,
                )
            )
            monkeypatch.setenv("BOB_BOT_TOKEN", "bob-token")
            client = FakeTelegramClient()
            result = await TelegramTransport(storage, client).send_private(
                "acme.bob",
                "acme.alice",
                "blocked",
            )
            assert result["status"] == "blocked"
            assert result["error"] == "POLICY_DENY"
            assert client.sent == []
        finally:
            await storage.close()


@pytest.mark.asyncio
async def test_telegram_supervisor_discuss_and_records_group_messages():
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        await relay.startup()
        try:
            client = FakeTelegramClient()
            runner = TelegramSupervisorRunner(
                relay,
                token="supervisor-token",
                chat_id="-100123",
                room="telegram:lab",
                client=client,
                cooldown_seconds=0,
            )
            client.updates = [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 10,
                        "chat": {"id": -100123, "type": "supergroup"},
                        "from": {"id": 1, "username": "Aditya"},
                        "text": '/discuss "ship plan" --agents @AaryaBot,@BheeshmBot',
                    },
                },
                {
                    "update_id": 2,
                    "message": {
                        "message_id": 11,
                        "chat": {"id": -100123, "type": "supergroup"},
                        "from": {"id": 2, "username": "AaryaBot", "is_bot": True},
                        "text": "I will coordinate.",
                    },
                },
            ]

            await runner.poll_once()

            history = await relay.storage.fetch_history("telegram:lab", 10)
            assert [msg.sender for msg in history] == ["telegram.Aditya", "telegram.AaryaBot"]
            assert history[0].content == "ship plan"
            assert history[1].thread == "ship plan"
            assert client.sent[-1][2].startswith("Discussion started:")
        finally:
            await relay.shutdown()


@pytest.mark.asyncio
async def test_telegram_supervisor_paused_moderation_deletes_message():
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        await relay.startup()
        try:
            client = FakeTelegramClient()
            runner = TelegramSupervisorRunner(
                relay,
                token="supervisor-token",
                chat_id="-100123",
                room="telegram:lab",
                client=client,
                moderate=True,
            )
            client.updates = [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 10,
                        "chat": {"id": -100123, "type": "supergroup"},
                        "from": {"id": 1, "username": "Aditya"},
                        "text": "/pause",
                    },
                },
                {
                    "update_id": 2,
                    "message": {
                        "message_id": 11,
                        "chat": {"id": -100123, "type": "supergroup"},
                        "from": {"id": 2, "username": "KarnaBot", "is_bot": True},
                        "text": "Continuing anyway.",
                    },
                },
            ]

            await runner.poll_once()

            history = await relay.storage.fetch_history("telegram:lab", 10)
            assert history[-1].content.startswith("supervisor_block: paused")
            assert ("supervisor-token", "-100123", "delete:11") in client.sent
        finally:
            await relay.shutdown()


@pytest.mark.asyncio
async def test_telegram_inbound_update_normalizes_registered_bot_sender():
    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        try:
            await storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="acme.bob",
                    username="BobAgentBot",
                    token_ref="env:BOB_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            msg = await TelegramTransport(storage, FakeTelegramClient()).normalize_update(
                "acme.alice",
                {
                    "message": {
                        "message_id": 7,
                        "text": "status?",
                        "from": {"is_bot": True, "username": "BobAgentBot"},
                        "chat": {"id": 1234, "type": "private"},
                    }
                },
            )
            assert msg.sender == "acme.bob"
            assert msg.target == "acme.alice"
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
                    identity_id="acme.bob",
                    username="BobAgentBot",
                    token_ref="env:BOB_BOT_TOKEN",
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
                "acme.alice",
                {
                    "message": {
                        "message_id": 8,
                        "text": "review this",
                        "from": {"is_bot": True, "username": "BobAgentBot"},
                        "chat": {"id": -100123, "type": "supergroup", "title": "AgentRelay Lab"},
                    }
                },
            )
            assert msg.sender == "acme.bob"
            assert msg.target is None
            assert msg.room == "telegram:lab"
            assert msg.content == "review this"
        finally:
            await storage.close()


@pytest.mark.asyncio
async def test_telegram_update_rejects_unregistered_sender():
    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        try:
            with pytest.raises(ValueError, match="TELEGRAM_SENDER_NOT_REGISTERED"):
                await TelegramTransport(storage, FakeTelegramClient()).normalize_update(
                    "acme.alice",
                    {
                        "message": {
                            "message_id": 9,
                            "text": "let me in",
                            "from": {"id": 999, "username": "UnknownBot"},
                            "chat": {"id": 1234, "type": "private"},
                        }
                    },
                )
        finally:
            await storage.close()


@pytest.mark.asyncio
async def test_telegram_group_update_rejects_unregistered_group():
    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        try:
            await storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="acme.bob",
                    username="BobAgentBot",
                    token_ref="env:BOB_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            with pytest.raises(ValueError, match="TELEGRAM_GROUP_NOT_REGISTERED"):
                await TelegramTransport(storage, FakeTelegramClient()).normalize_update(
                    "acme.alice",
                    {
                        "message": {
                            "message_id": 10,
                            "text": "review this",
                            "from": {"is_bot": True, "username": "BobAgentBot"},
                            "chat": {"id": -100999, "type": "supergroup"},
                        }
                    },
                )
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
                    "from": {"is_bot": True, "username": "BobAgentBot"},
                    "chat": {"id": 1234, "type": "private"},
                },
            }
        ]
        await relay.startup()
        try:
            await relay.storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="acme.alice",
                    username="AliceAgentBot",
                    token_ref="env:ALICE_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await relay.storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="acme.bob",
                    username="BobAgentBot",
                    token_ref="env:BOB_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            monkeypatch.setenv("ALICE_BOT_TOKEN", "alice-token")
            monkeypatch.setenv("BOB_BOT_TOKEN", "bob-token")
            await TelegramBotRunner(relay, client).poll_once()

            history = await relay.storage.fetch_history("telegram:private")
            assert [msg.content for msg in history] == ["status?"]
            assert history[0].sender == "acme.bob"
            assert history[0].target == "acme.alice"
            assert client.update_calls == [("alice-token", None), ("bob-token", None)]
            assert client.sent == []
        finally:
            await relay.shutdown()


@pytest.mark.asyncio
async def test_telegram_runner_does_not_advance_offset_when_processing_fails(monkeypatch):
    class FailingRelay:
        def __init__(self, storage: Storage):
            self.storage = storage

        async def process(self, *_args, **_kwargs):
            raise RuntimeError("storage unavailable")

    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        try:
            await storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="acme.alice",
                    username="AliceAgentBot",
                    token_ref="env:ALICE_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="acme.bob",
                    username="BobAgentBot",
                    token_ref="env:BOB_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            monkeypatch.setenv("ALICE_BOT_TOKEN", "alice-token")
            client = FakeTelegramClient()
            update = {
                "update_id": 10,
                "message": {
                    "message_id": 7,
                    "text": "status?",
                    "from": {"is_bot": True, "username": "BobAgentBot"},
                    "chat": {"id": 1234, "type": "private"},
                },
            }
            runner = TelegramBotRunner(FailingRelay(storage), client)

            with pytest.raises(RuntimeError, match="storage unavailable"):
                await runner._handle_update(
                    TelegramTransport(storage, client),
                    "acme.alice",
                    update,
                )

            assert runner._offsets == {}
        finally:
            await storage.close()


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
                    identity_id="acme.alice",
                    username="AliceAgentBot",
                    token_ref="env:ALICE_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await relay.storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="acme.bob",
                    username="BobAgentBot",
                    token_ref="env:BOB_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            monkeypatch.setenv("ALICE_BOT_TOKEN", "alice-token")
            result = await relay.process(
                {"content": "report ready", "target": "acme.bob"},
                "acme.alice",
                "general",
            )
            assert result["status"] == "sent"
            assert result["delivered_to"] == 1
            assert client.sent == [("alice-token", "@BobAgentBot", "report ready")]
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
                    identity_id="acme.alice",
                    username="AliceAgentBot",
                    token_ref="env:ALICE_BOT_TOKEN",
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
            monkeypatch.setenv("ALICE_BOT_TOKEN", "alice-token")
            result = await relay.process(
                {"content": "group-visible update"},
                "acme.alice",
                "telegram:lab",
            )
            assert result["status"] == "sent"
            assert result["delivered_to"] == 1
            assert client.sent == [("alice-token", "-100123", "group-visible update")]
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
                    identity_id="acme.alice",
                    username="AliceAgentBot",
                    token_ref="env:ALICE_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            await relay.storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id="acme.bob",
                    username="BobAgentBot",
                    token_ref="env:BOB_BOT_TOKEN",
                    bot_to_bot_enabled=True,
                )
            )
            monkeypatch.setenv("ALICE_BOT_TOKEN", "alice-token")
            monkeypatch.setenv("BOB_BOT_TOKEN", "bob-token")

            started = time.perf_counter()
            await TelegramBotRunner(relay, client).poll_once()
            elapsed = time.perf_counter() - started

            assert sorted(client.update_calls) == [("alice-token", None), ("bob-token", None)]
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
                    identity_id="acme.alice",
                    username="AliceAgentBot",
                    token_ref="env:ALICE_BOT_TOKEN",
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
            monkeypatch.setenv("ALICE_BOT_TOKEN", "alice-token")
            result = await relay.process(
                {"content": "group-visible update", "skip_transport_delivery": True},
                "acme.alice",
                "telegram:lab",
            )
            assert result["status"] == "sent"
            assert result["delivered_to"] == 1
            # Telegram delivery should still happen because sender is not telegram./system
            assert client.sent == [("alice-token", "-100123", "group-visible update")]
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
