from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentrelay.mcp import RelayClient, create_mcp_server, discover_token
from agentrelay.relay import Relay, create_app


@pytest.mark.asyncio
async def test_mcp_server_exposes_all_phase_4_tools():
    mcp = create_mcp_server(token="test-token")
    tools = await mcp.list_tools()
    assert {tool.name for tool in tools} == {
        "send_message",
        "send_dm",
        "read_messages",
        "list_rooms",
        "list_agents",
        "join_room",
        "close_thread",
    }


def test_discover_token_reads_existing_local_token(monkeypatch: pytest.MonkeyPatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("HOME", td)
        monkeypatch.delenv("AGENTRELAY_TOKEN", raising=False)
        token_path = Path(td) / ".agentrelay" / "token"
        token_path.parent.mkdir(parents=True)
        token_path.write_text("local-token")
        assert discover_token() == "local-token"


def test_agentrelay_mcp_entry_point_importable():
    from agentrelay.mcp import cli_main

    assert callable(cli_main)


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


def test_relay_basic_websocket_connection_for_mcp_client():
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        app = create_app(relay)
        with TestClient(app) as client:
            with client.websocket_connect("/ws?token=test-token&agent=mcp&room=general") as ws:
                ws.send_json({"content": "mcp connection ok"})
                assert ws.receive_json()["content"] == "mcp connection ok"
                assert ws.receive_json()["content"] == "ack"
