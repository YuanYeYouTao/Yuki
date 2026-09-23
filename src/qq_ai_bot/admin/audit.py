"""Redacted, append-only audit support for administrator operations."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.models import AdminOperationEvent
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import AdminOperationEventModel

_SECRET_KEYS = (
    "api_key",
    "apikey",
    "access_token",
    "password",
    "secret",
    "credential",
    "authorization",
    "cookie",
)
_CONTENT_KEYS = (
    "content",
    "preference_value",
    "system_prompt",
    "prompt",
    "reasoning",
    "webpage",
    "raw_content",
    "html",
)


def redact_audit_value(value: object, *, key: str = "") -> object:
    """Produce bounded JSON data without credentials or large untrusted bodies."""

    normalized_key = key.casefold()
    if any(token in normalized_key for token in (*_SECRET_KEYS, *_CONTENT_KEYS)):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key)[:128]: redact_audit_value(item_value, key=str(item_key))
            for item_key, item_value in list(value.items())[:50]
        }
    if isinstance(value, list | tuple):
        return [redact_audit_value(item) for item in value[:50]]
    if isinstance(value, str):
        return value if len(value) <= 512 else value[:512] + "[TRUNCATED]"
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:512]


def _json(value: object) -> str:
    return json.dumps(redact_audit_value(value), ensure_ascii=False, default=str)


def _decode(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def event_from_model(row: AdminOperationEventModel) -> AdminOperationEvent:
    """Convert a storage row without exposing ORM internals."""

    return AdminOperationEvent(
        id=row.id,
        actor_user_id=row.actor_user_id,
        trigger_message_id=row.trigger_message_id,
        conversation_key=row.conversation_key,
        capability=row.capability,
        operation=row.operation,
        target_type=row.target_type,
        target_id=row.target_id,
        before=_decode(row.before_json),
        after=_decode(row.after_json),
        success=row.success,
        error_category=row.error_category,
        duration_seconds=row.duration_seconds,
        created_at=row.created_at,
        actor_principal_kind=row.actor_principal_kind,
        actor_principal_id=row.actor_principal_id,
    )


class AuditSubject(Protocol):
    """Minimal actor fields persisted on an administrator audit row."""

    @property
    def user_id(self) -> str: ...

    @property
    def trigger_message_id(self) -> str: ...

    @property
    def conversation_key(self) -> str: ...


async def add_audit_event(
    session: AsyncSession,
    *,
    actor: AuditSubject,
    capability: str,
    operation: str,
    target_type: str,
    target_id: str,
    before: object,
    after: object,
    success: bool,
    error_category: str | None,
    duration_seconds: float,
) -> AdminOperationEventModel:
    """Append an audit row inside the caller's existing transaction."""

    row = AdminOperationEventModel(
        actor_user_id=actor.user_id,
        actor_principal_kind=getattr(actor, "principal_kind", None),
        actor_principal_id=getattr(actor, "principal_id", None),
        trigger_message_id=actor.trigger_message_id[:128],
        conversation_key=actor.conversation_key[:255],
        capability=capability[:64],
        operation=operation[:128],
        target_type=target_type[:64],
        target_id=target_id[:128],
        before_json=_json(before),
        after_json=_json(after),
        success=success,
        error_category=error_category[:64] if error_category else None,
        duration_seconds=max(0.0, duration_seconds),
        created_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


class AdminAuditService:
    """Write and query redacted administrator events."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Open the transaction shared by one business mutation and its audit row."""

        async with self._database.sessions() as session, session.begin():
            yield session

    async def record(
        self,
        *,
        actor: AuditSubject,
        capability: str,
        operation: str,
        target_type: str,
        target_id: str,
        before: object = None,
        after: object = None,
        success: bool,
        error_category: str | None = None,
        duration_seconds: float = 0,
        session: AsyncSession | None = None,
    ) -> AdminOperationEvent:
        if session is not None:
            row = await add_audit_event(
                session,
                actor=actor,
                capability=capability,
                operation=operation,
                target_type=target_type,
                target_id=target_id,
                before=before,
                after=after,
                success=success,
                error_category=error_category,
                duration_seconds=duration_seconds,
            )
            return event_from_model(row)
        async with self.transaction() as owned_session:
            row = await add_audit_event(
                owned_session,
                actor=actor,
                capability=capability,
                operation=operation,
                target_type=target_type,
                target_id=target_id,
                before=before,
                after=after,
                success=success,
                error_category=error_category,
                duration_seconds=duration_seconds,
            )
            return event_from_model(row)

    async def history(
        self,
        *,
        key: str | None = None,
        actor_user_id: str | None = None,
        capability: str | None = None,
        limit: int = 20,
    ) -> tuple[AdminOperationEvent, ...]:
        statement = select(AdminOperationEventModel)
        if key:
            statement = statement.where(AdminOperationEventModel.target_id == key)
        if actor_user_id:
            statement = statement.where(AdminOperationEventModel.actor_user_id == actor_user_id)
        if capability:
            statement = statement.where(AdminOperationEventModel.capability == capability)
        statement = statement.order_by(
            AdminOperationEventModel.created_at.desc(),
            AdminOperationEventModel.id.desc(),
        ).limit(max(1, min(limit, 100)))
        async with self._database.sessions() as session:
            rows = (await session.scalars(statement)).all()
            return tuple(event_from_model(row) for row in rows)

    async def get(
        self, event_id: int, *, session: AsyncSession | None = None
    ) -> AdminOperationEvent | None:
        if session is not None:
            row = await session.get(AdminOperationEventModel, event_id)
            return event_from_model(row) if row is not None else None
        async with self._database.sessions() as owned:
            row = await owned.get(AdminOperationEventModel, event_id)
            return event_from_model(row) if row is not None else None
