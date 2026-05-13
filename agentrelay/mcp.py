"""MCP stdio/SSE server for AgentRelay v2 — multi-agent identity support.

Each MCP tool accepts an optional ``sender`` / ``from_agent`` parameter so
multiple your MCP client agents sharing one MCP server can each use their own
identity.  The server keeps a pool of :class:`RelayClient` instances keyed
by ``(agent, room)`` and reuses them across calls.

If no per-call identity is given, the server falls back to the default
agent name configured via ``--agent``, ``AGENTRELAY_AGENT``, or the
hard-coded ``"mcp"`` default.

your MCP client agents configure their identity by adding connection params to
the MCP server config, e.g.:

.. code-block:: json

    {
      "mcpServers": {
        "agentrelay": {
          "command": "agentrelay-mcp",
          "args": ["--agent", "bob", "--room", "team"],
          "env": {"AGENTRELAY_URL": "ws://localhost:8899"}
        }
    }
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request

import websockets
from mcp.server.fastmcp import FastMCP

DEFAULT_RELAY_URL = "ws://localhost:8899"
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_RESPONSE_TIMEOUT = 10.0
RELAY_UNAVAILABLE_ERRORS = (
    TimeoutError,
    asyncio.TimeoutError,
    OSError,
    websockets.WebSocketException,
)


def agentrelay_home() -> Path:
    return Path(os.getenv("AGENTRELAY_HOME") or os.getenv("HOME") or Path.home()).expanduser()


def discover_token(token: str | None = None) -> str:
    """Return explicit/env/file token, creating a local token when needed."""
    if token:
        return token
    if env_token := os.getenv("AGENTRELAY_TOKEN"):
        return env_token
    if token_file := os.getenv("AGENTRELAY_TOKEN_FILE"):
        return Path(token_file).expanduser().read_text().strip()
    token_path = agentrelay_home() / ".agentrelay" / "token"
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
        base = "ws://" + base[len("http://"):]
    elif base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    return base.rstrip("/")


def _ws_url(relay_url: str, token: str, agent: str, room: str) -> str:
    return f"{_relay_url(relay_url)}/ws"


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
        await self._ws.send(
            json.dumps(
                {
                    "type": "auth",
                    "token": self.token,
                    "agent": self.agent,
                    "room": room,
                }
            )
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
            except RELAY_UNAVAILABLE_ERRORS as first_error:
                await self.close()
                try:
                    return await self._roundtrip(payload, room=room)
                except RELAY_UNAVAILABLE_ERRORS as second_error:
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
            except RELAY_UNAVAILABLE_ERRORS as exc:
                await self.close()
                return {"error": "RELAY_UNAVAILABLE", "detail": str(exc)}

    async def close_thread(self, thread: str, *, room: str | None = None) -> dict[str, Any]:
        return await self.send({"type": "close", "thread": thread}, room=room)


class ClientPool:
    """Pool of RelayClient instances keyed by ``(agent, room)``.

    Each unique (agent, room) pair gets its own persistent WebSocket so
    messages are sent with the correct sender identity.  The pool is lazy —
    clients are created on first use.
    """

    def __init__(
        self,
        *,
        relay_url: str,
        token: str,
        default_agent: str,
        default_room: str,
        connect_timeout: float,
        response_timeout: float,
    ):
        self.relay_url = relay_url
        self.token = token
        self.default_agent = default_agent
        self.default_room = default_room
        self.connect_timeout = connect_timeout
        self.response_timeout = response_timeout
        self._clients: dict[tuple[str, str], RelayClient] = {}

    def get(self, agent: str | None = None, room: str | None = None) -> RelayClient:
        a = agent or self.default_agent
        r = room or self.default_room
        key = (a, r)
        if key not in self._clients:
            self._clients[key] = RelayClient(
                relay_url=self.relay_url,
                token=self.token,
                agent=a,
                room=r,
                connect_timeout=self.connect_timeout,
                response_timeout=self.response_timeout,
            )
        return self._clients[key]

    async def close_all(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()


async def _get_json(url: str, token: str | None = None) -> Any:
    from urllib.request import urlopen

    http_url = url.replace("ws://", "http://", 1).replace("wss://", "https://", 1)

    def fetch() -> Any:
        request = Request(http_url)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urlopen(request, timeout=10) as response:  # noqa: S310 - local/admin configured URL
            return json.loads(response.read().decode("utf-8"))

    return await asyncio.to_thread(fetch)


def create_mcp_server(
    *,
    relay_url: str | None = None,
    token: str | None = None,
    agent: str | None = None,
    room: str | None = None,
) -> FastMCP:
    """Build the MCP server with per-call identity support.

    Every tool that sends a message accepts an optional ``from_agent``
    parameter.  When provided, the pool creates (or reuses) a dedicated
    RelayClient that connects under that agent name, so the relay server
    records the correct sender identity.
    """
    resolved_url = _relay_url(relay_url)
    resolved_token = discover_token(token)
    resolved_agent = agent or os.getenv("AGENTRELAY_AGENT", "mcp")
    default_room = room or os.getenv("AGENTRELAY_ROOM", "general")

    pool = ClientPool(
        relay_url=resolved_url,
        token=resolved_token,
        default_agent=resolved_agent,
        default_room=default_room,
        connect_timeout=float(os.getenv("AGENTRELAY_MCP_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT)),
        response_timeout=float(os.getenv("AGENTRELAY_MCP_RESPONSE_TIMEOUT", DEFAULT_RESPONSE_TIMEOUT)),
    )
    mcp = FastMCP("agentrelay")

    @mcp.tool()
    async def send_message(
        content: str,
        thread: str | None = None,
        from_agent: str | None = None,
        room: str | None = None,
    ) -> str:
        """Send a message to an AgentRelay room.

        Args:
            content: The message text.
            thread: Optional thread / topic ID.
            from_agent: Override the sender identity (e.g. ``"bob"``).
                When omitted, the server default is used.
            room: Override the target room.
        """
        client = pool.get(from_agent, room)
        payload: dict[str, Any] = {"content": content}
        if thread:
            payload["thread"] = thread
        result = await client.send(payload, room=room)
        return _json(result)

    @mcp.tool()
    async def send_dm(
        target: str,
        content: str,
        from_agent: str | None = None,
    ) -> str:
        """Send a direct message to one agent, queueing if they are offline.

        Args:
            target: Recipient agent name.
            content: The message text.
            from_agent: Override the sender identity.
        """
        client = pool.get(from_agent)
        result = await client.send({"content": content, "target": target})
        return _json(result)

    @mcp.tool()
    async def read_messages(limit: int = 20, room: str | None = None) -> str:
        """Read recent messages from a room."""
        active_room = room or default_room
        data = await _get_json(
            f"{resolved_url}/rooms/{quote(active_room)}/history?limit={int(limit)}",
            resolved_token,
        )
        return _json(data)

    @mcp.tool()
    async def list_rooms() -> str:
        """List active rooms."""
        data = await _get_json(f"{resolved_url}/rooms", resolved_token)
        return _json(data)

    @mcp.tool()
    async def list_agents(room: str | None = None) -> str:
        """List connected agents, optionally filtered by room."""
        data = await _get_json(f"{resolved_url}/agents", resolved_token)
        if room:
            data = [a for a in data if room in a.get("rooms", []) or a.get("room") == room]
        return _json(data)

    @mcp.tool()
    async def join_room(
        room: str,
        from_agent: str | None = None,
    ) -> str:
        """Join a room to announce/validate access.

        Args:
            room: Room name to join.
            from_agent: Override the agent identity for this join.
        """
        client = pool.get(from_agent, room)
        result = await client.join(room)
        return _json(result)

    @mcp.tool()
    async def close_thread(
        thread: str,
        room: str | None = None,
        from_agent: str | None = None,
    ) -> str:
        """Close a thread.

        Args:
            thread: Thread / topic ID to close.
            room: Override the target room.
            from_agent: Override the sender identity.
        """
        client = pool.get(from_agent, room)
        result = await client.close_thread(thread, room=room)
        return _json(result)

    return mcp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentrelay-mcp",
        description=(
            "AgentRelay MCP server.  Use --agent to set the default sender "
            "identity.  Each tool also accepts from_agent for per-call overrides."
        ),
    )
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
