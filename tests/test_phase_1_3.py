from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentrelay.governance import GovernanceEngine
from agentrelay.models import GovernanceConfig, Message
from agentrelay.relay import Relay, create_app
from agentrelay.storage import Storage


def auth_ws(ws, agent: str, room: str = "general") -> None:
    ws.send_json({"type": "auth", "token": "test-token", "agent": agent, "room": room})


AUTH_HEADERS = {"Authorization": "Bearer test-token"}


@pytest.mark.asyncio
async def test_storage_message_sequence_and_history():
    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        first = Message(sender="a", room="general", content="one")
        second = Message(sender="b", room="general", content="two")
        await storage.store_message(first)
        await storage.store_message(second)
        history = await storage.fetch_history("general")
        assert [m.seq for m in history] == [1, 2]
        assert [m.content for m in history] == ["one", "two"]
        await storage.close()


def test_governance_cooldown_rate_and_thread_caps():
    gov = GovernanceEngine(
        GovernanceConfig(cooldown_seconds=0, rate_limit=2, per_thread_cap=2, per_agent_thread_cap=1)
    )
    assert gov.check("a", "r", "t") == (True, None, None)
    gov.record("a", "r", "t")
    ok, error, _ = gov.check("a", "r", "t")
    assert not ok and error == "AGENT_THREAD_CAP_REACHED"
    assert gov.check("b", "r", "t") == (True, None, None)
    gov.record("b", "r", "t")
    ok, error, _ = gov.check("c", "r", "t")
    assert not ok and error == "THREAD_CAP_REACHED"
    assert gov.should_auto_close("r", "t")


def test_http_health_and_empty_rooms():
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        app = create_app(relay)
        with TestClient(app) as client:
            assert client.get("/").json()["status"] == "ok"
            assert client.get("/rooms", headers=AUTH_HEADERS).json() == []
            assert client.get("/agents", headers=AUTH_HEADERS).json() == []


def test_websocket_exchange_and_history():
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(
            token="test-token",
            db_path=str(Path(td) / "relay.db"),
            governance=GovernanceConfig(cooldown_seconds=0),
        )
        app = create_app(relay)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as a:
                auth_ws(a, "a")
                with client.websocket_connect("/ws") as b:
                    auth_ws(b, "b")
                    # a receives b's join notification.
                    join = a.receive_json()
                    assert join["type"] == "join"
                    a.send_json({"content": "hello", "thread": "topic-1"})
                    broadcast = a.receive_json()
                    ack = a.receive_json()
                    received = b.receive_json()
                    assert broadcast["content"] == "hello"
                    assert received["content"] == "hello"
                    assert ack["content"] == "ack"
            history = client.get("/rooms/general/history", headers=AUTH_HEADERS).json()
            assert len(history) == 1
            assert history[0]["content"] == "hello"


def test_invalid_token_rejected():
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        app = create_app(relay)
        with TestClient(app) as client:
            with pytest.raises(Exception):
                with client.websocket_connect("/ws") as ws:
                    ws.send_json({"type": "auth", "token": "bad", "agent": "a", "room": "general"})
                    ws.receive_json()
