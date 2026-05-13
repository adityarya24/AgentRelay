from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agentrelay.cli import main
from agentrelay.models import (
    Identity,
    IdentityKind,
    IdentityTransport,
    PolicyEffect,
    PolicyRule,
    TrustLevel,
)
from agentrelay.policy import PolicyEngine
from agentrelay.relay import Relay
from agentrelay.storage import Storage


@pytest.mark.asyncio
async def test_policy_denies_disabled_identity_and_requires_external_allow():
    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        try:
            await storage.upsert_identity(
                Identity(
                    id="external.somebot",
                    display_name="Some Bot",
                    kind=IdentityKind.EXTERNAL,
                    transport=IdentityTransport.TELEGRAM,
                    enabled=True,
                    trust_level=TrustLevel.SANDBOX,
                )
            )
            engine = PolicyEngine(storage)

            blocked = await engine.evaluate("external.somebot", "acme.alice", "general")
            assert blocked.allowed is False
            assert blocked.reason == "ALLOW_REQUIRED"

            await storage.upsert_policy(
                PolicyRule(
                    id="allow-general",
                    source="external.somebot",
                    target="acme.alice",
                    room="general",
                    effect=PolicyEffect.ALLOW,
                )
            )
            allowed = await engine.evaluate("external.somebot", "acme.alice", "general")
            assert allowed.allowed is True
            assert allowed.rule_id == "allow-general"

            wrong_room = await engine.evaluate("external.somebot", "acme.alice", "private")
            assert wrong_room.allowed is False
            assert wrong_room.reason == "ALLOW_REQUIRED"

            await storage.set_identity_enabled("external.somebot", False)
            disabled = await engine.evaluate("external.somebot", "acme.alice", "general")
            assert disabled.allowed is False
            assert disabled.reason == "IDENTITY_DISABLED"
        finally:
            await storage.close()


@pytest.mark.asyncio
async def test_policy_explicit_deny_wins_over_allow():
    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        try:
            await storage.upsert_policy(
                PolicyRule(
                    id="allow",
                    source="acme.bob",
                    target="acme.alice",
                    room="general",
                    effect=PolicyEffect.ALLOW,
                )
            )
            await storage.upsert_policy(
                PolicyRule(
                    id="deny",
                    source="acme.bob",
                    target="acme.alice",
                    room="general",
                    effect=PolicyEffect.DENY,
                    reason="maintenance",
                )
            )

            decision = await PolicyEngine(storage).evaluate(
                "acme.bob", "acme.alice", "general"
            )
            assert decision.allowed is False
            assert decision.reason == "POLICY_DENY"
            assert decision.rule_id == "deny"
        finally:
            await storage.close()


@pytest.mark.asyncio
async def test_policy_allow_required_respects_room_scope():
    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        try:
            await storage.upsert_policy(
                PolicyRule(
                    id="allow-private",
                    source="acme.bob",
                    target="acme.alice",
                    room="private",
                    effect=PolicyEffect.ALLOW,
                )
            )

            decision = await PolicyEngine(storage).evaluate(
                "acme.bob", "acme.alice", "general"
            )
            assert decision.allowed is True
            assert decision.reason == "DEFAULT_ALLOW"
        finally:
            await storage.close()


def test_policy_cli_adds_and_lists_room_scoped_rule(capsys):
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "relay.db")
        main(
            [
                "policy",
                "--db-path",
                db_path,
                "allow",
                "acme.bob",
                "acme.alice",
                "--room",
                "general",
                "--reason",
                "handoff",
                "--json",
            ]
        )
        saved = json.loads(capsys.readouterr().out)
        assert saved["source"] == "acme.bob"
        assert saved["target"] == "acme.alice"
        assert saved["room"] == "general"
        assert saved["effect"] == "allow"

        main(["policy", "--db-path", db_path, "list", "--json"])
        listed = json.loads(capsys.readouterr().out)
        assert [rule["id"] for rule in listed] == [saved["id"]]


@pytest.mark.asyncio
async def test_relay_blocks_dm_when_policy_denies_target_route():
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        await relay.startup()
        try:
            await relay.storage.upsert_policy(
                PolicyRule(
                    id="deny-dm",
                    source="acme.bob",
                    target="acme.alice",
                    room="general",
                    effect=PolicyEffect.DENY,
                )
            )

            result = await relay.process(
                {"content": "secret", "target": "acme.alice"},
                "acme.bob",
                "general",
            )
            assert result["error"] == "POLICY_DENY"
            assert relay.counters["messages_rejected"] == 1
            assert await relay.storage.fetch_history("general") == []
        finally:
            await relay.shutdown()


