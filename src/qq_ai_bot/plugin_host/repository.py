"""Async repositories for Plugin API installation, config, state, and audit data."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import LargeBinary, delete, func, or_, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.unit_of_work import optional_session
from qq_ai_bot.plugin_host.db_models import (
    PluginAuditEventModel,
    PluginConfigValueModel,
    PluginInstallationModel,
    PluginStateModel,
)

_VALID_SCOPES = frozenset({"global", "group", "user"})
_AUDIT_SECRET_KEYS = (
    "api_key",
    "apikey",
    "access_token",
    "password",
    "secret",
    "credential",
    "authorization",
    "cookie",
    "prompt",
    "reasoning",
    "content",
)


class PluginVersionConflictError(RuntimeError):
    """The caller's expected KV/config version is no longer current."""


class PluginApprovalError(RuntimeError):
    """A plugin cannot enter the requested approval or enabled state."""


@dataclass(frozen=True, slots=True)
class PluginInstallationRecord:
    plugin_id: str
    name: str
    version: str
    plugin_api: str
    yuki_requires: str
    manifest_hash: str
    entrypoint: str
    status: str
    enabled: bool
    approved_permissions: tuple[str, ...]
    requested_permissions: tuple[str, ...]
    failure_count: int
    last_error_category: str | None
    discovered_at: datetime
    approved_at: datetime | None
    started_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PluginConfigValueRecord:
    id: int
    plugin_id: str
    scope_type: str
    scope_id: str
    key: str
    value: object
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PluginStateRecord:
    id: int
    plugin_id: str
    namespace: str
    key: str
    value: object
    version: int
    subject_user_id: str | None
    expires_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PluginAuditEventRecord:
    id: int
    plugin_id: str
    actor_user_id: str | None
    operation: str
    permission: str | None
    success: bool
    error_category: str | None
    detail: object
    created_at: datetime


class PluginInstallationRepository:
    """Persist discovery and approval state without importing plugin code."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def upsert_discovered(
        self,
        *,
        plugin_id: str,
        name: str,
        version: str,
        plugin_api: str,
        yuki_requires: str,
        manifest_hash: str,
        entrypoint: str,
        requested_permissions: Iterable[str],
        now: datetime | None = None,
    ) -> PluginInstallationRecord:
        """Record a manifest and revoke stale approval when its identity changes."""

        timestamp = _aware_utc(now or datetime.now(UTC))
        requested = _permissions(requested_permissions)
        requested_json = _json(list(requested))
        async with self._database.sessions() as session, session.begin():
            row = await session.get(PluginInstallationModel, plugin_id)
            if row is None:
                row = PluginInstallationModel(
                    plugin_id=plugin_id[:128],
                    name=name[:128],
                    version=version[:64],
                    plugin_api=plugin_api[:32],
                    yuki_requires=yuki_requires[:128],
                    manifest_hash=manifest_hash[:64],
                    entrypoint=entrypoint[:255],
                    status="pending_approval",
                    enabled=False,
                    approved_permissions_json="[]",
                    requested_permissions_json=requested_json,
                    failure_count=0,
                    last_error_category=None,
                    discovered_at=timestamp,
                    approved_at=None,
                    started_at=None,
                    updated_at=timestamp,
                )
                session.add(row)
            else:
                approval_changed = (
                    row.manifest_hash != manifest_hash
                    or row.requested_permissions_json != requested_json
                    or row.plugin_api != plugin_api
                    or row.yuki_requires != yuki_requires
                )
                row.name = name[:128]
                row.version = version[:64]
                row.plugin_api = plugin_api[:32]
                row.yuki_requires = yuki_requires[:128]
                row.manifest_hash = manifest_hash[:64]
                row.entrypoint = entrypoint[:255]
                row.requested_permissions_json = requested_json
                row.updated_at = timestamp
                if approval_changed:
                    row.status = "pending_approval"
                    row.enabled = False
                    row.approved_permissions_json = "[]"
                    row.approved_at = None
                    row.started_at = None
            await session.flush()
            return _installation_record(row)

    async def get(
        self, plugin_id: str, *, session: AsyncSession | None = None
    ) -> PluginInstallationRecord | None:
        async with optional_session(self._database, session, write=False) as active:
            row = await active.get(PluginInstallationModel, plugin_id)
            return _installation_record(row) if row is not None else None

    async def list_all(self) -> tuple[PluginInstallationRecord, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(PluginInstallationModel).order_by(PluginInstallationModel.plugin_id)
                )
            ).all()
            return tuple(_installation_record(row) for row in rows)

    async def approve(
        self,
        plugin_id: str,
        *,
        permissions: Iterable[str] | None = None,
        now: datetime | None = None,
        session: AsyncSession | None = None,
    ) -> PluginInstallationRecord | None:
        timestamp = _aware_utc(now or datetime.now(UTC))
        async with optional_session(self._database, session, write=True) as active:
            row = await active.get(PluginInstallationModel, plugin_id)
            if row is None:
                return None
            requested = set(_decode_permissions(row.requested_permissions_json))
            approved = set(_permissions(permissions)) if permissions is not None else requested
            if not approved <= requested:
                raise PluginApprovalError("approved permissions must be requested by the plugin")
            row.approved_permissions_json = _json(sorted(approved))
            row.approved_at = timestamp
            row.status = "approved"
            row.updated_at = timestamp
            await active.flush()
            return _installation_record(row)

    async def set_enabled(
        self,
        plugin_id: str,
        *,
        enabled: bool,
        now: datetime | None = None,
        session: AsyncSession | None = None,
    ) -> PluginInstallationRecord | None:
        timestamp = _aware_utc(now or datetime.now(UTC))
        async with optional_session(self._database, session, write=True) as active:
            row = await active.get(PluginInstallationModel, plugin_id)
            if row is None:
                return None
            if enabled and row.approved_at is None:
                raise PluginApprovalError("plugin permissions have not been approved")
            row.enabled = enabled
            row.status = "approved" if enabled else "disabled"
            row.updated_at = timestamp
            await active.flush()
            return _installation_record(row)

    async def set_status(
        self,
        plugin_id: str,
        *,
        status: str,
        error_category: str | None = None,
        now: datetime | None = None,
    ) -> PluginInstallationRecord | None:
        timestamp = _aware_utc(now or datetime.now(UTC))
        async with self._database.sessions() as session, session.begin():
            row = await session.get(PluginInstallationModel, plugin_id)
            if row is None:
                return None
            row.status = status[:32]
            row.last_error_category = _optional(error_category, 64)
            row.updated_at = timestamp
            if status == "running":
                row.started_at = timestamp
                row.failure_count = 0
            await session.flush()
            return _installation_record(row)

    async def record_failure(
        self,
        plugin_id: str,
        *,
        error_category: str,
        disable_threshold: int,
        now: datetime | None = None,
    ) -> PluginInstallationRecord | None:
        timestamp = _aware_utc(now or datetime.now(UTC))
        async with self._database.sessions() as session, session.begin():
            row = await session.get(PluginInstallationModel, plugin_id)
            if row is None:
                return None
            row.failure_count += 1
            row.last_error_category = error_category[:64]
            row.status = "failed"
            if row.failure_count >= max(1, disable_threshold):
                row.enabled = False
            row.updated_at = timestamp
            await session.flush()
            return _installation_record(row)


class PluginConfigRepository:
    """Store non-secret scoped configuration with optimistic CAS semantics."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(
        self, *, plugin_id: str, scope_type: str, scope_id: str, key: str
    ) -> PluginConfigValueRecord | None:
        scope_id = _validated_scope(scope_type, scope_id)
        key = _storage_key(key, label="config key")
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(PluginConfigValueModel).where(
                    PluginConfigValueModel.plugin_id == plugin_id,
                    PluginConfigValueModel.scope_type == scope_type,
                    PluginConfigValueModel.scope_id == scope_id,
                    PluginConfigValueModel.key == key,
                )
            )
            return _config_record(row) if row is not None else None

    async def list_scope(
        self, *, plugin_id: str, scope_type: str, scope_id: str
    ) -> tuple[PluginConfigValueRecord, ...]:
        scope_id = _validated_scope(scope_type, scope_id)
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(PluginConfigValueModel)
                    .where(
                        PluginConfigValueModel.plugin_id == plugin_id,
                        PluginConfigValueModel.scope_type == scope_type,
                        PluginConfigValueModel.scope_id == scope_id,
                    )
                    .order_by(PluginConfigValueModel.key)
                )
            ).all()
            return tuple(_config_record(row) for row in rows)

    async def compare_and_set(
        self,
        *,
        plugin_id: str,
        scope_type: str,
        scope_id: str,
        key: str,
        expected_version: int,
        value: object,
        now: datetime | None = None,
    ) -> PluginConfigValueRecord:
        """Create at version 0 or replace exactly the expected existing version."""

        if expected_version < 0:
            raise ValueError("expected_version must be non-negative")
        scope_id = _validated_scope(scope_type, scope_id)
        key = _storage_key(key, label="config key")
        timestamp = _aware_utc(now or datetime.now(UTC))
        value_json = _json(value)
        async with self._database.sessions() as session, session.begin():
            if expected_version == 0:
                result = await session.execute(
                    insert(PluginConfigValueModel)
                    .values(
                        plugin_id=plugin_id,
                        scope_type=scope_type,
                        scope_id=scope_id,
                        key=key[:128],
                        value_json=value_json,
                        version=1,
                        updated_at=timestamp,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["plugin_id", "scope_type", "scope_id", "key"]
                    )
                )
                if not cast(CursorResult[Any], result).rowcount:
                    raise PluginVersionConflictError("plugin config version changed")
            else:
                result = await session.execute(
                    update(PluginConfigValueModel)
                    .where(
                        PluginConfigValueModel.plugin_id == plugin_id,
                        PluginConfigValueModel.scope_type == scope_type,
                        PluginConfigValueModel.scope_id == scope_id,
                        PluginConfigValueModel.key == key,
                        PluginConfigValueModel.version == expected_version,
                    )
                    .values(
                        value_json=value_json,
                        version=expected_version + 1,
                        updated_at=timestamp,
                    )
                )
                if not cast(CursorResult[Any], result).rowcount:
                    raise PluginVersionConflictError("plugin config version changed")
            row = await session.scalar(
                select(PluginConfigValueModel).where(
                    PluginConfigValueModel.plugin_id == plugin_id,
                    PluginConfigValueModel.scope_type == scope_type,
                    PluginConfigValueModel.scope_id == scope_id,
                    PluginConfigValueModel.key == key,
                )
            )
            assert row is not None
            return _config_record(row)

    async def delete(
        self,
        *,
        plugin_id: str,
        scope_type: str,
        scope_id: str,
        key: str,
        expected_version: int | None = None,
    ) -> bool:
        scope_id = _validated_scope(scope_type, scope_id)
        key = _storage_key(key, label="config key")
        statement = delete(PluginConfigValueModel).where(
            PluginConfigValueModel.plugin_id == plugin_id,
            PluginConfigValueModel.scope_type == scope_type,
            PluginConfigValueModel.scope_id == scope_id,
            PluginConfigValueModel.key == key,
        )
        if expected_version is not None:
            statement = statement.where(PluginConfigValueModel.version == expected_version)
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(statement)
            deleted = bool(cast(CursorResult[Any], result).rowcount)
        if expected_version is not None and not deleted:
            raise PluginVersionConflictError("plugin config version changed")
        return deleted


class PluginStateRepository:
    """Provide plugin-namespaced KV storage with TTL and optimistic CAS."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(
        self,
        *,
        plugin_id: str,
        namespace: str,
        key: str,
        now: datetime | None = None,
    ) -> PluginStateRecord | None:
        namespace = _storage_key(namespace, label="state namespace")
        key = _storage_key(key, label="state key")
        timestamp = _aware_utc(now or datetime.now(UTC))
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(PluginStateModel).where(
                    PluginStateModel.plugin_id == plugin_id,
                    PluginStateModel.namespace == namespace,
                    PluginStateModel.key == key,
                    or_(
                        PluginStateModel.expires_at.is_(None),
                        PluginStateModel.expires_at > timestamp,
                    ),
                )
            )
            return _state_record(row) if row is not None else None

    async def list_namespace(
        self,
        *,
        plugin_id: str,
        namespace: str,
        limit: int = 100,
        now: datetime | None = None,
    ) -> tuple[PluginStateRecord, ...]:
        namespace = _storage_key(namespace, label="state namespace")
        timestamp = _aware_utc(now or datetime.now(UTC))
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(PluginStateModel)
                    .where(
                        PluginStateModel.plugin_id == plugin_id,
                        PluginStateModel.namespace == namespace,
                        or_(
                            PluginStateModel.expires_at.is_(None),
                            PluginStateModel.expires_at > timestamp,
                        ),
                    )
                    .order_by(PluginStateModel.key)
                    .limit(max(1, min(limit, 1_000)))
                )
            ).all()
            return tuple(_state_record(row) for row in rows)

    async def storage_usage_bytes(
        self,
        *,
        plugin_id: str,
        now: datetime | None = None,
    ) -> int:
        """Return the UTF-8 JSON payload size for one plugin's live state."""

        timestamp = _aware_utc(now or datetime.now(UTC))
        async with self._database.sessions() as session:
            usage = await session.scalar(
                select(
                    func.coalesce(
                        func.sum(func.length(sql_cast(PluginStateModel.value_json, LargeBinary))),
                        0,
                    )
                ).where(
                    PluginStateModel.plugin_id == plugin_id,
                    or_(
                        PluginStateModel.expires_at.is_(None),
                        PluginStateModel.expires_at > timestamp,
                    ),
                )
            )
        return int(usage or 0)

    async def compare_and_set(
        self,
        *,
        plugin_id: str,
        namespace: str,
        key: str,
        expected_version: int,
        value: object,
        subject_user_id: str | None = None,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> PluginStateRecord:
        if expected_version < 0:
            raise ValueError("expected_version must be non-negative")
        namespace = _storage_key(namespace, label="state namespace")
        key = _storage_key(key, label="state key")
        timestamp = _aware_utc(now or datetime.now(UTC))
        expiry = _aware_utc(expires_at) if expires_at is not None else None
        value_json = _json(value)
        async with self._database.sessions() as session, session.begin():
            if expected_version == 0:
                await session.execute(
                    delete(PluginStateModel).where(
                        PluginStateModel.plugin_id == plugin_id,
                        PluginStateModel.namespace == namespace,
                        PluginStateModel.key == key,
                        PluginStateModel.expires_at.is_not(None),
                        PluginStateModel.expires_at <= timestamp,
                    )
                )
                result = await session.execute(
                    insert(PluginStateModel)
                    .values(
                        plugin_id=plugin_id,
                        namespace=namespace,
                        key=key,
                        value_json=value_json,
                        version=1,
                        subject_user_id=subject_user_id,
                        expires_at=expiry,
                        updated_at=timestamp,
                    )
                    .on_conflict_do_nothing(index_elements=["plugin_id", "namespace", "key"])
                )
                if not cast(CursorResult[Any], result).rowcount:
                    raise PluginVersionConflictError("plugin state version changed")
            else:
                result = await session.execute(
                    update(PluginStateModel)
                    .where(
                        PluginStateModel.plugin_id == plugin_id,
                        PluginStateModel.namespace == namespace,
                        PluginStateModel.key == key,
                        PluginStateModel.version == expected_version,
                    )
                    .values(
                        value_json=value_json,
                        version=expected_version + 1,
                        subject_user_id=subject_user_id,
                        expires_at=expiry,
                        updated_at=timestamp,
                    )
                )
                if not cast(CursorResult[Any], result).rowcount:
                    raise PluginVersionConflictError("plugin state version changed")
            row = await session.scalar(
                select(PluginStateModel).where(
                    PluginStateModel.plugin_id == plugin_id,
                    PluginStateModel.namespace == namespace,
                    PluginStateModel.key == key,
                )
            )
            assert row is not None
            return _state_record(row)

    async def delete(
        self,
        *,
        plugin_id: str,
        namespace: str,
        key: str,
        expected_version: int | None = None,
    ) -> bool:
        namespace = _storage_key(namespace, label="state namespace")
        key = _storage_key(key, label="state key")
        statement = delete(PluginStateModel).where(
            PluginStateModel.plugin_id == plugin_id,
            PluginStateModel.namespace == namespace,
            PluginStateModel.key == key,
        )
        if expected_version is not None:
            statement = statement.where(PluginStateModel.version == expected_version)
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(statement)
            deleted = bool(cast(CursorResult[Any], result).rowcount)
        if expected_version is not None and not deleted:
            raise PluginVersionConflictError("plugin state version changed")
        return deleted

    async def cleanup_expired(self, *, now: datetime | None = None) -> int:
        timestamp = _aware_utc(now or datetime.now(UTC))
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(PluginStateModel).where(
                    PluginStateModel.expires_at.is_not(None),
                    PluginStateModel.expires_at <= timestamp,
                )
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)


class PluginAuditRepository:
    """Append and inspect bounded audit metadata; secret-like fields are redacted."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def record(
        self,
        *,
        plugin_id: str,
        actor_user_id: str | None,
        operation: str,
        permission: str | None,
        success: bool,
        error_category: str | None = None,
        detail: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> PluginAuditEventRecord:
        row = PluginAuditEventModel(
            plugin_id=plugin_id[:128],
            actor_user_id=_optional(actor_user_id, 64),
            operation=operation[:128],
            permission=_optional(permission, 128),
            success=success,
            error_category=_optional(error_category, 64),
            detail_json=_json(_redact_audit(detail or {})),
            created_at=_aware_utc(now or datetime.now(UTC)),
        )
        async with self._database.sessions() as session, session.begin():
            session.add(row)
            await session.flush()
            return _audit_record(row)

    async def history(
        self,
        *,
        plugin_id: str | None = None,
        actor_user_id: str | None = None,
        limit: int = 100,
    ) -> tuple[PluginAuditEventRecord, ...]:
        statement = select(PluginAuditEventModel)
        if plugin_id is not None:
            statement = statement.where(PluginAuditEventModel.plugin_id == plugin_id)
        if actor_user_id is not None:
            statement = statement.where(PluginAuditEventModel.actor_user_id == actor_user_id)
        statement = statement.order_by(
            PluginAuditEventModel.created_at.desc(), PluginAuditEventModel.id.desc()
        ).limit(max(1, min(limit, 1_000)))
        async with self._database.sessions() as session:
            rows = (await session.scalars(statement)).all()
            return tuple(_audit_record(row) for row in rows)


def _installation_record(row: PluginInstallationModel) -> PluginInstallationRecord:
    return PluginInstallationRecord(
        plugin_id=row.plugin_id,
        name=row.name,
        version=row.version,
        plugin_api=row.plugin_api,
        yuki_requires=row.yuki_requires,
        manifest_hash=row.manifest_hash,
        entrypoint=row.entrypoint,
        status=row.status,
        enabled=row.enabled,
        approved_permissions=_decode_permissions(row.approved_permissions_json),
        requested_permissions=_decode_permissions(row.requested_permissions_json),
        failure_count=row.failure_count,
        last_error_category=row.last_error_category,
        discovered_at=_aware_utc(row.discovered_at),
        approved_at=_aware_utc(row.approved_at) if row.approved_at else None,
        started_at=_aware_utc(row.started_at) if row.started_at else None,
        updated_at=_aware_utc(row.updated_at),
    )


def _config_record(row: PluginConfigValueModel) -> PluginConfigValueRecord:
    return PluginConfigValueRecord(
        id=row.id,
        plugin_id=row.plugin_id,
        scope_type=row.scope_type,
        scope_id=row.scope_id,
        key=row.key,
        value=_decode(row.value_json),
        version=row.version,
        updated_at=_aware_utc(row.updated_at),
    )


def _state_record(row: PluginStateModel) -> PluginStateRecord:
    return PluginStateRecord(
        id=row.id,
        plugin_id=row.plugin_id,
        namespace=row.namespace,
        key=row.key,
        value=_decode(row.value_json),
        version=row.version,
        subject_user_id=row.subject_user_id,
        expires_at=_aware_utc(row.expires_at) if row.expires_at else None,
        updated_at=_aware_utc(row.updated_at),
    )


def _audit_record(row: PluginAuditEventModel) -> PluginAuditEventRecord:
    return PluginAuditEventRecord(
        id=row.id,
        plugin_id=row.plugin_id,
        actor_user_id=row.actor_user_id,
        operation=row.operation,
        permission=row.permission,
        success=row.success,
        error_category=row.error_category,
        detail=_decode(row.detail_json),
        created_at=_aware_utc(row.created_at),
    )


def _permissions(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip()[:128] for value in values or () if str(value).strip()}))


def _decode_permissions(value: str) -> tuple[str, ...]:
    decoded = _decode(value)
    if not isinstance(decoded, list):
        return ()
    return _permissions(str(item) for item in decoded)


def _validated_scope(scope_type: str, scope_id: str) -> str:
    if scope_type not in _VALID_SCOPES:
        raise ValueError("scope_type must be global, group, or user")
    normalized = scope_id.strip()
    if scope_type == "global" and normalized:
        raise ValueError("global plugin config must use an empty scope_id")
    if scope_type != "global" and not normalized:
        raise ValueError("group/user plugin config requires a scope_id")
    return normalized


def _storage_key(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        raise ValueError(f"{label} must contain 1 to 128 characters")
    return normalized


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _optional(value: str | None, limit: int) -> str | None:
    return value[:limit] if value else None


def _redact_audit(value: object, *, key: str = "") -> object:
    if any(token in key.casefold() for token in _AUDIT_SECRET_KEYS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key)[:128]: _redact_audit(item_value, key=str(item_key))
            for item_key, item_value in list(value.items())[:50]
        }
    if isinstance(value, list | tuple):
        return [_redact_audit(item) for item in value[:50]]
    if isinstance(value, str):
        return value if len(value) <= 512 else value[:512] + "[TRUNCATED]"
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:512]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
