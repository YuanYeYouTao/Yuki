"""Typed event names and immutable notification envelopes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field

from yuki_plugin_sdk.models import JsonValue, StrictModel


class EventName(StrEnum):
    APPLICATION_STARTED = "application.started"
    APPLICATION_STOPPING = "application.stopping"
    MESSAGE_NORMALIZED = "message.normalized"
    MESSAGE_TRIGGERED = "message.triggered"
    TURN_ADMITTED = "turn.admitted"
    TURN_REJECTED = "turn.rejected"
    AUTONOMOUS_DECLINED = "turn.autonomous_declined"
    TURN_CLOSED = "turn.closed"
    REPLY_SENT = "reply.sent"


class EventEnvelope(StrictModel):
    event_id: UUID = Field(default_factory=uuid4)
    name: EventName
    schema_version: int = Field(default=1, ge=1)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: Mapping[str, JsonValue] = Field(default_factory=dict)


NotificationHandler = Callable[[EventEnvelope], Awaitable[None]]


class HookExecution(StrictModel):
    plugin_id: str
    hook_id: str
    success: bool
    duration_seconds: float = Field(ge=0)
    error_category: str | None = None
