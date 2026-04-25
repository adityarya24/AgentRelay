"""MCP stdio/SSE server for AgentRelay v2."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote

import websockets
from mcp.server.fastmcp import FastMCP

DEFAULT_RELAY_URL = "ws://localhost:8899"
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_RESPONSE_TIMEOUT = 10.0


def discover_token(token: str | None = None) -> str:
    """Return explicit/env/file token, creating a local token when needed."""
    if token:
        return token
    if env_token := os.getenv("AGENTRELAY_TOKEN"):
        return env_token
    token_path = Path.home() / ".agentrelay" / "token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if token_path.exists():
        return token_path.read_text().strip()
    generated = secrets.token_urlsafe(32)
    token_path.write_text(generated)
    token_path.chmod(0o600)
    return generated


def _relay_url(url: str | None = None) -> str:
    base = url or os.getenv("AGENTRELAY_URL") or DEFAULT_RELAY_URL
    if base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    elif base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    return base.rstrip("/")


def _ws_url(relay_url: str, token: str, agent: str, room: str) -> str:
    return (
        f"{_relay_url(relay_url)}/ws?token={quote(token)}"
        f"&agent={quote(agent)}&room={quote(room)}"
    )


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


class RelayClient:
    """Small persistent WebSocket client with reconnect and bounded waits."""

    def __init__(
        self,
        *,
        relay_url: str,
        token: str,
        agent: str,
        room: str,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        response_timeout: float = DEFAULT_RESPONSE_TIMEOUT,
    ):
        self.relay_url = relay_url
        self.token = token
        self.agent = agent
        self.room = room
        self.connect_timeout = connect_timeout
        self.response_timeout = response_timeout
        self._ws: Any | None = None
        self._active_room = room
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _connect(self, room: str) -> Any:
        if self._ws is not None and self._active_room == room:
            return self._ws
        await self.close()
        self._active_room = room
        self._ws = await asyncio.wait_for(
            websockets.connect(_ws_url(self.relay_url, self.token, self.agent, room)),
            timeout=self.connect_timeout,
        )
        return self._ws

    async def _roundtrip(self, payload: dict[str, Any], *, room: str | None = None) -> dict[str, Any]:
        target_room = room or self.room
        ws = await self._connect(target_room)
        await ws.send(json.dumps(payload))
        deadline = asyncio.get_running_loop().time() + self.response_timeout
        last: dict[str, Any] | None = None
        while asyncio.get_running_loop().time() < deadline:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=max(0.1, deadline - asyncio.get_running_loop().time())
            )
            msg = json.loads(raw)
            last = msg
            if msg.get("content") == "ack" or msg.get("error") or msg.get("status"):
                return msg
        return last or {"status": "sent"}

    async def send(self, payload: dict[str, Any], *, room: str | None = None) -> dict[str, Any]:
        async with self._lock:
            try:
                return await self._roundtrip(payload, room=room)
            except (TimeoutError, OSError, websockets.WebSocketException) as first_error:
                await self.close()
                try:
                    return await self._roundtrip(payload, room=room)
                except (TimeoutError, OSError, websockets.WebSocketException) as second_error:
                    await self.close()
                    return {
                        "error": "RELAY_UNAVAILABLE",
                        "detail": str(second_error or first_error),
                    }

    async def join(self, room: str) -> dict[str, Any]:
        async with self._lock:
            try:
                ws = await self._connect(self.room)
                await ws.send(json.dumps({"type": "join", "room": room}))
                self._active_room = room
                return {"status": "joined", "room": room}
            except (TimeoutError, OSError, websockets.WebSocketException) as exc:
                await self.close()
                return {"error": "RELAY_UNAVAILABLE", "detail": str(exc)}

    async def close_thread(self, thread: str, *, room: str | None = None) -> dict[str, Any]:
        return await self.send({"type": "close", "thread": thread}, room=room)


async def _get_json(url: str) -> Any:
    from urllib.request import urlopen

    http_url = url.replace("ws://", "http://", 1).replace("wss://", "https://", 1)

    def fetch() -> Any:
        with urlopen(http_url, timeout=10) as response:  # noqa: S310 - local/admin configured URL
            return json.loads(response.read().decode("utf-8"))

    return await asyncio.to_thread(fetch)


def create_mcp_server(
    *,
    relay_url: str | None = None,
    token: str | None = None,
    agent: str | None = None,
    room: str | None = None,
) -> FastMCP:
    resolved_url = _relay_url(relay_url)
    resolved_token = discover_token(token)
    resolved_agent = agent or os.getenv("AGENTRELAY_AGENT", "mcp")
    default_room = room or os.getenv("AGENTRELAY_ROOM", "general")
    client = RelayClient(
        relay_url=resolved_url,
        token=resolved_token,
        agent=resolved_agent,
        room=default_room,
        connect_timeout=float(os.getenv("AGENTRELAY_MCP_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT)),
        response_timeout=float(os.getenv("AGENTRELAY_MCP_RESPONSE_TIMEOUT", DEFAULT_RESPONSE_TIMEOUT)),
    )
    mcp = FastMCP("agentrelay")

    @mcp.tool()
    async def send_message(content: str, thread: str | None = None) -> str:
        """Send a message to the default AgentRelay room."""
        result = await client.send({"content": content, "thread": thread})
        return _json(result)

    @mcp.tool()
    async def send_dm(target: str, content: str) -> str:
        """Send a direct message to one agent, queueing if they are offline."""
        result = await client.send({"content": content, "target": target})
        return _json(result)

    @mcp.tool()
    async def read_messages(limit: int = 20, room: str | None = None) -> str:
        """Read recent messages from a room."""
        active_room = room or default_room
        data = await _get_json(f"{resolved_url}/rooms/{quote(active_room)}/history?limit={int(limit)}")
        return _json(data)

    @mcp.tool()
    async def list_rooms() -> str:
        """List active rooms."""
        data = await _get_json(f"{resolved_url}/rooms")
        return _json(data)

    @mcp.tool()
    async def list_agents(room: str | None = None) -> str:
        """List connected agents, optionally filtered by room."""
        data = await _get_json(f"{resolved_url}/agents")
        if room:
            data = [agent for agent in data if room in agent.get("rooms", []) or agent.get("room") == room]
        return _json(data)

    @mcp.tool()
    async def join_room(room: str) -> str:
        """Join a room once to announce/validate access."""
        result = await client.join(room)
        return _json(result)

    @mcp.tool()
    async def close_thread(thread: str) -> str:
        """Close a thread in the default room."""
        result = await client.close_thread(thread)
        return _json(result)

    return mcp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentrelay-mcp")
    parser.add_argument("--url", default=os.getenv("AGENTRELAY_URL", DEFAULT_RELAY_URL))
    parser.add_argument("--token")
    parser.add_argument("--agent", default=os.getenv("AGENTRELAY_AGENT", "mcp"))
    parser.add_argument("--room", default=os.getenv("AGENTRELAY_ROOM", "general"))
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8898)
    return parser


def cli_main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    mcp = create_mcp_server(relay_url=args.url, token=args.token, agent=args.agent, room=args.room)
    if args.transport == "sse":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
    mcp.run(args.transport)


if __name__ == "__main__":
    cli_main()
