"""Persistence for plugin-owned Agent sessions isolated from Yuki chat history."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, or_, select, update
from sqlalchemy.engine import CursorResult

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.db_models import (
    PluginAgentMessageModel,
    PluginAgentSessionModel,
)
from qq_ai_bot.plugin_host.ownership import (
    actor_matches_session,
    apply_inherited_sender,
    inherit_message_sender_person,
    require_live_actor,
    require_session_readable,
    resolve_active_space_id,
    resolve_human_person_id,
    stamp_session_owners,
)

_SESSION_ROLES = frozenset({"user", "assistant", "tool"})
_SESSION_SCOPES = frozenset({"user", "group", "plugin"})
_FORBIDDEN_METADATA_KEYS = (
    "api_key",
    "access_token",
    "authorization",
    "cookie",
    "password",
    "secret",
    "reasoning",
    "system_prompt",
)


class PluginSessionUnavailableError(RuntimeError):
    """The opaque session does not exist, is closed, expired, or belongs elsewhere."""


@dataclass(frozen=True, slots=True)
class PluginAgentSessionRecord:
    session_id: str
    plugin_id: str
    owner_user_id: str | None
    scope_type: str
    scope_id: str
    name: str
    model: str
    instructions: str
    persistence: str
    context_profile: str
    allowed_capabilities: tuple[str, ...]
    status: str
    next_sequence: int
    turn_count: int
    created_at: datetime
    updated_at: datetime
    last_active_at: datetime
    expires_at: datetime | None
    canonical_owner_person_id: str | None = None
    canonical_space_id: str | None = None


@dataclass(frozen=True, slots=True)
class PluginAgentMessageRecord:
    id: int
    session_id: str
    sequence: int
    role: str
    sender_user_id: str | None
    content: str
    metadata: object
    created_at: datetime
    canonical_sender_person_id: str | None = None


class PluginAgentSessionRepository:
    """Create and append isolated transcripts using opaque host-issued IDs."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(
        self,
        *,
        plugin_id: str,
        owner_user_id: str | None,
        scope_type: str,
        scope_id: str,
        name: str = "",
        model: str = "",
        instructions: str = "Continue this isolated plugin session.",
        persistence: str = "durable",
        context_profile: str = "none",
        allowed_capabilities: Iterable[str] = (),
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> PluginAgentSessionRecord:
        scope_id = _validate_scope(scope_type, scope_id, owner_user_id)
        timestamp = _aware_utc(now or datetime.now(UTC))
        expiry = _aware_utc(expires_at) if expires_at else None
        if expiry is not None and expiry <= timestamp:
            raise ValueError("expires_at must be in the future")
        instructions = _validate_instructions(instructions)
        if persistence not in {"ephemeral", "durable"}:
            raise ValueError("persistence must be ephemeral or durable")
        if context_profile not in {"none", "current_user", "current_group"}:
            raise ValueError("unsupported plugin Agent context profile")
        capabilities = _capabilities(allowed_capabilities)
        row = PluginAgentSessionModel(
            session_id=str(uuid.uuid4()),
            plugin_id=plugin_id[:128],
            owner_user_id=owner_user_id,
            scope_type=scope_type,
            scope_id=scope_id,
            name=name[:128],
            model=model[:128],
            instructions=instructions,
            persistence=persistence,
            context_profile=context_profile,
            allowed_capabilities_json=json.dumps(
                capabilities, ensure_ascii=False, separators=(",", ":")
            ),
            status="active",
            next_sequence=1,
            turn_count=0,
            created_at=timestamp,
            updated_at=timestamp,
            last_active_at=timestamp,
            expires_at=expiry,
        )
        async with self._database.sessions() as session, session.begin():
            session.add(row)
            await session.flush()
            await stamp_session_owners(session, row)
            return _session_record(row)

    async def get(
        self,
        *,
        plugin_id: str,
        session_id: str,
        include_expired: bool = False,
        now: datetime | None = None,
    ) -> PluginAgentSessionRecord | None:
        statement = select(PluginAgentSessionModel).where(
            PluginAgentSessionModel.plugin_id == plugin_id,
            PluginAgentSessionModel.session_id == session_id,
        )
        if not include_expired:
            timestamp = _aware_utc(now or datetime.now(UTC))
            statement = statement.where(
                or_(
                    PluginAgentSessionModel.expires_at.is_(None),
                    PluginAgentSessionModel.expires_at > timestamp,
                )
            )
        async with self._database.sessions() as session:
            row = await session.scalar(statement)
            if row is None:
                return None
            await require_session_readable(session, row)
            return _session_record(row)

    async def get_for_actor(
        self,
        *,
        plugin_id: str,
        session_id: str,
        actor_user_id: str,
        current_group_id: str | None,
        include_expired: bool = False,
        now: datetime | None = None,
    ) -> PluginAgentSessionRecord | None:
        """Load one session and authorize the Host actor.

        Unbound or different actors get not-found. Only a matching actor
        sees PluginOwnershipError from the persisted row itself.
        """

        statement = select(PluginAgentSessionModel).where(
            PluginAgentSessionModel.plugin_id == plugin_id,
            PluginAgentSessionModel.session_id == session_id,
        )
        if not include_expired:
            timestamp = _aware_utc(now or datetime.now(UTC))
            statement = statement.where(
                or_(
                    PluginAgentSessionModel.expires_at.is_(None),
                    PluginAgentSessionModel.expires_at > timestamp,
                )
            )
        async with self._database.sessions() as session:
            row = await session.scalar(statement)
            if row is None:
                return None
            if not await actor_matches_session(
                session,
                row,
                actor_user_id=actor_user_id,
                current_group_id=current_group_id,
            ):
                return None
            await require_session_readable(session, row)
            await require_live_actor(
                session,
                row,
                actor_user_id=actor_user_id,
                current_group_id=current_group_id,
            )
            return _session_record(row)

    async def list_scope(
        self,
        *,
        plugin_id: str,
        scope_type: str,
        scope_id: str,
        limit: int = 100,
        include_closed: bool = False,
        now: datetime | None = None,
    ) -> tuple[PluginAgentSessionRecord, ...]:
        if scope_type not in _SESSION_SCOPES:
            raise ValueError("unsupported plugin Agent session scope")
        timestamp = _aware_utc(now or datetime.now(UTC))
        async with self._database.sessions() as session:
            statement = select(PluginAgentSessionModel).where(
                PluginAgentSessionModel.plugin_id == plugin_id,
                PluginAgentSessionModel.scope_type == scope_type,
                or_(
                    PluginAgentSessionModel.expires_at.is_(None),
                    PluginAgentSessionModel.expires_at > timestamp,
                ),
            )
            if scope_type == "user":
                person_id = await resolve_human_person_id(
                    session,
                    scope_id,
                    missing_message="session owner has no Person",
                )
                statement = statement.where(
                    PluginAgentSessionModel.canonical_owner_person_id == person_id
                )
            elif scope_type == "group":
                space_id = await resolve_active_space_id(session, scope_id)
                statement = statement.where(PluginAgentSessionModel.canonical_space_id == space_id)
            else:
                statement = statement.where(PluginAgentSessionModel.scope_id == scope_id)
            if not include_closed:
                statement = statement.where(PluginAgentSessionModel.status == "active")
            statement = statement.order_by(
                PluginAgentSessionModel.last_active_at.desc(),
                PluginAgentSessionModel.session_id,
            ).limit(max(1, min(limit, 1_000)))
            rows = (await session.scalars(statement)).all()
            for row in rows:
                await require_session_readable(session, row)
            return tuple(_session_record(row) for row in rows)

    async def append_message(
        self,
        *,
        plugin_id: str,
        session_id: str,
        role: str,
        content: str,
        sender_user_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> PluginAgentMessageRecord:
        """Atomically allocate a transcript sequence and append one visible message."""

        if role not in _SESSION_ROLES:
            raise ValueError("role must be user, assistant, or tool")
        if role == "user" and not sender_user_id:
            raise ValueError("user messages require sender_user_id")
        if role != "user" and sender_user_id is not None:
            raise ValueError("only user messages may carry sender_user_id")
        timestamp = _aware_utc(now or datetime.now(UTC))
        metadata_json = json.dumps(
            _safe_metadata(metadata or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        async with self._database.sessions() as session, session.begin():
            values: dict[str, object] = {
                "next_sequence": PluginAgentSessionModel.next_sequence + 1,
                "updated_at": timestamp,
                "last_active_at": timestamp,
            }
            if role == "assistant":
                values["turn_count"] = PluginAgentSessionModel.turn_count + 1
            result = await session.execute(
                update(PluginAgentSessionModel)
                .where(
                    PluginAgentSessionModel.plugin_id == plugin_id,
                    PluginAgentSessionModel.session_id == session_id,
                    PluginAgentSessionModel.status == "active",
                    or_(
                        PluginAgentSessionModel.expires_at.is_(None),
                        PluginAgentSessionModel.expires_at > timestamp,
                    ),
                )
                .values(**values)
                .returning(PluginAgentSessionModel.next_sequence)
            )
            next_sequence = result.scalar_one_or_none()
            if next_sequence is None:
                raise PluginSessionUnavailableError("plugin Agent session is unavailable")
            parent = await session.get(PluginAgentSessionModel, session_id)
            if parent is None or parent.plugin_id != plugin_id:
                raise PluginSessionUnavailableError("plugin Agent session is unavailable")
            sender_person = await inherit_message_sender_person(
                session,
                parent,
                role=role,
                sender_user_id=sender_user_id,
            )
            row = PluginAgentMessageModel(
                session_id=session_id,
                sequence=int(next_sequence) - 1,
                role=role,
                sender_user_id=sender_user_id,
                content=content,
                metadata_json=metadata_json,
                created_at=timestamp,
            )
            apply_inherited_sender(row, sender_person)
            session.add(row)
            await session.flush()
            return _message_record(row)

    async def list_messages(
        self,
        *,
        plugin_id: str,
        session_id: str,
        limit: int = 100,
    ) -> tuple[PluginAgentMessageRecord, ...]:
        bounded = max(1, min(limit, 10_000))
        async with self._database.sessions() as session:
            parent = await session.scalar(
                select(PluginAgentSessionModel).where(
                    PluginAgentSessionModel.plugin_id == plugin_id,
                    PluginAgentSessionModel.session_id == session_id,
                )
            )
            if parent is None:
                return ()
            await require_session_readable(session, parent)
            rows = list(
                (
                    await session.scalars(
                        select(PluginAgentMessageModel)
                        .where(PluginAgentMessageModel.session_id == session_id)
                        .order_by(PluginAgentMessageModel.sequence.desc())
                        .limit(bounded)
                    )
                ).all()
            )
        rows.reverse()
        return tuple(_message_record(row) for row in rows)

    async def close(
        self,
        *,
        plugin_id: str,
        session_id: str,
        status: str = "closed",
        now: datetime | None = None,
    ) -> bool:
        if status not in {"closed", "expired", "blocked"}:
            raise ValueError("invalid terminal plugin Agent session status")
        timestamp = _aware_utc(now or datetime.now(UTC))
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(PluginAgentSessionModel)
                .where(
                    PluginAgentSessionModel.plugin_id == plugin_id,
                    PluginAgentSessionModel.session_id == session_id,
                )
                .values(status=status, updated_at=timestamp)
            )
            return bool(cast(CursorResult[Any], result).rowcount)

    async def reset(
        self,
        *,
        plugin_id: str,
        session_id: str,
        now: datetime | None = None,
    ) -> PluginAgentSessionRecord | None:
        """Atomically clear only this plugin session's isolated transcript."""

        timestamp = _aware_utc(now or datetime.now(UTC))
        async with self._database.sessions() as session, session.begin():
            row = await session.scalar(
                select(PluginAgentSessionModel).where(
                    PluginAgentSessionModel.plugin_id == plugin_id,
                    PluginAgentSessionModel.session_id == session_id,
                    PluginAgentSessionModel.status == "active",
                    or_(
                        PluginAgentSessionModel.expires_at.is_(None),
                        PluginAgentSessionModel.expires_at > timestamp,
                    ),
                )
            )
            if row is None:
                return None
            await require_session_readable(session, row)
            await session.execute(
                delete(PluginAgentMessageModel).where(
                    PluginAgentMessageModel.session_id == session_id
                )
            )
            row.next_sequence = 1
            row.turn_count = 0
            row.updated_at = timestamp
            row.last_active_at = timestamp
            await session.flush()
            return _session_record(row)

    async def delete(self, *, plugin_id: str, session_id: str) -> bool:
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(PluginAgentSessionModel).where(
                    PluginAgentSessionModel.plugin_id == plugin_id,
                    PluginAgentSessionModel.session_id == session_id,
                )
            )
            return bool(cast(CursorResult[Any], result).rowcount)

    async def expire_due(self, *, now: datetime | None = None) -> int:
        timestamp = _aware_utc(now or datetime.now(UTC))
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(PluginAgentSessionModel)
                .where(
                    PluginAgentSessionModel.status == "active",
                    PluginAgentSessionModel.expires_at.is_not(None),
                    PluginAgentSessionModel.expires_at <= timestamp,
                )
                .values(status="expired", updated_at=timestamp)
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)

    async def delete_ephemeral(self) -> int:
        """Remove non-durable sessions during a host lifecycle cleanup."""

        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(PluginAgentSessionModel).where(
                    PluginAgentSessionModel.persistence == "ephemeral"
                )
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)


def _session_record(row: PluginAgentSessionModel) -> PluginAgentSessionRecord:
    return PluginAgentSessionRecord(
        session_id=row.session_id,
        plugin_id=row.plugin_id,
        owner_user_id=row.owner_user_id,
        scope_type=row.scope_type,
        scope_id=row.scope_id,
        name=row.name,
        model=row.model,
        instructions=row.instructions,
        persistence=row.persistence,
        context_profile=row.context_profile,
        allowed_capabilities=_decode_capabilities(row.allowed_capabilities_json),
        status=row.status,
        next_sequence=row.next_sequence,
        turn_count=row.turn_count,
        created_at=_aware_utc(row.created_at),
        updated_at=_aware_utc(row.updated_at),
        last_active_at=_aware_utc(row.last_active_at),
        expires_at=_aware_utc(row.expires_at) if row.expires_at else None,
        canonical_owner_person_id=row.canonical_owner_person_id,
        canonical_space_id=row.canonical_space_id,
    )


def _message_record(row: PluginAgentMessageModel) -> PluginAgentMessageRecord:
    try:
        metadata: object = json.loads(row.metadata_json)
    except json.JSONDecodeError:
        metadata = None
    return PluginAgentMessageRecord(
        id=row.id,
        session_id=row.session_id,
        sequence=row.sequence,
        role=row.role,
        sender_user_id=row.sender_user_id,
        content=row.content,
        metadata=metadata,
        created_at=_aware_utc(row.created_at),
        canonical_sender_person_id=row.canonical_sender_person_id,
    )


def _validate_scope(scope_type: str, scope_id: str, owner_user_id: str | None) -> str:
    if scope_type not in _SESSION_SCOPES:
        raise ValueError("unsupported plugin Agent session scope")
    normalized = scope_id.strip()
    if scope_type == "plugin":
        if normalized:
            raise ValueError("plugin-scoped sessions use an empty scope_id")
    elif not normalized:
        raise ValueError("user/group sessions require a scope_id")
    if scope_type == "user" and (owner_user_id is None or normalized != owner_user_id):
        raise ValueError("user-scoped sessions must match their owner")
    return normalized


def _validate_instructions(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 8_000:
        raise ValueError("instructions must contain 1 to 8000 characters")
    return normalized


def _capabilities(values: Iterable[str]) -> tuple[str, ...]:
    normalized = {str(value).strip() for value in values if str(value).strip()}
    if any(len(value) > 128 for value in normalized) or len(normalized) > 64:
        raise ValueError("allowed capabilities exceed the session schema limits")
    return tuple(sorted(normalized))


def _decode_capabilities(value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return ()
    if not isinstance(decoded, list):
        return ()
    return _capabilities(str(item) for item in decoded)


def _safe_metadata(value: object, *, key: str = "") -> object:
    if any(token in key.casefold() for token in _FORBIDDEN_METADATA_KEYS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key)[:128]: _safe_metadata(item_value, key=str(item_key))
            for item_key, item_value in list(value.items())[:50]
        }
    if isinstance(value, list | tuple):
        return [_safe_metadata(item) for item in value[:50]]
    if isinstance(value, str):
        return value if len(value) <= 2_000 else value[:2_000] + "[TRUNCATED]"
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:2_000]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
