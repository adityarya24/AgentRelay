"""Pydantic models for AgentRelay Telegram plugin."""
from __future__ import annotations

from pydantic import BaseModel, Field

from agentrelay.models import utc_timestamp


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
