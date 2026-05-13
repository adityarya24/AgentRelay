"""Tests proving multiple agents (bob/alice) maintain separate identities
through the MCP → relay pipeline, and that list_agents shows both when connected.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentrelay.mcp import ClientPool, RelayClient, create_mcp_server, discover_token
from agentrelay.relay import Relay, create_app


def auth_ws(ws, agent: str, room: str = "general") -> None:
    ws.send_json({"type": "auth", "token": "test-token", "agent": agent, "room": room})


AUTH_HEADERS = {"Authorization": "Bearer test-token"}


# ---------------------------------------------------------------------------
# ClientPool unit tests
# ---------------------------------------------------------------------------

def test_client_pool_returns_same_client_for_same_key():
    pool = ClientPool(
        relay_url="ws://127.0.0.1:9999",
        token="test",
        default_agent="mcp",
        default_room="general",
        connect_timeout=1,
        response_timeout=1,
    )
    c1 = pool.get("bob", "team")
    c2 = pool.get("bob", "team")
    assert c1 is c2
    assert c1.agent == "bob"
    assert c1.room == "team"


def test_client_pool_returns_different_clients_for_different_agents():
    pool = ClientPool(
        relay_url="ws://127.0.0.1:9999",
        token="test",
        default_agent="mcp",
        default_room="general",
        connect_timeout=1,
        response_timeout=1,
    )
    bob = pool.get("bob", "team")
    alice = pool.get("alice", "team")
    assert bob is not alice
    assert bob.agent == "bob"
    assert alice.agent == "alice"


def test_client_pool_falls_back_to_defaults():
    pool = ClientPool(
        relay_url="ws://127.0.0.1:9999",
        token="test",
        default_agent="mcp",
        default_room="general",
        connect_timeout=1,
        response_timeout=1,
    )
    client = pool.get()
    assert client.agent == "mcp"
    assert client.room == "general"


def test_client_pool_agent_override_without_room():
    pool = ClientPool(
        relay_url="ws://127.0.0.1:9999",
        token="test",
        default_agent="mcp",
        default_room="general",
        connect_timeout=1,
        response_timeout=1,
    )
    client = pool.get("bob")
    assert client.agent == "bob"
    assert client.room == "general"


# ---------------------------------------------------------------------------
# MCP tool surface tests (no live relay)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_server_exposes_all_phase_4_tools():
    mcp = create_mcp_server(token="test-token")
    tools = await mcp.list_tools()
    tool_names = {tool.name for tool in tools}
    # New tools with from_agent param should be present
    assert "send_message" in tool_names
    assert "send_dm" in tool_names
    assert "read_messages" in tool_names
    assert "list_rooms" in tool_names
    assert "list_agents" in tool_names
    assert "join_room" in tool_names
    assert "close_thread" in tool_names


# ---------------------------------------------------------------------------
# Multi-agent identity through the relay server
# ---------------------------------------------------------------------------

def test_two_agents_see_correct_sender_fields():
    """Connect bob and alice to the relay; verify each receives messages
    from the other with the correct sender field."""
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(
            token="test-token",
            db_path=str(Path(td) / "relay.db"),
        )
        app = create_app(relay)
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as ws_bob,
                client.websocket_connect("/ws") as ws_alice,
            ):
                auth_ws(ws_bob, "bob", "team")
                auth_ws(ws_alice, "alice", "team")
                # bob receives alice's join notification
                join_msg = ws_bob.receive_json()
                assert join_msg["type"] == "join"
                assert join_msg["sender"] == "system"

                # alice sends a DM to bob
                ws_alice.send_json({"content": "hello bob", "target": "bob"})
                # bob receives the DM
                dm = ws_bob.receive_json()
                assert dm["content"] == "hello bob"
                assert dm["sender"] == "alice"
                assert dm["target"] == "bob"

                # alice receives ack for her DM send
                ack = ws_alice.receive_json()
                assert ack["content"] == "ack"


def test_list_agents_shows_both_connected_agents():
    """After connecting bob and alice, the /agents endpoint returns both."""
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(
            token="test-token",
            db_path=str(Path(td) / "relay.db"),
        )
        app = create_app(relay)
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as ws_bob,
                client.websocket_connect("/ws") as ws_alice,
            ):
                auth_ws(ws_bob, "bob", "team")
                auth_ws(ws_alice, "alice", "team")
                agents = client.get("/agents", headers=AUTH_HEADERS).json()
                agent_ids = {a["id"] for a in agents}
                assert "bob" in agent_ids
                assert "alice" in agent_ids


def test_bob_broadcast_arrives_at_alice_with_correct_sender():
    """bob broadcasts a message; alice receives it with sender='bob'."""
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(
            token="test-token",
            db_path=str(Path(td) / "relay.db"),
        )
        app = create_app(relay)
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as ws_bob,
                client.websocket_connect("/ws") as ws_alice,
            ):
                auth_ws(ws_bob, "bob", "team")
                auth_ws(ws_alice, "alice", "team")
                # discard join notification
                ws_bob.receive_json()

                ws_bob.send_json({"content": "standup in 5"})

                # bob receives its own broadcast echo
                bcast_echo = ws_bob.receive_json()
                assert bcast_echo["sender"] == "bob"
                assert bcast_echo["content"] == "standup in 5"

                # bob receives ack
                ack = ws_bob.receive_json()
                assert ack["content"] == "ack"

                # alice receives the broadcast
                broadcast = ws_alice.receive_json()
                assert broadcast["sender"] == "bob"
                assert broadcast["content"] == "standup in 5"


# ---------------------------------------------------------------------------
# RelayClient reconnect tests (preserved)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_client_returns_error_when_relay_down():
    client = RelayClient(
        relay_url="ws://127.0.0.1:9",
        token="bad",
        agent="mcp",
        room="general",
        connect_timeout=0.1,
        response_timeout=0.1,
    )
    result = await client.send({"content": "will fail"})
    assert result["error"] == "RELAY_UNAVAILABLE"


def test_discover_token_reads_existing_local_token(monkeypatch: pytest.MonkeyPatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("HOME", td)
        monkeypatch.delenv("AGENTRELAY_TOKEN", raising=False)
        token_path = Path(td) / ".agentrelay" / "token"
        token_path.parent.mkdir(parents=True)
        token_path.write_text("local-token")
        assert discover_token() == "local-token"


def test_discover_token_reads_token_file_env(monkeypatch: pytest.MonkeyPatch):
    with tempfile.TemporaryDirectory() as td:
        token_path = Path(td) / "token"
        token_path.write_text("file-token\n")
        monkeypatch.delenv("AGENTRELAY_TOKEN", raising=False)
        monkeypatch.setenv("AGENTRELAY_TOKEN_FILE", str(token_path))

        assert discover_token() == "file-token"


def test_relay_basic_websocket_connection_for_mcp_client():
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        app = create_app(relay)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                auth_ws(ws, "mcp")
                ws.send_json({"content": "mcp connection ok"})
                msg = ws.receive_json()
                assert msg["content"] == "mcp connection ok"
                ack = ws.receive_json()
                assert ack["content"] == "ack"
