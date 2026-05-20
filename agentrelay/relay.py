"""AgentRelay v2 core FastAPI/WebSocket relay."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from . import __version__
from .governance import GovernanceEngine
from .models import Agent, GovernanceConfig, Message, MessageType, Room, ThreadState
from .policy import PolicyEngine
from .storage import Storage
from .telegram_transport import TelegramClient, TelegramTransport

TokenHook = Callable[[Message], Awaitable[None]]
logger = logging.getLogger("agentrelay.relay")


def default_db_path() -> str:
    return os.getenv("AGENTRELAY_DB_PATH", str(Path.home() / ".agentrelay" / "data" / "relay.db"))


def ensure_token(token: str | None = None) -> str:
    if token:
        return token
    if env_token := os.getenv("AGENTRELAY_TOKEN"):
        return env_token
    if token_file := os.getenv("AGENTRELAY_TOKEN_FILE"):
        return Path(token_file).expanduser().read_text().strip()
    token_path = Path.home() / ".agentrelay" / "token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if token_path.exists():
        return token_path.read_text().strip()
    generated = secrets.token_urlsafe(32)
    token_path.write_text(generated)
    token_path.chmod(0o600)
    return generated


class Relay:
    def __init__(
        self,
        *,
        token: str | None = None,
        db_path: str | None = None,
        governance: GovernanceConfig | None = None,
        msg_ttl_hours: int | None = None,
    ):
        self.token = ensure_token(token)
        self.storage = Storage(db_path or default_db_path())
        self.governance = GovernanceEngine(governance or GovernanceConfig())
        self.msg_ttl_hours = msg_ttl_hours or int(os.getenv("AGENTRELAY_MSG_TTL_HOURS", "24"))
        self.started_at = time.time()
        self._agents: dict[str, Agent] = {}
        self._ws: dict[str, dict[str, WebSocket]] = defaultdict(dict)
        self._agent_rooms: dict[str, set[str]] = defaultdict(set)
        self._offline: dict[str, deque[dict]] = defaultdict(lambda: deque(maxlen=100))
        self._room_locks: dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._auth_failures: dict[str, list[float]] = defaultdict(list)
        self._tg_hook: TokenHook | None = None
        self._telegram_transport_client: TelegramClient | None = None
        self.counters: dict[str, int] = {
            "messages_received": 0,
            "messages_sent": 0,
            "messages_rejected": 0,
            "connects": 0,
            "disconnects": 0,
            "dm_queued": 0,
        }

    def set_telegram_hook(self, hook: TokenHook | None) -> None:
        self._tg_hook = hook

    def set_telegram_transport_client(self, client: TelegramClient | None) -> None:
        self._telegram_transport_client = client

    async def startup(self) -> None:
        await self.storage.init()
        if state := await self.storage.load_governance():
            self.governance.import_state(state)
        terminal_threads = await self.storage.fetch_terminal_threads()
        if terminal_threads:
            cleaned = self.governance.cleanup_threads(terminal_threads)
            logger.info("cleaned terminal thread governance state", extra={"threads": cleaned})
        logger.info("relay started", extra={"db_path": self.storage.db_path})
        self._tasks = [
            asyncio.create_task(self._persist_governance_loop()),
            asyncio.create_task(self._cleanup_loop()),
        ]
        tg_token = os.getenv("AGENTRELAY_TG_TOKEN")
        tg_chat = os.getenv("AGENTRELAY_TG_CHAT")
        if tg_token and tg_chat:
            from .telegram import TelegramBot

            tg_bot = TelegramBot(self, tg_token, tg_chat)
            self.set_telegram_hook(tg_bot.forward)
            self._tasks.append(asyncio.create_task(tg_bot.run()))
        if os.getenv("AGENTRELAY_TELEGRAM_TRANSPORT", "").lower() in {"1", "true", "yes"}:
            from .telegram_transport import TelegramBotRunner, TelegramHTTPClient

            client = self._telegram_transport_client or TelegramHTTPClient()
            self.set_telegram_transport_client(client)
            self._tasks.append(asyncio.create_task(TelegramBotRunner(self, client).run()))

    async def shutdown(self) -> None:
        await self.storage.store_governance(self.governance.export_state())
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        await self.storage.close()

    async def _persist_governance_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            terminal_threads = await self.storage.fetch_terminal_threads()
            if terminal_threads:
                self.governance.cleanup_threads(terminal_threads)
            await self.storage.store_governance(self.governance.export_state())
            self.governance.cleanup_stale(set(self._agents))

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            removed = await self.storage.cleanup(self.msg_ttl_hours)
            if removed:
                logger.info("cleaned expired messages", extra={"count": removed})

    async def _room_lock(self, room: str) -> asyncio.Lock:
        async with self._locks_lock:
            lock = self._room_locks.get(room)
            if lock is None:
                lock = asyncio.Lock()
                self._room_locks[room] = lock
            return lock

    def _check_auth_rate(self, ip: str) -> bool:
        now = time.time()
        failures = self._auth_failures.get(ip, [])
        failures = [t for t in failures if now - t < 300]
        self._auth_failures[ip] = failures
        if len(failures) >= 10:
            return False
        return True

    def _record_auth_failure(self, ip: str) -> None:
        self._auth_failures.setdefault(ip, []).append(time.time())

    def _clear_auth_failures(self, ip: str) -> None:
        self._auth_failures.pop(ip, None)

    def _auth_ok(self, token: str | None) -> bool:
        return bool(token and secrets.compare_digest(token, self.token))

    async def _websocket_auth(self, ws: WebSocket) -> tuple[str, str] | None:
        client_ip = getattr(getattr(ws, "client", None), "host", None) or "unknown"
        if not self._check_auth_rate(client_ip):
            await ws.close(code=1008, reason="rate limited")
            return None
        try:
            raw = await asyncio.wait_for(ws.receive_json(), timeout=5)
        except Exception:
            self._record_auth_failure(client_ip)
            await ws.close(code=1008, reason="auth required")
            return None
        if raw.get("type") != "auth" or not self._auth_ok(raw.get("token")):
            self._record_auth_failure(client_ip)
            await ws.close(code=1008, reason="invalid token")
            return None
        self._clear_auth_failures(client_ip)
        agent = str(raw.get("agent") or ws.query_params.get("agent") or "agent")
        room = str(raw.get("room") or ws.query_params.get("room") or "general")
        return agent, room

    def rooms(self) -> list[Room]:
        seen = set(self._ws) | {agent.room for agent in self._agents.values()}
        return [Room(id=room, name=room, agents=sorted(self._ws.get(room, {}))) for room in sorted(seen)]

    async def register(self, ws: WebSocket, agent_id: str, room: str) -> None:
        old_rooms = list(self._agent_rooms.get(agent_id, set()))
        for old_room in old_rooms:
            old_ws = self._ws.get(old_room, {}).pop(agent_id, None)
            if old_ws and old_ws is not ws:
                with suppress(Exception):
                    await old_ws.close(code=4000, reason="reconnected")
        self._agents[agent_id] = Agent(id=agent_id, name=agent_id, room=room)
        self._ws[room][agent_id] = ws
        self._agent_rooms[agent_id].add(room)
        self.counters["connects"] += 1
        for payload in list(self._offline.pop(agent_id, deque())):
            await ws.send_text(json.dumps(payload))
        await self._broadcast_system(room, f"{agent_id} joined", MessageType.JOIN, exclude=agent_id)

    async def disconnect(self, agent_id: str, room: str) -> None:
        self._ws.get(room, {}).pop(agent_id, None)
        if not self._ws.get(room):
            self._ws.pop(room, None)
        self._agent_rooms[agent_id].discard(room)
        if not self._agent_rooms[agent_id]:
            self._agent_rooms.pop(agent_id, None)
            self._agents.pop(agent_id, None)
        self.counters["disconnects"] += 1
        await self._broadcast_system(room, f"{agent_id} left", MessageType.LEAVE)

    async def join_room(self, agent_id: str, room: str, ws: WebSocket) -> None:
        self._ws[room][agent_id] = ws
        self._agent_rooms[agent_id].add(room)
        if agent_id in self._agents:
            self._agents[agent_id].room = room
        await self._broadcast_system(room, f"{agent_id} joined", MessageType.JOIN, exclude=agent_id)

    async def leave_room(self, agent_id: str, room: str) -> None:
        self._ws.get(room, {}).pop(agent_id, None)
        self._agent_rooms[agent_id].discard(room)
        await self._broadcast_system(room, f"{agent_id} left", MessageType.LEAVE)

    async def _send(self, agent_id: str, room: str, payload: dict) -> bool:
        ws = self._ws.get(room, {}).get(agent_id)
        if not ws:
            return False
        try:
            await ws.send_text(json.dumps(payload))
            return True
        except Exception:
            logger.exception("failed sending websocket payload", extra={"agent": agent_id, "room": room})
            self._ws.get(room, {}).pop(agent_id, None)
            return False

    async def _broadcast(self, room: str, payload: dict, exclude: str | None = None) -> int:
        peers = list(self._ws.get(room, {}).items())

        async def send_one(agent_id: str, ws: WebSocket) -> tuple[str, bool]:
            if agent_id == exclude:
                return agent_id, True
            try:
                await ws.send_text(json.dumps(payload))
                return agent_id, True
            except Exception:
                logger.exception("failed broadcasting websocket payload", extra={"agent": agent_id, "room": room})
                return agent_id, False

        results = await asyncio.gather(*(send_one(a, ws) for a, ws in peers), return_exceptions=False)
        sent = 0
        for agent_id, ok in results:
            if ok and agent_id != exclude:
                sent += 1
            elif not ok:
                self._ws.get(room, {}).pop(agent_id, None)
        self.counters["messages_sent"] += sent
        return sent

    async def _broadcast_system(
        self, room: str, content: str, msg_type: MessageType = MessageType.SYSTEM, exclude: str | None = None
    ) -> None:
        msg = Message(sender="system", room=room, content=content, type=msg_type)
        await self._broadcast(room, msg.wire(), exclude=exclude)
        if self._tg_hook:
            await self._tg_hook(msg)

    async def _send_dm(self, target: str, msg: Message) -> int:
        for room in self._agent_rooms.get(target, set()):
            if await self._send(target, room, msg.wire()):
                self.counters["messages_sent"] += 1
                return 1
        self._offline[target].append(msg.wire())
        self.counters["dm_queued"] += 1
        logger.info("queued offline dm", extra={"target": target, "sender": msg.sender})
        return 0

    async def _send_transport_dm(self, target: str, msg: Message) -> int:
        if self._telegram_transport_client is None:
            return 0
        if await self.storage.get_telegram_bot(target) is None:
            return 0
        result = await TelegramTransport(self.storage, self._telegram_transport_client).send_private(
            msg.sender,
            target,
            msg.content,
        )
        if result.get("status") == "sent":
            self.counters["messages_sent"] += 1
            return 1
        if result.get("status") == "blocked":
            self.counters["messages_rejected"] += 1
            logger.info(
                "telegram dm blocked by policy",
                extra={"sender": msg.sender, "target": target, "error": result.get("error")},
            )
        return 0

    async def _send_transport_room(self, room: str, msg: Message) -> int:
        if self._telegram_transport_client is None:
            return 0
        if await self.storage.get_telegram_group(room) is None:
            return 0
        result = await TelegramTransport(self.storage, self._telegram_transport_client).send_group(
            msg.sender,
            room,
            msg.content,
        )
        if result.get("status") == "sent":
            self.counters["messages_sent"] += 1
            return 1
        if result.get("status") == "blocked":
            self.counters["messages_rejected"] += 1
            logger.info(
                "telegram group message blocked by policy",
                extra={"sender": msg.sender, "room": room, "error": result.get("error")},
            )
        return 0

    async def _check_policy_routes(
        self, sender: str, room: str, target: str | None, mentions: list[str], thread: str | None
    ) -> dict | None:
        engine = PolicyEngine(self.storage)
        route_targets = [target] if target else mentions or ["*"]
        for route_target in route_targets:
            decision = await engine.evaluate(sender, route_target, room)
            if not decision.allowed:
                self.counters["messages_rejected"] += 1
                return await self._reject(
                    sender,
                    room,
                    decision.reason,
                    thread=thread,
                    rule_id=decision.rule_id,
                )
        return None

    async def process(self, raw: dict, sender: str, room: str) -> dict:
        self.counters["messages_received"] += 1
        content = str(raw.get("content", ""))
        if len(content.encode("utf-8")) > 4096:
            self.counters["messages_rejected"] += 1
            return await self._reject(sender, room, "MESSAGE_TOO_LARGE")

        try:
            msg_type = MessageType(raw.get("type", "dm" if raw.get("target") else "text"))
        except ValueError:
            self.counters["messages_rejected"] += 1
            return await self._reject(sender, room, "INVALID_MESSAGE_TYPE")
        target = raw.get("target")
        thread = raw.get("thread")
        if thread and len(str(thread)) > 128:
            self.counters["messages_rejected"] += 1
            return await self._reject(sender, room, "THREAD_ID_TOO_LONG")

        if msg_type == MessageType.CLOSE:
            if not thread:
                return await self._reject(sender, room, "THREAD_REQUIRED")
            await self.storage.update_thread_state(str(thread), ThreadState.CLOSED, room)
            self.governance.cleanup_thread(room, str(thread))
            await self._broadcast_system(room, f"thread closed: {thread}")
            return {"status": "closed", "thread": thread}

        if thread:
            state = await self.storage.get_thread_state(str(thread), room)
            if state in {ThreadState.CLOSING.value, ThreadState.CLOSED.value}:
                self.counters["messages_rejected"] += 1
                return await self._reject(sender, room, "THREAD_CLOSED", thread=str(thread))

        mentions = raw.get("mentions") or []
        if isinstance(mentions, str):
            mentions = [mentions]
        mentions = [str(mention) for mention in mentions]
        if rejected := await self._check_policy_routes(
            sender,
            room,
            str(target) if target else None,
            mentions,
            str(thread) if thread else None,
        ):
            return rejected
        msg = Message(
            sender=sender,
            room=room,
            content=content,
            thread=str(thread) if thread else None,
            type=MessageType.DM if target else msg_type,
            target=str(target) if target else None,
            mentions=mentions,
        )

        lock = await self._room_lock(room)
        async with lock:
            ok, error, retry_after = self.governance.check(sender, room, msg.thread)
            if not ok:
                self.counters["messages_rejected"] += 1
                return await self._reject(sender, room, error or "GOVERNANCE_BLOCK", retry_after, msg.thread)
            self.governance.record(sender, room, msg.thread)

        skip_transport_delivery = bool(raw.get("skip_transport_delivery")) and (
            sender == "system" or sender.startswith("telegram.")
        )
        await self.storage.store_message(msg)
        if msg.target:
            delivered = await self._send_dm(msg.target, msg)
            if not delivered and not skip_transport_delivery:
                delivered = await self._send_transport_dm(msg.target, msg)
        else:
            delivered = await self._broadcast(room, msg.wire())
            if not skip_transport_delivery:
                delivered += await self._send_transport_room(room, msg)
        ack = {"type": "system", "content": "ack", "ref_id": msg.id, "seq": msg.seq}
        await self._send(sender, room, ack)

        if msg.thread:
            state = await self.storage.get_thread_state(msg.thread, room)
            if state == ThreadState.OPEN.value:
                await self.storage.update_thread_state(msg.thread, ThreadState.ACTIVE, room)
            if self.governance.should_auto_close(room, msg.thread):
                await self.storage.update_thread_state(msg.thread, ThreadState.CLOSING, room)
                self.governance.cleanup_thread(room, msg.thread)
                await self._broadcast_system(room, f"thread auto-closing: {msg.thread}")
            elif self.governance.should_timeout(room, msg.thread):
                await self.storage.update_thread_state(msg.thread, ThreadState.CLOSING, room)
                self.governance.cleanup_thread(room, msg.thread)
                await self._broadcast_system(room, f"thread inactive: {msg.thread}")

        if self._tg_hook:
            await self._tg_hook(msg)
        return {"status": "sent", "seq": msg.seq, "delivered_to": delivered}

    async def _reject(
        self,
        agent: str,
        room: str,
        error: str,
        retry_after: int | None = None,
        thread: str | None = None,
        rule_id: str | None = None,
    ) -> dict:
        payload = {"type": "system", "content": f"governance_block: {error}", "error": error}
        if retry_after is not None:
            payload["retry_after"] = retry_after
        if thread:
            payload["thread"] = thread
        if rule_id:
            payload["rule_id"] = rule_id
        await self._send(agent, room, payload)
        return payload

    async def ws_handler(self, ws: WebSocket) -> None:
        await ws.accept()
        auth = await self._websocket_auth(ws)
        if auth is None:
            return
        agent, room = auth
        await self.register(ws, agent, room)
        try:
            while True:
                raw = await ws.receive_json()
                typ = raw.get("type")
                if typ == "join":
                    new_room = str(raw.get("room") or room)
                    await self.join_room(agent, new_room, ws)
                    room = new_room
                    continue
                if typ == "leave":
                    leave_room = str(raw.get("room") or room)
                    await self.leave_room(agent, leave_room)
                    continue
                await self.process(raw, agent, room)
        except WebSocketDisconnect:
            await self.disconnect(agent, room)
        except Exception:
            logger.exception("websocket handler error", extra={"agent": agent, "room": room})
            await self._send(agent, room, {"type": "system", "content": "error", "error": "INTERNAL_ERROR"})
            await self.disconnect(agent, room)


def create_app(relay: Relay | None = None) -> FastAPI:
    relay = relay or Relay(
        governance=GovernanceConfig(
            cooldown_seconds=int(os.getenv("AGENTRELAY_COOLDOWN", "30")),
            rate_limit=int(os.getenv("AGENTRELAY_RATE_LIMIT", "60")),
            per_thread_cap=int(os.getenv("AGENTRELAY_THREAD_CAP", "15")),
            per_agent_thread_cap=int(os.getenv("AGENTRELAY_AGENT_THREAD_CAP", "3")),
        )
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await relay.startup()
        try:
            yield
        finally:
            await relay.shutdown()

    app = FastAPI(title="AgentRelay", version=__version__, lifespan=lifespan)
    app.state.relay = relay

    def require_http_auth(
        request: Request,
        authorization: str | None = Header(default=None),
        x_agentrelay_token: str | None = Header(default=None, alias="X-AgentRelay-Token"),
    ) -> None:
        client_ip = request.client.host if request.client else "unknown"
        if not relay._check_auth_rate(client_ip):
            raise HTTPException(status_code=429, detail="rate limited")
        bearer = None
        if authorization:
            scheme, _, value = authorization.partition(" ")
            bearer = value if scheme.lower() == "bearer" and value else authorization
        if relay._auth_ok(x_agentrelay_token or bearer):
            relay._clear_auth_failures(client_ip)
            return
        relay._record_auth_failure(client_ip)
        raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/")
    async def health():
        return {"status": "ok"}

    @app.get("/rooms")
    async def rooms(_: None = Depends(require_http_auth)):
        return [room.model_dump() for room in relay.rooms()]

    @app.get("/rooms/{room}/history")
    async def history(
        room: str,
        _: None = Depends(require_http_auth),
        limit: int = Query(50, ge=1, le=500),
        after_seq: int = 0,
    ):
        return [msg.wire() for msg in await relay.storage.fetch_history(room, limit, after_seq)]

    @app.get("/agents")
    async def agents(_: None = Depends(require_http_auth)):
        return [agent.model_dump() | {"rooms": sorted(relay._agent_rooms.get(agent.id, []))} for agent in relay._agents.values()]

    @app.get("/metrics")
    async def metrics(_: None = Depends(require_http_auth)):
        return {
            **relay.counters,
            "uptime_seconds": int(time.time() - relay.started_at),
            "connected_agents": len(relay._agents),
            "active_rooms": len(relay._ws),
            "governance": relay.governance.metrics(),
        }

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await relay.ws_handler(ws)

    @app.exception_handler(ValueError)
    async def value_error_handler(_, exc: ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    return app
