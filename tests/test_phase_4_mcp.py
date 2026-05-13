"""Tests proving multiple agents (bheem/aarya) maintain separate identities
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
    c1 = pool.get("bheem", "team")
    c2 = pool.get("bheem", "team")
    assert c1 is c2
    assert c1.agent == "bheem"
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
    bheem = pool.get("bheem", "team")
    aarya = pool.get("aarya", "team")
    assert bheem is not aarya
    assert bheem.agent == "bheem"
    assert aarya.agent == "aarya"


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
    client = pool.get("bheem")
    assert client.agent == "bheem"
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
    """Connect bheem and aarya to the relay; verify each receives messages
    from the other with the correct sender field."""
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(
            token="test-token",
            db_path=str(Path(td) / "relay.db"),
        )
        app = create_app(relay)
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as ws_bheem,
                client.websocket_connect("/ws") as ws_aarya,
            ):
                auth_ws(ws_bheem, "bheem", "team")
                auth_ws(ws_aarya, "aarya", "team")
                # bheem receives aarya's join notification
                join_msg = ws_bheem.receive_json()
                assert join_msg["type"] == "join"
                assert join_msg["sender"] == "system"

                # aarya sends a DM to bheem
                ws_aarya.send_json({"content": "hello bheem", "target": "bheem"})
                # bheem receives the DM
                dm = ws_bheem.receive_json()
                assert dm["content"] == "hello bheem"
                assert dm["sender"] == "aarya"
                assert dm["target"] == "bheem"

                # aarya receives ack for her DM send
                ack = ws_aarya.receive_json()
                assert ack["content"] == "ack"


def test_list_agents_shows_both_connected_agents():
    """After connecting bheem and aarya, the /agents endpoint returns both."""
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(
            token="test-token",
            db_path=str(Path(td) / "relay.db"),
        )
        app = create_app(relay)
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as ws_bheem,
                client.websocket_connect("/ws") as ws_aarya,
            ):
                auth_ws(ws_bheem, "bheem", "team")
                auth_ws(ws_aarya, "aarya", "team")
                agents = client.get("/agents", headers=AUTH_HEADERS).json()
                agent_ids = {a["id"] for a in agents}
                assert "bheem" in agent_ids
                assert "aarya" in agent_ids


def test_bheem_broadcast_arrives_at_aarya_with_correct_sender():
    """bheem broadcasts a message; aarya receives it with sender='bheem'."""
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(
            token="test-token",
            db_path=str(Path(td) / "relay.db"),
        )
        app = create_app(relay)
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as ws_bheem,
                client.websocket_connect("/ws") as ws_aarya,
            ):
                auth_ws(ws_bheem, "bheem", "team")
                auth_ws(ws_aarya, "aarya", "team")
                # discard join notification
                ws_bheem.receive_json()

                ws_bheem.send_json({"content": "standup in 5"})

                # bheem receives its own broadcast echo
                bcast_echo = ws_bheem.receive_json()
                assert bcast_echo["sender"] == "bheem"
                assert bcast_echo["content"] == "standup in 5"

                # bheem receives ack
                ack = ws_bheem.receive_json()
                assert ack["content"] == "ack"

                # aarya receives the broadcast
                broadcast = ws_aarya.receive_json()
                assert broadcast["sender"] == "bheem"
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
