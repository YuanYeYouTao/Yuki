"""Isolated, plugin-owned AI-session protocol."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import Field, field_validator

from yuki_plugin_sdk.models import JsonValue, StrictModel


class SessionPersistence(StrEnum):
    EPHEMERAL = "ephemeral"
    DURABLE = "durable"


class SessionContextProfile(StrEnum):
    NONE = "none"
    CURRENT_USER = "current_user"
    CURRENT_GROUP = "current_group"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class CreateAgentSessionRequest(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    instructions: str = Field(min_length=1, max_length=8_000)
    persistence: SessionPersistence = SessionPersistence.DURABLE
    context_profile: SessionContextProfile = SessionContextProfile.NONE
    allowed_capabilities: tuple[str, ...] = Field(default=(), max_length=64)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("allowed_capabilities")
    @classmethod
    def _unique_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("allowed_capabilities cannot contain duplicates")
        return value


class RunAgentSessionRequest(StrictModel):
    session_id: UUID
    user_input: str = Field(min_length=1, max_length=12_000)
    allowed_capabilities: tuple[str, ...] | None = Field(default=None, max_length=64)
    max_tool_calls: int | None = Field(default=None, ge=0, le=64)
    max_model_requests: int | None = Field(default=None, ge=1, le=64)

    @field_validator("allowed_capabilities")
    @classmethod
    def _unique_capabilities(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("allowed_capabilities cannot contain duplicates")
        return value


class AgentSession(StrictModel):
    session_id: UUID
    name: str
    status: SessionStatus
    persistence: SessionPersistence
    context_profile: SessionContextProfile
    created_at: datetime
    updated_at: datetime
    turn_count: int = Field(default=0, ge=0)


class AgentSessionRunResult(StrictModel):
    session: AgentSession
    text: str
    tool_calls_used: int = Field(default=0, ge=0)
    model_requests: int = Field(default=1, ge=1)


class AgentSessionFacade(Protocol):
    """A facade already bound to one plugin and current trusted authority."""

    async def create(self, request: CreateAgentSessionRequest) -> AgentSession: ...

    async def run(self, request: RunAgentSessionRequest) -> AgentSessionRunResult: ...

    async def reset(self, session_id: UUID) -> AgentSession: ...

    async def close(self, session_id: UUID) -> AgentSession: ...
