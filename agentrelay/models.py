"""Pydantic models shared by AgentRelay v2 modules."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_timestamp() -> float:
    """Return the current UTC timestamp as POSIX seconds."""
    return datetime.now(timezone.utc).timestamp()


def make_id() -> str:
    """Generate a compact time-sortable message/thread ID."""
    return f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:12]}"


class MessageType(str, Enum):
    TEXT = "text"
    SYSTEM = "system"
    JOIN = "join"
    LEAVE = "leave"
    DM = "dm"
    CLOSE = "close"


class ThreadState(str, Enum):
    OPEN = "open"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"


class IdentityKind(str, Enum):
    LOCAL = "local"
    VPS = "vps"
    EXTERNAL = "external"
    SYSTEM = "system"


class IdentityTransport(str, Enum):
    MCP = "mcp"
    WEBSOCKET = "websocket"
    TELEGRAM = "telegram"
    CLI = "cli"
    HTTP = "http"


class TrustLevel(str, Enum):
    TRUSTED = "trusted"
    SANDBOX = "sandbox"
    BLOCKED = "blocked"


class PolicyEffect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class Message(BaseModel):
    id: str = Field(default_factory=make_id)
    sender: str
    room: str
    content: str = ""
    thread: str | None = None
    type: MessageType = MessageType.TEXT
    target: str | None = None
    mentions: list[str] = Field(default_factory=list)
    ts: float = Field(default_factory=utc_timestamp)
    seq: int | None = None

    def wire(self) -> dict:
        data = self.model_dump(exclude_none=True)
        data["type"] = self.type.value if isinstance(self.type, MessageType) else self.type
        return data


class Agent(BaseModel):
    id: str
    name: str
    room: str
    connected_at: float = Field(default_factory=utc_timestamp)


class Room(BaseModel):
    id: str
    name: str
    agents: list[str] = Field(default_factory=list)


class Identity(BaseModel):
    id: str
    display_name: str
    kind: IdentityKind = IdentityKind.LOCAL
    transport: IdentityTransport = IdentityTransport.MCP
    capabilities: list[str] = Field(default_factory=list)
    enabled: bool = True
    trust_level: TrustLevel = TrustLevel.TRUSTED
    metadata: dict[str, str] = Field(default_factory=dict)
    secret_ref: str | None = None
    created_at: float = Field(default_factory=utc_timestamp)
    updated_at: float = Field(default_factory=utc_timestamp)


class PolicyRule(BaseModel):
    id: str = Field(default_factory=make_id)
    source: str
    target: str
    room: str = "*"
    effect: PolicyEffect
    reason: str | None = None
    enabled: bool = True
    created_at: float = Field(default_factory=utc_timestamp)
    updated_at: float = Field(default_factory=utc_timestamp)


class PolicyDecision(BaseModel):
    allowed: bool
    reason: str
    rule_id: str | None = None


class TelegramBotRegistration(BaseModel):
    identity_id: str
    username: str
    token_ref: str
    bot_to_bot_enabled: bool = False
    enabled: bool = True
    verified_at: float | None = None
    last_error: str | None = None
    created_at: float = Field(default_factory=utc_timestamp)
    updated_at: float = Field(default_factory=utc_timestamp)


class TelegramGroupRegistration(BaseModel):
    room: str
    chat_id: str
    title: str | None = None
    enabled: bool = True
    created_at: float = Field(default_factory=utc_timestamp)
    updated_at: float = Field(default_factory=utc_timestamp)


class GovernanceConfig(BaseModel):
    per_agent_thread_cap: int = 3
    per_thread_cap: int = 15
    cooldown_seconds: int = 30
    inactivity_timeout: int = 300
    rate_limit: int = 60
