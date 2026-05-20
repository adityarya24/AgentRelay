from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentrelay.governance import GovernanceEngine
from agentrelay.models import GovernanceConfig, Message
from agentrelay.relay import Relay, create_app, ensure_token
from agentrelay.storage import Storage


def auth_ws(ws, agent: str, room: str = "general") -> None:
    ws.send_json({"type": "auth", "token": "test-token", "agent": agent, "room": room})


AUTH_HEADERS = {"Authorization": "Bearer test-token"}


def test_ensure_token_reads_token_file_env(monkeypatch: pytest.MonkeyPatch):
    with tempfile.TemporaryDirectory() as td:
        token_path = Path(td) / "token"
        token_path.write_text("file-token\n")
        monkeypatch.delenv("AGENTRELAY_TOKEN", raising=False)
        monkeypatch.setenv("AGENTRELAY_TOKEN_FILE", str(token_path))

        assert ensure_token() == "file-token"


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


def test_ws_auth_rate_limit():
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        app = create_app(relay)
        with TestClient(app) as client:
            for i in range(10):
                with pytest.raises(Exception):
                    with client.websocket_connect("/ws") as ws:
                        ws.send_json({"type": "auth", "token": "bad", "agent": "a", "room": "general"})
                        ws.receive_json()
            # 11th attempt should be rate limited immediately
            with pytest.raises(Exception):
                with client.websocket_connect("/ws") as ws:
                    ws.send_json({"type": "auth", "token": "bad", "agent": "a", "room": "general"})
                    ws.receive_json()
            assert len(relay._auth_failures.get("testclient", [])) >= 10


def test_http_auth_rate_limit():
    with tempfile.TemporaryDirectory() as td:
        relay = Relay(token="test-token", db_path=str(Path(td) / "relay.db"))
        app = create_app(relay)
        with TestClient(app) as client:
            for _ in range(10):
                response = client.get("/rooms", headers={"Authorization": "Bearer bad"})
                assert response.status_code == 401

            response = client.get("/rooms", headers={"Authorization": "Bearer bad"})
            assert response.status_code == 429

            relay._auth_failures["testclient"] = []
            response = client.get("/rooms", headers=AUTH_HEADERS)
            assert response.status_code == 200
            assert "testclient" not in relay._auth_failures


@pytest.mark.asyncio
async def test_cleanup_removes_orphan_closed_threads():
    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        try:
            await storage.store_thread("orphan", "general", "Orphan thread")
            await storage.update_thread_state("orphan", "closed", "general")
            await asyncio.sleep(0.1)
            removed = await storage.cleanup(ttl_hours=0)
            assert removed == 0
            state = await storage.get_thread_state("orphan", "general")
            assert state is None
        finally:
            await storage.close()


def test_start_does_not_print_token(capsys, monkeypatch):
    from agentrelay.cli import cmd_start
    import argparse

    def stop_server(*args, **kwargs):
        raise SystemExit

    monkeypatch.setenv("AGENTRELAY_TOKEN", "test-token")
    monkeypatch.setattr("agentrelay.cli.uvicorn.run", stop_server)
    args = argparse.Namespace(
        host=None, port=None, token=None,
        telegram_token=None, telegram_chat=None, telegram_transport=False,
        db_path=None, config=None,
    )
    with pytest.raises(SystemExit):
        cmd_start(args)
    captured = capsys.readouterr()
    assert "Token: sk_" not in captured.out
    assert "Token: test-token" not in captured.out
    assert "Token file:" in captured.out
    assert "Token env:" in captured.out


def test_start_reports_configured_token_file(capsys, monkeypatch: pytest.MonkeyPatch):
    from agentrelay.cli import cmd_start
    import argparse

    def stop_server(*args, **kwargs):
        raise SystemExit

    with tempfile.TemporaryDirectory() as td:
        token_file = Path(td) / "configured-token"
        token_file.write_text("test-token\n")
        monkeypatch.delenv("AGENTRELAY_TOKEN", raising=False)
        monkeypatch.setenv("AGENTRELAY_TOKEN_FILE", str(token_file))
        monkeypatch.setattr("agentrelay.cli.uvicorn.run", stop_server)
        args = argparse.Namespace(
            host=None, port=None, token=None,
            telegram_token=None, telegram_chat=None, telegram_transport=False,
            db_path=None, config=None,
        )

        with pytest.raises(SystemExit):
            cmd_start(args)

        assert f"Token file: {token_file}" in capsys.readouterr().out


def test_token_path_has_no_generation_side_effect(capsys, monkeypatch: pytest.MonkeyPatch):
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        token_file = home / ".agentrelay" / "token"
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.delenv("AGENTRELAY_TOKEN", raising=False)
        monkeypatch.delenv("AGENTRELAY_TOKEN_FILE", raising=False)

        from agentrelay.cli import main

        main(["token", "--path"])

        assert capsys.readouterr().out.strip() == str(token_file)
        assert not token_file.exists()


def test_token_path_reports_configured_token_file(capsys, monkeypatch: pytest.MonkeyPatch):
    with tempfile.TemporaryDirectory() as td:
        token_file = Path(td) / "configured-token"
        monkeypatch.delenv("AGENTRELAY_TOKEN", raising=False)
        monkeypatch.setenv("AGENTRELAY_TOKEN_FILE", str(token_file))

        from agentrelay.cli import main

        main(["token", "--path"])

        assert capsys.readouterr().out.strip() == str(token_file)
        assert not token_file.exists()
