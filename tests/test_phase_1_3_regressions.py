from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentrelay.models import GovernanceConfig, Message
from agentrelay.relay import Relay, create_app
from agentrelay.storage import Storage


@pytest.mark.asyncio
async def test_dm_delivery_count_is_int_for_online_and_offline_targets():
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(
            token="test-token",
            db_path=str(Path(td) / "relay.db"),
            governance=GovernanceConfig(cooldown_seconds=0),
        )
        sent: list[tuple[str, str, dict]] = []

        async def fake_send(agent_id: str, room: str, payload: dict) -> bool:
            sent.append((agent_id, room, payload))
            return True

        relay._send = fake_send  # type: ignore[method-assign]
        await relay.startup()
        try:
            relay._agent_rooms["b"].add("general")
            online = await relay.process({"content": "secret", "target": "b"}, "a", "general")
            assert online["delivered_to"] == 1
            assert isinstance(online["delivered_to"], int)
            assert sent[0][0] == "b"
            assert sent[0][2]["content"] == "secret"

            relay._agent_rooms.pop("b")
            offline = await relay.process({"content": "later", "target": "b"}, "a", "general")
            assert offline["delivered_to"] == 0
            assert isinstance(offline["delivered_to"], int)
        finally:
            await relay.shutdown()


def test_governance_terminal_thread_cleanup_after_auto_close():
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(
            token="test-token",
            db_path=str(Path(td) / "relay.db"),
            governance=GovernanceConfig(cooldown_seconds=0, per_thread_cap=1, per_agent_thread_cap=10),
        )
        app = create_app(relay)
        with TestClient(app) as client:
            with client.websocket_connect("/ws?token=test-token&agent=a&room=general") as a:
                a.send_json({"content": "cap me", "thread": "t1"})
                a.receive_json()  # broadcast
                a.receive_json()  # ack
                a.receive_json()  # auto-close system event
                assert "general:t1" not in relay.governance._thread_counts
                assert "general:t1" not in relay.governance._agent_thread_counts
                assert "general:t1" not in relay.governance._thread_last_activity


@pytest.mark.asyncio
async def test_governance_export_import_omits_closed_threads_on_startup():
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "relay.db")
        storage = Storage(db)
        await storage.init()
        msg = Message(sender="a", room="general", content="done", thread="old-thread")
        await storage.store_message(msg)
        await storage.update_thread_state("old-thread", "closed")
        await storage.store_governance(
            {
                "agent_thread_counts": {"general:old-thread": {"a": 1}},
                "thread_counts": {"general:old-thread": 1},
                "cooldowns": {},
                "global_rates": {},
                "thread_last_activity": {"general:old-thread": 1.0},
                "metrics": {},
            }
        )
        await storage.close()

        relay = Relay(token="test-token", db_path=db)
        await relay.startup()
        try:
            assert "general:old-thread" not in relay.governance._thread_counts
            assert "general:old-thread" not in relay.governance._agent_thread_counts
            assert "general:old-thread" not in relay.governance._thread_last_activity
        finally:
            await relay.shutdown()
