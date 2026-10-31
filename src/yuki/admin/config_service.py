"""Validated SQLite runtime overrides, snapshots, audit history, and rollback."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Select, and_, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.audit import AdminAuditService, AuditSubject, add_audit_event, event_from_model
from yuki.admin.config_owners import resolve_space_owner
from yuki.admin.config_registry import ConfigRegistry
from yuki.admin.models import (
    AdminOperationEvent,
    AgentRuntimeConfig,
    ConfigApplyMode,
    ConfigChangeResult,
    ConfigScopeType,
    ConfigSpec,
    ConfigValue,
    ContextRuntimeConfig,
    ControlAuditRef,
    ConversationRuntimeConfig,
    EffectiveConfigValue,
    LLMRuntimeConfig,
    MemoryRetrievalRuntimeConfig,
    PluginRuntimeConfig,
    ReplyRuntimeConfig,
    RuntimeConfigSnapshot,
    ToolingRuntimeConfig,
    VisionRuntimeConfig,
    WebRuntimeConfig,
    WorkStorageRuntimeConfig,
)
from yuki.config import Settings
from yuki.domain.memory_config import MemoryConfigScope
from yuki.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.person_identity import (
    current_person_id,
    person_family_condition,
    person_root_id,
    same_person_id,
)
from yuki.persistence.database import Database, write_failure_committed
from yuki.persistence.models import AdminOperationEventModel, RuntimeConfigOverrideModel
from yuki.persistence.unit_of_work import optional_session


@dataclass(frozen=True, slots=True)
class RuntimeConfigOverrideRecord:
    """Storage-neutral runtime override projection."""

    id: int
    config_key: str
    scope_type: ConfigScopeType
    scope_id: str
    value: ConfigValue
    value_type: str
    apply_mode: ConfigApplyMode
    version: int
    created_at: datetime
    updated_at: datetime
    updated_by: str
    canonical_person_id: str | None = None
    canonical_space_id: str | None = None


class ConfigRollbackPreparationChanged(ValueError):
    def __init__(self) -> None:
        super().__init__("config_rollback_preparation_changed")


_ROLLBACK_AUDIT_FIELDS = (
    "capability",
    "operation",
    "success",
    "actor_user_id",
    "actor_principal_id",
    "actor_principal_kind",
    "target_type",
    "target_id",
    "before_json",
    "after_json",
)


@dataclass(frozen=True, slots=True)
class ConfigRollbackPreparation:
    original: AdminOperationEvent | None
    original_values: tuple[Any, ...] | None
    lineage_scope: str | None
    lineage_ids: tuple[int, ...]
    lineage_json: str
    lineage_matches: bool


def _rollback_lineage(scope_id: str, row_id: int, version: int) -> Select[Any]:
    audit = AdminOperationEventModel
    moves = func.json_each(audit.after_json, "$.configuration_moves").table_valued("value")
    selected = and_(
        func.json_extract(moves.c.value, "$.table") == "runtime_config_overrides",
        func.json_extract(moves.c.value, "$.id") == row_id,
        func.json_extract(moves.c.value, "$.version") == version,
    )
    original_moves = (
        select(
            func.json_group_array(
                func.json_object(
                    "from",
                    func.json_extract(moves.c.value, "$.from"),
                    "to",
                    func.json_extract(moves.c.value, "$.to"),
                )
            )
        )
        .select_from(moves)
        .where(selected)
        .correlate(audit)
        .scalar_subquery()
    )
    return (
        select(audit.id, original_moves.label("after_json"))
        .where(
            audit.capability == "identity.person.merge",
            audit.success.is_(True),
            person_family_condition(audit.target_id, scope_id),
            func.json_type(audit.after_json, "$.configuration_moves") == "array",
            select(moves.c.value).where(selected).correlate(audit).exists(),
        )
        .order_by(audit.id)
    )


@dataclass(frozen=True, slots=True)
class ConfigInspection:
    """Current process value and desired persisted value from the same resolver."""

    effective: EffectiveConfigValue
    saved: EffectiveConfigValue
    version: int | None

    @property
    def pending_restart(self) -> bool:
        return (
            self.effective.apply_mode is ConfigApplyMode.RESTART_REQUIRED
            and self.effective.value != self.saved.value
        )


def _record(row: RuntimeConfigOverrideModel) -> RuntimeConfigOverrideRecord:
    try:
        decoded: ConfigValue = json.loads(row.value_json)
    except json.JSONDecodeError:
        decoded = None
    return RuntimeConfigOverrideRecord(
        id=row.id,
        config_key=row.config_key,
        scope_type=ConfigScopeType(row.scope_type),
        scope_id=row.canonical_person_id or row.canonical_space_id or "",
        value=decoded,
        value_type=row.value_type,
        apply_mode=ConfigApplyMode(row.apply_mode),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
        canonical_person_id=row.canonical_person_id,
        canonical_space_id=row.canonical_space_id,
    )


class RuntimeConfigRepository:
    """Low-level persistence for validated overrides and atomic config audits."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def read_memory_snapshot(
        self,
        scope: MemoryConfigScope,
        *,
        session: AsyncSession | None = None,
    ) -> tuple[tuple[RuntimeConfigOverrideRecord, ...], str | None, str | None]:
        """Read existing canonical owners, including disabled historical owners.

        This is configuration lookup, not task admission or mutation authorization.
        """
        conditions: list[Any] = [
            RuntimeConfigOverrideModel.scope_type == ConfigScopeType.GLOBAL.value
        ]
        owned = session is None
        async with optional_session(self._database, session, write=False) as session:
            if owned:
                await session.execute(text("BEGIN"))
            person_id = None
            for owner_id, model, kind, column in (
                (
                    scope.person_id,
                    CanonicalPersonModel,
                    ConfigScopeType.USER,
                    RuntimeConfigOverrideModel.canonical_person_id,
                ),
                (
                    scope.space_id,
                    CanonicalSpaceModel,
                    ConfigScopeType.GROUP,
                    RuntimeConfigOverrideModel.canonical_space_id,
                ),
            ):
                if owner_id is None:
                    continue
                if await session.get(model, owner_id) is None:
                    raise CanonicalIdentityError("missing_or_invalid_canonical_config_owner")
                resolved_owner = (
                    await current_person_id(session, owner_id)
                    if kind is ConfigScopeType.USER
                    else owner_id
                )
                if kind is ConfigScopeType.USER:
                    person_id = resolved_owner
                conditions.append(
                    (RuntimeConfigOverrideModel.scope_type == kind.value)
                    & (column == resolved_owner)
                )
            rows = (
                await session.scalars(
                    select(RuntimeConfigOverrideModel)
                    .where(or_(*conditions))
                    .order_by(RuntimeConfigOverrideModel.id)
                )
            ).all()
            return tuple(_record(row) for row in rows), person_id, scope.space_id

    async def list_all(
        self,
        *,
        keys: tuple[str, ...] | None = None,
        session: AsyncSession | None = None,
        scope: MemoryConfigScope | None = None,
    ) -> tuple[RuntimeConfigOverrideRecord, ...]:
        statement = select(RuntimeConfigOverrideModel).where(
            RuntimeConfigOverrideModel.canonical_person_id.is_(None)
            | (
                RuntimeConfigOverrideModel.canonical_person_id
                == person_root_id(RuntimeConfigOverrideModel.canonical_person_id)
            )
        )
        if keys is not None:
            statement = statement.where(RuntimeConfigOverrideModel.config_key.in_(keys))
        if scope is not None:
            conditions = [RuntimeConfigOverrideModel.scope_type == ConfigScopeType.GLOBAL.value]
            for owner_id, kind, column in (
                (
                    scope.person_id,
                    ConfigScopeType.USER,
                    RuntimeConfigOverrideModel.canonical_person_id,
                ),
                (
                    scope.space_id,
                    ConfigScopeType.GROUP,
                    RuntimeConfigOverrideModel.canonical_space_id,
                ),
            ):
                if owner_id is not None:
                    conditions.append(
                        (RuntimeConfigOverrideModel.scope_type == kind.value)
                        & (
                            column
                            == (
                                person_root_id(owner_id)
                                if kind is ConfigScopeType.USER
                                else owner_id
                            )
                        )
                    )
            statement = statement.where(or_(*conditions))
        async with optional_session(self._database, session, write=False) as active:
            rows = (await active.scalars(statement)).all()
            return tuple(_record(row) for row in rows)

    async def list_relevant(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
        session: AsyncSession | None = None,
    ) -> tuple[RuntimeConfigOverrideRecord, ...]:
        async with optional_session(self._database, session, write=False) as active:
            records, _, _ = await self._read_relevant(active, user_id=user_id, group_id=group_id)
            return records

    async def read_relevant_snapshot(
        self, *, user_id: str | None, group_id: str | None, keys: tuple[str, ...] | None = None
    ) -> tuple[tuple[RuntimeConfigOverrideRecord, ...], str | None, str | None]:
        """Resolve owners and their overrides once in this preparation's read view."""
        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            return await self._read_relevant(session, user_id=user_id, group_id=group_id, keys=keys)

    async def _read_relevant(
        self,
        active: AsyncSession,
        *,
        user_id: str | None,
        group_id: str | None,
        keys: tuple[str, ...] | None = None,
    ) -> tuple[tuple[RuntimeConfigOverrideRecord, ...], str | None, str | None]:
        conditions: list[Any] = [
            RuntimeConfigOverrideModel.scope_type == ConfigScopeType.GLOBAL.value
        ]
        person_id = await current_person_id(active, user_id) if user_id else None
        space_id = await resolve_space_owner(active, group_id) if group_id else None
        for owner_id, kind, column in (
            (person_id, ConfigScopeType.USER, RuntimeConfigOverrideModel.canonical_person_id),
            (space_id, ConfigScopeType.GROUP, RuntimeConfigOverrideModel.canonical_space_id),
        ):
            if owner_id is not None:
                conditions.append(
                    (RuntimeConfigOverrideModel.scope_type == kind.value) & (column == owner_id)
                )
        rows = (
            await active.scalars(
                select(RuntimeConfigOverrideModel)
                .where(
                    or_(*conditions),
                    *(
                        [RuntimeConfigOverrideModel.config_key.in_(keys)]
                        if keys is not None
                        else []
                    ),
                )
                .order_by(
                    RuntimeConfigOverrideModel.config_key,
                    RuntimeConfigOverrideModel.scope_type,
                    RuntimeConfigOverrideModel.id,
                )
            )
        ).all()
        return tuple(_record(row) for row in rows), person_id, space_id

    async def get(
        self,
        *,
        key: str,
        scope_type: ConfigScopeType,
        scope_id: str,
        session: AsyncSession | None = None,
    ) -> RuntimeConfigOverrideRecord | None:
        async with optional_session(self._database, session, write=False) as active:
            if scope_type is ConfigScopeType.USER:
                person_id = await current_person_id(active, scope_id)
                matches = list(
                    await active.scalars(
                        select(RuntimeConfigOverrideModel).where(
                            RuntimeConfigOverrideModel.config_key == key,
                            RuntimeConfigOverrideModel.scope_type == scope_type.value,
                            RuntimeConfigOverrideModel.canonical_person_id == person_id,
                        )
                    )
                )
            elif scope_type is ConfigScopeType.GROUP:
                space_id = await resolve_space_owner(active, scope_id)
                matches = list(
                    await active.scalars(
                        select(RuntimeConfigOverrideModel).where(
                            RuntimeConfigOverrideModel.config_key == key,
                            RuntimeConfigOverrideModel.scope_type == scope_type.value,
                            RuntimeConfigOverrideModel.canonical_space_id == space_id,
                        )
                    )
                )
            else:
                matches = list(
                    await active.scalars(
                        select(RuntimeConfigOverrideModel).where(
                            RuntimeConfigOverrideModel.config_key == key,
                            RuntimeConfigOverrideModel.scope_type == scope_type.value,
                            RuntimeConfigOverrideModel.canonical_person_id.is_(None),
                            RuntimeConfigOverrideModel.canonical_space_id.is_(None),
                        )
                    )
                )
            if len(matches) > 1:
                raise CanonicalIdentityError("canonical_owner_mismatch")
            return _record(matches[0]) if matches else None

    async def save_with_audit(
        self,
        *,
        spec: ConfigSpec,
        value: ConfigValue,
        scope_type: ConfigScopeType,
        scope_id: str,
        actor: AuditSubject,
        before_state: dict[str, object],
        started: float,
        initial_version: int = 1,
        operation: str = "set_override",
        person_id: str | None = None,
        space_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> tuple[RuntimeConfigOverrideRecord, AdminOperationEvent]:
        now = datetime.now(UTC)
        async with optional_session(self._database, session, write=True) as active:
            owner_clause = _config_owner_clause(
                scope_type,
                person_id=person_id,
                space_id=space_id,
            )
            row = await active.scalar(
                select(RuntimeConfigOverrideModel).where(
                    RuntimeConfigOverrideModel.config_key == spec.key,
                    owner_clause,
                )
            )
            if row is None:
                row = RuntimeConfigOverrideModel(
                    config_key=spec.key,
                    scope_type=scope_type.value,
                    value_json=json.dumps(value, ensure_ascii=False),
                    value_type=spec.value_type,
                    apply_mode=spec.apply_mode.value,
                    version=max(1, initial_version),
                    created_at=now,
                    updated_at=now,
                    updated_by=actor.user_id,
                    canonical_person_id=person_id,
                    canonical_space_id=space_id,
                )
                active.add(row)
            else:
                row.value_json = json.dumps(value, ensure_ascii=False)
                row.value_type = spec.value_type
                row.apply_mode = spec.apply_mode.value
                row.version += 1
                row.updated_at = now
                row.updated_by = actor.user_id
            await active.flush()
            after_state = _override_state(
                _record(row),
                public_scope_id=_public_owner_scope_id(
                    person_id=person_id,
                    space_id=space_id,
                    storage_scope_id=scope_id,
                ),
            )
            audit = await add_audit_event(
                active,
                actor=actor,
                capability="runtime_config",
                operation=operation,
                target_type=f"config.{scope_type.value}",
                target_id=spec.key,
                before=before_state,
                after=after_state,
                success=True,
                error_category=None,
                duration_seconds=time.perf_counter() - started,
            )
            return _record(row), event_from_model(audit)

    async def delete_with_audit(
        self,
        *,
        spec: ConfigSpec,
        scope_type: ConfigScopeType,
        scope_id: str,
        actor: AuditSubject,
        before: RuntimeConfigOverrideRecord,
        started: float,
        operation: str = "delete_override",
        public_scope_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> AdminOperationEvent:
        exposed_scope_id = scope_id if public_scope_id is None else public_scope_id
        async with optional_session(self._database, session, write=True) as active:
            person_id = scope_id if scope_type is ConfigScopeType.USER else None
            space_id = scope_id if scope_type is ConfigScopeType.GROUP else None
            await active.execute(
                delete(RuntimeConfigOverrideModel).where(
                    RuntimeConfigOverrideModel.config_key == spec.key,
                    _config_owner_clause(
                        scope_type,
                        person_id=person_id,
                        space_id=space_id,
                    ),
                )
            )
            audit = await add_audit_event(
                active,
                actor=actor,
                capability="runtime_config",
                operation=operation,
                target_type=f"config.{scope_type.value}",
                target_id=spec.key,
                before=_override_state(before, public_scope_id=public_scope_id),
                after=_missing_override_state(spec.key, scope_type, exposed_scope_id),
                success=True,
                error_category=None,
                duration_seconds=time.perf_counter() - started,
            )
            return event_from_model(audit)


def _public_owner_scope_id(
    *,
    person_id: str | None,
    space_id: str | None,
    storage_scope_id: str,
) -> str:
    """Public owner id for a canonical config scope."""

    return person_id or space_id or storage_scope_id


def _config_owner_clause(
    scope_type: ConfigScopeType,
    *,
    person_id: str | None,
    space_id: str | None,
) -> Any:
    if scope_type is ConfigScopeType.USER:
        if not person_id or space_id is not None:
            raise CanonicalIdentityError("missing_canonical_owner")
        return (
            (RuntimeConfigOverrideModel.scope_type == scope_type.value)
            & (RuntimeConfigOverrideModel.canonical_person_id == person_id)
            & RuntimeConfigOverrideModel.canonical_space_id.is_(None)
        )
    if scope_type is ConfigScopeType.GROUP:
        if not space_id or person_id is not None:
            raise CanonicalIdentityError("missing_canonical_owner")
        return (
            (RuntimeConfigOverrideModel.scope_type == scope_type.value)
            & RuntimeConfigOverrideModel.canonical_person_id.is_(None)
            & (RuntimeConfigOverrideModel.canonical_space_id == space_id)
        )
    if person_id is not None or space_id is not None:
        raise CanonicalIdentityError("canonical_owner_mismatch")
    return (
        (RuntimeConfigOverrideModel.scope_type == scope_type.value)
        & RuntimeConfigOverrideModel.canonical_person_id.is_(None)
        & RuntimeConfigOverrideModel.canonical_space_id.is_(None)
    )


def _missing_override_state(
    key: str,
    scope_type: ConfigScopeType,
    scope_id: str,
) -> dict[str, object]:
    return {
        "key": key,
        "scope_type": scope_type.value,
        "scope_id": scope_id,
        "override_exists": False,
        "value": None,
        "version": None,
    }


def _override_state(
    record: RuntimeConfigOverrideRecord,
    *,
    public_scope_id: str | None = None,
) -> dict[str, object]:
    return {
        "key": record.config_key,
        "scope_type": record.scope_type.value,
        "scope_id": record.scope_id if public_scope_id is None else public_scope_id,
        "override_exists": True,
        "row_id": record.id,
        "value": record.value,
        "version": record.version,
    }


def _state_value(state: object, key: str) -> object:
    return state.get(key) if isinstance(state, dict) else None


class RuntimeConfigService:
    """Resolve, validate, persist, audit, snapshot, and roll back runtime settings."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        registry: ConfigRegistry | None = None,
        repository: RuntimeConfigRepository | None = None,
        audit: AdminAuditService | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self.registry = registry or ConfigRegistry()
        self._repository = repository or RuntimeConfigRepository(database)
        self._audit = audit or AdminAuditService(database)
        self._active_restart: dict[
            tuple[str, ConfigScopeType, str], RuntimeConfigOverrideRecord
        ] = {}

    async def initialize(self) -> None:
        """Activate persisted restart-required overrides for this process."""

        records = await self._repository.list_all()
        self._active_restart = {
            (row.config_key, row.scope_type, row.scope_id): row
            for row in records
            if row.apply_mode is ConfigApplyMode.RESTART_REQUIRED and self._valid_stored_record(row)
        }

    async def startup_settings_updates(self) -> dict[str, object]:
        """Map activated global restart overrides back to long-lived Settings fields."""

        updates: dict[str, object] = {}
        for spec in self.registry.list():
            if spec.apply_mode is not ConfigApplyMode.RESTART_REQUIRED:
                continue
            effective = self._resolve(spec, (), user_id=None, group_id=None)
            if effective.value is not None:
                for field_name in spec.settings_fields:
                    updates[field_name] = effective.value
        return updates

    async def get_effective(
        self,
        key: str,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> EffectiveConfigValue:
        spec = self.registry.get(key)
        if spec.apply_mode is ConfigApplyMode.SECRET or spec.sensitive:
            return EffectiveConfigValue(
                key=spec.key,
                value=None,
                source="protected",
                scope_type=None,
                scope_id="",
                apply_mode=spec.apply_mode,
                configured=bool(spec.default_getter(self._settings)),
            )
        if session is None:
            records, person_id, space_id = await self._repository.read_relevant_snapshot(
                user_id=user_id, group_id=group_id
            )
        else:
            records, person_id, space_id = await self._repository._read_relevant(
                session, user_id=user_id, group_id=group_id
            )
        return self._resolve(
            spec,
            records,
            user_id=user_id,
            group_id=group_id,
            person_id=person_id,
            space_id=space_id,
        )

    async def inspect_configs(
        self,
        keys: tuple[str, ...],
        *,
        scope: MemoryConfigScope | None = None,
        session: AsyncSession | None = None,
    ) -> tuple[ConfigInspection, ...]:
        """Inspect canonical owners without transport identity lookup or writes."""
        scope = scope if scope is not None else MemoryConfigScope()
        specs = tuple(self.registry.get(key) for key in keys)
        records, person_id, space_id = await self._repository.read_memory_snapshot(
            scope, session=session
        )
        inspections: list[ConfigInspection] = []
        for spec in specs:
            if spec.sensitive or spec.apply_mode is ConfigApplyMode.SECRET:
                protected = EffectiveConfigValue(
                    key=spec.key,
                    value=None,
                    source="protected",
                    scope_type=None,
                    scope_id="",
                    apply_mode=spec.apply_mode,
                    configured=bool(spec.default_getter(self._settings)),
                )
                inspections.append(ConfigInspection(protected, protected, None))
                continue
            effective = self._resolve(
                spec,
                records,
                user_id=None,
                group_id=None,
                person_id=person_id,
                space_id=space_id,
            )
            saved = self._resolve(
                spec,
                records,
                user_id=None,
                group_id=None,
                person_id=person_id,
                space_id=space_id,
                honor_restart_activation=False,
            )
            selected = (
                self._canonical_row(
                    [
                        row
                        for row in records
                        if row.config_key == spec.key and self._valid_stored_record(row)
                    ],
                    scope=saved.scope_type,
                    person_id=person_id,
                    space_id=space_id,
                )
                if saved.scope_type is not None
                else None
            )
            inspections.append(
                ConfigInspection(effective, saved, selected.version if selected else None)
            )
        return tuple(inspections)

    async def set_override(
        self,
        key: str,
        value: object,
        *,
        scope_type: str,
        scope_id: str,
        actor_user_id: str,
        trigger_message_id: str,
        conversation_key: str = "",
        audit_ref: ControlAuditRef | None = None,
        expected_version: int | None = None,
        session: AsyncSession | None = None,
    ) -> ConfigChangeResult:
        started = time.perf_counter()
        actor = audit_ref or self._audit_ref(
            actor_user_id,
            trigger_message_id=trigger_message_id,
            conversation_key=conversation_key,
        )
        raw_scope = scope_type
        spec: ConfigSpec | None = None
        try:
            spec = self.registry.get(key)
            scope, normalized_scope_id = self._validate_write(
                spec,
                scope_type,
                scope_id,
            )
            converted = self.registry.convert(spec, value)
        except (KeyError, PermissionError, ValueError) as exc:
            category = self._error_category(exc)
            await self._audit.record(
                actor=actor,
                capability="runtime_config",
                operation="set_override",
                target_type=f"config.{raw_scope[:16]}",
                target_id=spec.key if spec is not None else key[:128],
                before=None,
                after={"attempted": "[REDACTED]"},
                success=False,
                error_category=category,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
            return ConfigChangeResult(
                success=False,
                key=spec.key if spec is not None else key,
                scope_type=self._safe_scope(scope_type),
                scope_id=scope_id,
                apply_mode=spec.apply_mode if spec is not None else None,
                error_category=category,
                detail=str(exc),
            )

        async with optional_session(self._database, session, write=True) as session:
            try:
                storage_scope_id, person_id, space_id = await self._bind_write_scope(
                    scope,
                    normalized_scope_id,
                    session=session,
                )
                public_scope_id = _public_owner_scope_id(
                    person_id=person_id,
                    space_id=space_id,
                    storage_scope_id=storage_scope_id,
                )
                before_effective = await self.get_effective(
                    spec.key,
                    user_id=normalized_scope_id if scope is ConfigScopeType.USER else None,
                    group_id=normalized_scope_id if scope is ConfigScopeType.GROUP else None,
                    session=session,
                )
                before_override = await self._repository.get(
                    key=spec.key,
                    scope_type=scope,
                    scope_id=storage_scope_id,
                    session=session,
                )
                actual_version = 0 if before_override is None else before_override.version
                if expected_version is not None and actual_version != expected_version:
                    return ConfigChangeResult(
                        success=False,
                        key=spec.key,
                        scope_type=scope,
                        scope_id=public_scope_id,
                        apply_mode=spec.apply_mode,
                        error_category="version_conflict",
                        detail="expected_version does not match the current override",
                    )
            except ValueError as exc:
                category = self._error_category(exc)
                await self._audit.record(
                    actor=actor,
                    capability="runtime_config",
                    operation="set_override",
                    target_type=f"config.{scope.value}",
                    target_id=spec.key,
                    before=None,
                    after={"attempted": converted},
                    success=False,
                    error_category=category,
                    duration_seconds=time.perf_counter() - started,
                    session=session,
                )
                return ConfigChangeResult(
                    success=False,
                    key=spec.key,
                    scope_type=scope,
                    scope_id=normalized_scope_id,
                    apply_mode=spec.apply_mode,
                    error_category=category,
                    detail=str(exc),
                )

            row, audit = await self._repository.save_with_audit(
                spec=spec,
                value=converted,
                scope_type=scope,
                scope_id=storage_scope_id,
                actor=actor,
                before_state=(
                    _override_state(before_override, public_scope_id=public_scope_id)
                    if before_override is not None
                    else _missing_override_state(spec.key, scope, public_scope_id)
                ),
                started=started,
                person_id=person_id,
                space_id=space_id,
                session=session,
            )
            pending_restart = (
                spec.apply_mode is ConfigApplyMode.RESTART_REQUIRED
                and converted != before_effective.value
            )
            return ConfigChangeResult(
                success=True,
                key=spec.key,
                scope_type=scope,
                scope_id=public_scope_id,
                before=before_effective.value,
                after=converted,
                apply_mode=spec.apply_mode,
                pending_restart=pending_restart,
                change_id=audit.id,
                version=row.version,
                detail=self._apply_detail(
                    spec.apply_mode,
                    pending_restart=pending_restart,
                ),
            )

    async def delete_override(
        self,
        key: str,
        *,
        scope_type: str,
        scope_id: str,
        actor_user_id: str,
        trigger_message_id: str,
        conversation_key: str = "",
        audit_ref: ControlAuditRef | None = None,
        expected_version: int | None = None,
        session: AsyncSession | None = None,
    ) -> ConfigChangeResult:
        started = time.perf_counter()
        actor = audit_ref or self._audit_ref(
            actor_user_id,
            trigger_message_id=trigger_message_id,
            conversation_key=conversation_key,
        )
        spec: ConfigSpec | None = None
        try:
            spec = self.registry.get(key)
            scope, normalized_scope_id = self._validate_write(
                spec,
                scope_type,
                scope_id,
            )
        except (KeyError, PermissionError, ValueError) as exc:
            category = self._error_category(exc)
            await self._audit.record(
                actor=actor,
                capability="runtime_config",
                operation="delete_override",
                target_type=f"config.{scope_type[:16]}",
                target_id=spec.key if spec is not None else key[:128],
                success=False,
                error_category=category,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
            return ConfigChangeResult(
                False,
                spec.key if spec else key,
                self._safe_scope(scope_type),
                scope_id,
                apply_mode=spec.apply_mode if spec else None,
                error_category=category,
                detail=str(exc),
            )
        async with optional_session(self._database, session, write=True) as session:
            storage_scope_id, bound_person_id, bound_space_id = await self._bind_write_scope(
                scope,
                normalized_scope_id,
                session=session,
            )
            public_scope_id = _public_owner_scope_id(
                person_id=bound_person_id,
                space_id=bound_space_id,
                storage_scope_id=storage_scope_id,
            )
            before = await self._repository.get(
                key=spec.key,
                scope_type=scope,
                scope_id=storage_scope_id,
                session=session,
            )
            if before is None:
                await self._audit.record(
                    actor=actor,
                    capability="runtime_config",
                    operation="delete_override",
                    target_type=f"config.{scope.value}",
                    target_id=spec.key,
                    success=False,
                    error_category="not_found",
                    duration_seconds=time.perf_counter() - started,
                    session=session,
                )
                return ConfigChangeResult(
                    False,
                    spec.key,
                    scope,
                    public_scope_id,
                    apply_mode=spec.apply_mode,
                    error_category="not_found",
                    detail="当前作用域没有数据库覆盖值",
                )
            if expected_version is not None and before.version != expected_version:
                return ConfigChangeResult(
                    False,
                    spec.key,
                    scope,
                    public_scope_id,
                    apply_mode=spec.apply_mode,
                    error_category="version_conflict",
                    detail="expected_version does not match the current override",
                )
            audit = await self._repository.delete_with_audit(
                spec=spec,
                scope_type=scope,
                scope_id=before.scope_id,
                actor=actor,
                before=before,
                started=started,
                public_scope_id=public_scope_id,
                session=session,
            )
            remaining = await self._repository.list_relevant(
                user_id=normalized_scope_id if scope is ConfigScopeType.USER else None,
                group_id=normalized_scope_id if scope is ConfigScopeType.GROUP else None,
                session=session,
            )
            person_id, space_id = await self._owner_match(
                user_id=normalized_scope_id if scope is ConfigScopeType.USER else None,
                group_id=normalized_scope_id if scope is ConfigScopeType.GROUP else None,
                session=session,
            )
            after_effective = self._resolve(
                spec,
                remaining,
                user_id=normalized_scope_id if scope is ConfigScopeType.USER else None,
                group_id=normalized_scope_id if scope is ConfigScopeType.GROUP else None,
                honor_restart_activation=False,
                person_id=person_id,
                space_id=space_id,
            )
            active_effective = await self.get_effective(
                spec.key,
                user_id=normalized_scope_id if scope is ConfigScopeType.USER else None,
                group_id=normalized_scope_id if scope is ConfigScopeType.GROUP else None,
                session=session,
            )
            pending_restart = (
                spec.apply_mode is ConfigApplyMode.RESTART_REQUIRED
                and after_effective.value != active_effective.value
            )
            return ConfigChangeResult(
                True,
                spec.key,
                scope,
                public_scope_id,
                before=before.value,
                after=after_effective.value,
                apply_mode=spec.apply_mode,
                pending_restart=pending_restart,
                change_id=audit.id,
                detail=self._apply_detail(
                    spec.apply_mode,
                    pending_restart=pending_restart,
                ),
            )

    async def history(
        self,
        *,
        key: str | None = None,
        actor_user_id: str | None = None,
        limit: int = 20,
    ) -> tuple[AdminOperationEvent, ...]:
        normalized_key = self.registry.get(key).key if key else None
        return await self._audit.history(
            key=normalized_key,
            actor_user_id=actor_user_id,
            capability="runtime_config",
            limit=limit,
        )

    async def prepare_rollback(self, change_id: int) -> ConfigRollbackPreparation:
        async with self._database.read_session() as reader:
            await reader.execute(text("BEGIN"))
            original = await self._audit.get(change_id, session=reader)
            values = (
                await reader.execute(
                    select(
                        *(
                            getattr(AdminOperationEventModel, field)
                            for field in _ROLLBACK_AUDIT_FIELDS
                        )
                    ).where(AdminOperationEventModel.id == change_id)
                )
            ).one_or_none()
            lineage_scope = None
            moves: tuple[tuple[Any, ...], ...] = ()
            matches = False
            if (
                original is not None
                and original.success
                and original.capability == "runtime_config"
                and original.operation in {"set_override", "delete_override"}
                and _state_value(original.before, "scope_type") == "user"
                and _state_value(original.after, "row_id") is None
            ):
                original_scope = str(_state_value(original.before, "scope_id") or "")
                lineage_scope = await current_person_id(reader, original_scope)
                key = str(_state_value(original.before, "key") or original.target_id)
                current = await self._repository.get(
                    key=key,
                    scope_type=ConfigScopeType.USER,
                    scope_id=lineage_scope,
                    session=reader,
                )
                if current is not None and current.scope_id != original_scope:
                    moves = tuple(
                        tuple(row)
                        for row in await reader.execute(
                            _rollback_lineage(lineage_scope, current.id, current.version)
                        )
                    )
                    lineage = original_scope
                    for _, raw in moves:
                        for move in json.loads(raw):
                            if move["from"] == lineage:
                                lineage = move["to"]
                    matches = lineage == current.scope_id
            return ConfigRollbackPreparation(
                original,
                tuple(values) if values is not None else None,
                lineage_scope,
                tuple(row[0] for row in moves),
                json.dumps(moves),
                matches,
            )

    async def rollback(
        self,
        change_id: int,
        *,
        actor_user_id: str,
        trigger_message_id: str = "",
        conversation_key: str = "",
        audit_ref: ControlAuditRef | None = None,
        expected_version: int | None = None,
        session: AsyncSession | None = None,
        prepared: ConfigRollbackPreparation | None = None,
    ) -> ConfigChangeResult:
        if session is None:
            while True:
                prepared = await self.prepare_rollback(change_id)
                try:
                    async with self._database.immediate_session() as writer:
                        return await self.rollback(
                            change_id,
                            actor_user_id=actor_user_id,
                            trigger_message_id=trigger_message_id,
                            conversation_key=conversation_key,
                            audit_ref=audit_ref,
                            expected_version=expected_version,
                            session=writer,
                            prepared=prepared,
                        )
                except ConfigRollbackPreparationChanged as exc:
                    if write_failure_committed(exc) is not False:
                        raise
                    continue
        if prepared is None:
            raise ValueError("config_rollback_preparation_required")
        if prepared.original_values is None:
            unchanged = (
                await session.scalar(
                    select(AdminOperationEventModel.id).where(
                        AdminOperationEventModel.id == change_id
                    )
                )
                is None
            )
        else:
            unchanged = bool(
                await session.scalar(
                    select(
                        and_(
                            *(
                                getattr(AdminOperationEventModel, field).is_(value)
                                for field, value in zip(
                                    _ROLLBACK_AUDIT_FIELDS, prepared.original_values, strict=True
                                )
                            )
                        )
                    ).where(AdminOperationEventModel.id == change_id)
                )
            )
        if not unchanged:
            raise ConfigRollbackPreparationChanged()
        started = time.perf_counter()
        actor = audit_ref or self._audit_ref(
            actor_user_id,
            trigger_message_id=trigger_message_id,
            conversation_key=conversation_key,
        )
        original = prepared.original
        fallback_scope = ConfigScopeType.GLOBAL
        if (
            original is None
            or original.capability != "runtime_config"
            or original.operation not in {"set_override", "delete_override"}
            or not original.success
        ):
            await self._audit.record(
                actor=actor,
                capability="runtime_config",
                operation="rollback",
                target_type="config",
                target_id=str(change_id),
                success=False,
                error_category="not_rollbackable",
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
            return ConfigChangeResult(
                False,
                "",
                fallback_scope,
                "",
                error_category="not_rollbackable",
                detail="该变更不存在或不属于可恢复的配置修改",
            )
        if not await self._same_config_actor(original, actor, session=session):
            await self._audit.record(
                actor=actor,
                capability="runtime_config",
                operation="rollback",
                target_type=original.target_type,
                target_id=original.target_id,
                success=False,
                error_category="permission_denied",
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
            return ConfigChangeResult(
                False,
                original.target_id,
                fallback_scope,
                "",
                error_category="permission_denied",
                detail="只能恢复当前管理员本人执行的配置修改",
            )

        before_state = original.before
        after_state = original.after
        try:
            key = str(_state_value(before_state, "key") or original.target_id)
            spec = self.registry.get(key)
            scope = ConfigScopeType(str(_state_value(before_state, "scope_type")))
            scope_id = str(_state_value(before_state, "scope_id") or "")
            self._validate_write(spec, scope.value, scope_id)
        except (KeyError, PermissionError, ValueError) as exc:
            category = self._error_category(exc)
            await self._audit.record(
                actor=actor,
                capability="runtime_config",
                operation="rollback",
                target_type=original.target_type,
                target_id=original.target_id,
                before=original.after,
                after={"change_id": change_id},
                success=False,
                error_category=category,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
            return ConfigChangeResult(
                False,
                original.target_id,
                fallback_scope,
                "",
                error_category=category,
                detail=str(exc),
            )

        async with optional_session(self._database, session, write=True) as session:
            storage_scope_id, person_id, space_id = await self._bind_write_scope(
                scope,
                scope_id,
                session=session,
            )
            public_scope_id = _public_owner_scope_id(
                person_id=person_id,
                space_id=space_id,
                storage_scope_id=storage_scope_id,
            )
            current = await self._repository.get(
                key=spec.key,
                scope_type=scope,
                scope_id=storage_scope_id,
                session=session,
            )
            actual_version = 0 if current is None else current.version
            if expected_version is not None and actual_version != expected_version:
                return ConfigChangeResult(
                    False,
                    spec.key,
                    scope,
                    public_scope_id,
                    apply_mode=spec.apply_mode,
                    error_category="version_conflict",
                    detail="expected_version does not match the current override",
                )
            if not await self._matches_original_state(
                session,
                current,
                after_state,
                original_scope_id=scope_id,
                prepared=prepared,
            ):
                await self._audit.record(
                    actor=actor,
                    capability="runtime_config",
                    operation="rollback",
                    target_type=f"config.{scope.value}",
                    target_id=spec.key,
                    before=(
                        _override_state(current, public_scope_id=public_scope_id)
                        if current
                        else None
                    ),
                    after={"change_id": change_id},
                    success=False,
                    error_category="rollback_conflict",
                    duration_seconds=time.perf_counter() - started,
                    session=session,
                )
                return ConfigChangeResult(
                    False,
                    spec.key,
                    scope,
                    public_scope_id,
                    apply_mode=spec.apply_mode,
                    error_category="rollback_conflict",
                    detail="该作用域之后已有其他修改，拒绝覆盖较新的值",
                )

            restore_exists = bool(_state_value(before_state, "override_exists"))
            if restore_exists:
                restore_value = _state_value(before_state, "value")
                try:
                    converted = self.registry.convert(spec, restore_value)
                except ValueError as exc:
                    await self._audit.record(
                        actor=actor,
                        capability="runtime_config",
                        operation="rollback",
                        target_type=f"config.{scope.value}",
                        target_id=spec.key,
                        before=(
                            _override_state(current, public_scope_id=public_scope_id)
                            if current
                            else None
                        ),
                        after={"change_id": change_id},
                        success=False,
                        error_category="validation_error",
                        duration_seconds=time.perf_counter() - started,
                        session=session,
                    )
                    return ConfigChangeResult(
                        False,
                        spec.key,
                        scope,
                        public_scope_id,
                        apply_mode=spec.apply_mode,
                        error_category="validation_error",
                        detail=str(exc),
                    )

                prior_version = _state_value(before_state, "version")
                initial = (
                    int(prior_version) + 1
                    if isinstance(prior_version, int) and not isinstance(prior_version, bool)
                    else 1
                )
                row, audit = await self._repository.save_with_audit(
                    spec=spec,
                    value=converted,
                    scope_type=scope,
                    scope_id=storage_scope_id,
                    actor=actor,
                    before_state=(
                        _override_state(current, public_scope_id=public_scope_id)
                        if current
                        else _missing_override_state(spec.key, scope, public_scope_id)
                    ),
                    started=started,
                    initial_version=initial,
                    operation=f"rollback:{change_id}",
                    person_id=person_id,
                    space_id=space_id,
                    session=session,
                )
                active_effective = await self.get_effective(
                    spec.key,
                    user_id=scope_id if scope is ConfigScopeType.USER else None,
                    group_id=scope_id if scope is ConfigScopeType.GROUP else None,
                    session=session,
                )
                pending_restart = (
                    spec.apply_mode is ConfigApplyMode.RESTART_REQUIRED
                    and converted != active_effective.value
                )
                return ConfigChangeResult(
                    True,
                    spec.key,
                    scope,
                    public_scope_id,
                    before=current.value if current else None,
                    after=converted,
                    apply_mode=spec.apply_mode,
                    pending_restart=pending_restart,
                    change_id=audit.id,
                    version=row.version,
                    detail=self._apply_detail(
                        spec.apply_mode,
                        pending_restart=pending_restart,
                    ),
                )
            if current is None:
                await self._audit.record(
                    actor=actor,
                    capability="runtime_config",
                    operation="rollback",
                    target_type=f"config.{scope.value}",
                    target_id=spec.key,
                    before=None,
                    after={"change_id": change_id},
                    success=False,
                    error_category="rollback_conflict",
                    duration_seconds=time.perf_counter() - started,
                    session=session,
                )
                return ConfigChangeResult(
                    False,
                    spec.key,
                    scope,
                    public_scope_id,
                    apply_mode=spec.apply_mode,
                    error_category="rollback_conflict",
                    detail="当前覆盖已经不存在",
                )
            audit = await self._repository.delete_with_audit(
                spec=spec,
                scope_type=scope,
                scope_id=storage_scope_id,
                actor=actor,
                before=current,
                started=started,
                operation=f"rollback:{change_id}",
                public_scope_id=public_scope_id,
                session=session,
            )
            remaining = await self._repository.list_relevant(
                user_id=scope_id if scope is ConfigScopeType.USER else None,
                group_id=scope_id if scope is ConfigScopeType.GROUP else None,
                session=session,
            )
            person_id, space_id = await self._owner_match(
                user_id=scope_id if scope is ConfigScopeType.USER else None,
                group_id=scope_id if scope is ConfigScopeType.GROUP else None,
                session=session,
            )
            effective = self._resolve(
                spec,
                remaining,
                user_id=scope_id if scope is ConfigScopeType.USER else None,
                group_id=scope_id if scope is ConfigScopeType.GROUP else None,
                honor_restart_activation=False,
                person_id=person_id,
                space_id=space_id,
            )
            active_effective = await self.get_effective(
                spec.key,
                user_id=scope_id if scope is ConfigScopeType.USER else None,
                group_id=scope_id if scope is ConfigScopeType.GROUP else None,
                session=session,
            )
            pending_restart = (
                spec.apply_mode is ConfigApplyMode.RESTART_REQUIRED
                and effective.value != active_effective.value
            )
            return ConfigChangeResult(
                True,
                spec.key,
                scope,
                public_scope_id,
                before=current.value,
                after=effective.value,
                apply_mode=spec.apply_mode,
                pending_restart=pending_restart,
                change_id=audit.id,
                detail=self._apply_detail(
                    spec.apply_mode,
                    pending_restart=pending_restart,
                ),
            )

    async def pending_restart_count(self) -> int:
        return len(await self.pending_restart_entries())

    async def pending_restart_entries(
        self, *, session: AsyncSession | None = None
    ) -> tuple[str, ...]:
        """One key per changed owner; activated overrides are not pending."""
        current = {
            (row.config_key, row.scope_type, row.scope_id): row
            for row in await self._repository.list_all(session=session)
            if row.apply_mode is ConfigApplyMode.RESTART_REQUIRED and self._valid_stored_record(row)
        }
        keys = set(current) | set(self._active_restart)
        pending: list[str] = []
        for key in sorted(keys):
            config_key, _scope_type, _scope_id = key
            spec = self.registry.get(config_key)
            current_value = (
                current[key].value if key in current else spec.default_getter(self._settings)
            )
            active_value = (
                self._active_restart[key].value
                if key in self._active_restart
                else spec.default_getter(self._settings)
            )
            if current_value != active_value:
                pending.append(config_key)
        return tuple(pending)

    async def participation_policy(self, *, group_id: str | None) -> ConversationRuntimeConfig:
        """Read only participation keys in the original owner-resolution snapshot."""
        keys = tuple(key for key in self.registry.keys if key.startswith("conversation."))
        records, person_id, space_id = await self._repository.read_relevant_snapshot(
            user_id=None, group_id=group_id, keys=keys
        )
        by_key: dict[str, list[RuntimeConfigOverrideRecord]] = defaultdict(list)
        for row in records:
            by_key[row.config_key].append(row)

        def value(key: str) -> ConfigValue:
            return self._resolve(
                self.registry.get(key),
                tuple(by_key.get(key, ())),
                user_id=None,
                group_id=group_id,
                person_id=person_id,
                space_id=space_id,
            ).value

        return _conversation_policy(value)

    async def snapshot(
        self,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
        memory_scope: MemoryConfigScope | None = None,
    ) -> RuntimeConfigSnapshot:
        if memory_scope is not None:
            if user_id is not None or group_id is not None:
                raise ValueError("memory config scope cannot mix canonical and external owners")
            records, person_id, space_id = await self._repository.read_memory_snapshot(memory_scope)
        else:
            records, person_id, space_id = await self._repository.read_relevant_snapshot(
                user_id=user_id, group_id=group_id
            )
        by_key: dict[str, list[RuntimeConfigOverrideRecord]] = defaultdict(list)
        for row in records:
            by_key[row.config_key].append(row)
        restart_by_key: dict[str, list[RuntimeConfigOverrideRecord]] = defaultdict(list)
        for row in self._active_restart.values():
            restart_by_key[row.config_key].append(row)

        def value(key: str) -> ConfigValue:
            spec = self.registry.get(key)
            shared_context = spec.category == "context" and space_id is not None
            return self._resolve(
                spec,
                tuple(
                    (
                        restart_by_key
                        if spec.apply_mode is ConfigApplyMode.RESTART_REQUIRED
                        else by_key
                    ).get(key, ())
                ),
                user_id=None if shared_context else user_id,
                group_id=group_id,
                person_id=None if shared_context else person_id,
                space_id=space_id,
                honor_restart_activation=False,
            ).value

        delay_min = float(cast(float | int, value("reply.delay_min_seconds")))
        delay_max = float(cast(float | int, value("reply.delay_max_seconds")))
        return RuntimeConfigSnapshot(
            plugins=PluginRuntimeConfig(
                hook_timeout_seconds=float(
                    cast(float | int, value("plugins.hook_timeout_seconds"))
                ),
            ),
            context=ContextRuntimeConfig(
                local_event_limit=int(cast(int, value("context.local_event_limit"))),
                compact_trigger_tokens=int(cast(int, value("context.compact_trigger_tokens"))),
            ),
            memory=MemoryRetrievalRuntimeConfig(
                retrieval_enabled=bool(value("memory.retrieval_enabled")),
                self_enabled=self._settings.self_memory_enabled,
                lexical_candidate_limit=int(cast(int, value("memory.lexical_candidate_limit"))),
                context_limit_per_entity=int(cast(int, value("memory.context_limit_per_entity"))),
                overview_limit_per_entity=int(cast(int, value("memory.overview_limit_per_entity"))),
                query_term_limit=int(cast(int, value("memory.query_term_limit"))),
                short_query_fallback_enabled=bool(value("memory.short_query_fallback_enabled")),
                semantic_enabled=bool(value("memory.semantic_enabled")),
                semantic_candidate_limit=int(cast(int, value("memory.semantic_candidate_limit"))),
                hybrid_lexical_weight=float(
                    cast(float | int, value("memory.hybrid_lexical_weight"))
                ),
                hybrid_semantic_weight=float(
                    cast(float | int, value("memory.hybrid_semantic_weight"))
                ),
                hybrid_rrf_k=int(cast(int, value("memory.hybrid_rrf_k"))),
                maintenance_enabled=bool(value("memory.maintenance_enabled")),
                maintenance_interval_seconds=float(
                    cast(float | int, value("memory.maintenance_interval_seconds"))
                ),
                maintenance_batch_limit=int(cast(int, value("memory.maintenance_batch_limit"))),
            ),
            reply=ReplyRuntimeConfig(
                delay_min_seconds=delay_min,
                delay_max_seconds=delay_max,
                max_qq_message_chars=int(cast(int, value("reply.max_qq_message_chars"))),
                hard_max_messages=int(cast(int, value("reply.hard_max_messages"))),
            ),
            llm=LLMRuntimeConfig(
                model=self._settings.llm_model,
            ),
            agent=AgentRuntimeConfig(
                max_tool_calls=int(cast(int, value("agent.max_tool_calls"))),
                max_model_requests=int(cast(int, value("agent.max_model_requests"))),
                tool_result_max_characters=int(
                    cast(int, value("agent.tool_result_max_characters"))
                ),
            ),
            tooling=ToolingRuntimeConfig(
                max_parallel_calls=int(cast(int, value("tooling.max_parallel_calls"))),
                result_token_budget=(
                    int(cast(int, value("tooling.result_token_budget")))
                    if value("tooling.result_token_budget") is not None
                    else None
                ),
                result_item_limit=(
                    int(cast(int, value("tooling.result_item_limit")))
                    if value("tooling.result_item_limit") is not None
                    else None
                ),
                result_artifact_enabled=bool(value("tooling.result_artifact_enabled")),
                result_artifact_retention_seconds=int(
                    cast(int, value("tooling.result_artifact_retention_seconds"))
                ),
            ),
            work_storage=WorkStorageRuntimeConfig(
                total_max_bytes=int(cast(int, value("storage.protocol_total_max_bytes"))),
                object_max_bytes=int(cast(int, value("storage.protocol_object_max_bytes"))),
                disk_reserve_bytes=int(cast(int, value("storage.protocol_disk_reserve_bytes"))),
            ),
            web=WebRuntimeConfig(
                mode=self._settings.web.mode.value,
                search_max_results=int(cast(int, value("web.search_max_results"))),
                source_retention_days=int(cast(int, value("web.source_retention_days"))),
            ),
            vision=VisionRuntimeConfig(
                max_images_per_turn=int(cast(int, value("vision.max_images_per_turn"))),
                max_frames_per_turn=int(cast(int, value("vision.max_frames_per_turn"))),
                gif_max_frames=int(cast(int, value("vision.gif_max_frames"))),
                video_max_duration_seconds=int(
                    cast(int, value("vision.video_max_duration_seconds"))
                ),
                video_sample_interval_seconds=int(
                    cast(int, value("vision.video_sample_interval_seconds"))
                ),
                video_max_frames=int(cast(int, value("vision.video_max_frames"))),
                video_max_download_bytes=int(cast(int, value("vision.video_max_download_bytes"))),
                thinking_enabled=bool(value("vision.thinking_enabled")),
                thinking_budget=int(cast(int, value("vision.thinking_budget"))),
                per_user_requests_per_minute=int(
                    cast(int, value("vision.per_user_requests_per_minute"))
                ),
                per_group_requests_per_minute=int(
                    cast(int, value("vision.per_group_requests_per_minute"))
                ),
                analysis_retention_days=int(cast(int, value("vision.analysis_retention_days"))),
            ),
            conversation=_conversation_policy(value),
        )

    def _resolve(
        self,
        spec: ConfigSpec,
        records: tuple[RuntimeConfigOverrideRecord, ...],
        *,
        user_id: str | None,
        group_id: str | None,
        honor_restart_activation: bool = True,
        person_id: str | None = None,
        space_id: str | None = None,
    ) -> EffectiveConfigValue:
        selected_records = records
        if honor_restart_activation and spec.apply_mode is ConfigApplyMode.RESTART_REQUIRED:
            selected_records = tuple(self._active_restart.values())
        valid_rows = [
            row
            for row in selected_records
            if row.config_key == spec.key and self._valid_stored_record(row)
        ]
        candidates = (
            (
                ConfigScopeType.USER,
                person_id if person_id is not None else user_id,
            ),
            (
                ConfigScopeType.GROUP,
                space_id if space_id is not None else group_id,
            ),
            (
                ConfigScopeType.GLOBAL,
                "",
            ),
        )
        for scope, scope_id in candidates:
            if scope_id is None or scope not in spec.allowed_scopes:
                continue
            row = self._canonical_row(
                valid_rows,
                scope=scope,
                person_id=person_id,
                space_id=space_id,
            )
            exposed_scope_id = ""
            if row is not None and scope is ConfigScopeType.USER:
                exposed_scope_id = row.canonical_person_id or ""
            elif row is not None and scope is ConfigScopeType.GROUP:
                exposed_scope_id = row.canonical_space_id or ""
            if row is not None:
                # Pending is computed process-wide by pending_restart_count; an activated
                # row itself is never pending for the value returned here.
                return EffectiveConfigValue(
                    key=spec.key,
                    value=row.value,
                    source=f"runtime:{scope.value}",
                    scope_type=scope,
                    scope_id=exposed_scope_id,
                    apply_mode=spec.apply_mode,
                    pending_restart=False,
                )
        source = (
            "env"
            if any(field in self._settings.model_fields_set for field in spec.settings_fields)
            else "default"
        )
        return EffectiveConfigValue(
            key=spec.key,
            value=spec.default_getter(self._settings),
            source=source,
            scope_type=None,
            scope_id="",
            apply_mode=spec.apply_mode,
        )

    def _valid_stored_record(self, row: RuntimeConfigOverrideRecord) -> bool:
        spec = self.registry.maybe_get(row.config_key)
        if (
            spec is None
            or not spec.mutable
            or row.scope_type not in spec.allowed_scopes
            or row.value_type != spec.value_type
            or row.apply_mode is not spec.apply_mode
        ):
            return False
        try:
            return self.registry.convert(spec, row.value) == row.value
        except ValueError:
            return False

    @staticmethod
    def _canonical_row(
        rows: list[RuntimeConfigOverrideRecord],
        *,
        scope: ConfigScopeType,
        person_id: str | None,
        space_id: str | None,
    ) -> RuntimeConfigOverrideRecord | None:
        if scope is ConfigScopeType.USER:
            matched = [
                row
                for row in rows
                if row.scope_type is ConfigScopeType.USER and row.canonical_person_id == person_id
            ]
        elif scope is ConfigScopeType.GROUP:
            matched = [
                row
                for row in rows
                if row.scope_type is ConfigScopeType.GROUP and row.canonical_space_id == space_id
            ]
        else:
            matched = [
                row
                for row in rows
                if row.scope_type is ConfigScopeType.GLOBAL
                and row.canonical_person_id is None
                and row.canonical_space_id is None
            ]
        if len(matched) > 1:
            raise CanonicalIdentityError("canonical_owner_mismatch")
        return matched[0] if matched else None

    async def _owner_match(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
        session: AsyncSession | None = None,
    ) -> tuple[str | None, str | None]:
        async with optional_session(self._database, session, write=False) as active:
            person_id = await current_person_id(active, user_id) if user_id else None
            space_id = await resolve_space_owner(active, group_id) if group_id else None
            return person_id, space_id

    async def _bind_write_scope(
        self,
        scope: ConfigScopeType,
        scope_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> tuple[str, str | None, str | None]:
        if scope is ConfigScopeType.GLOBAL:
            return "", None, None
        async with optional_session(self._database, session, write=False) as active:
            if scope is ConfigScopeType.USER:
                resolved = await current_person_id(active, scope_id)
                return resolved, resolved, None
            resolved_group = await resolve_space_owner(active, scope_id)
            return resolved_group, None, resolved_group

    def _validate_write(
        self,
        spec: ConfigSpec,
        scope_type: str,
        scope_id: str,
    ) -> tuple[ConfigScopeType, str]:
        if not spec.mutable:
            if spec.apply_mode is ConfigApplyMode.SECRET:
                raise PermissionError("凭证只能确认是否配置，不能读取或修改")
            raise PermissionError("该配置只能通过启动环境维护")
        try:
            scope = ConfigScopeType(scope_type.casefold())
        except ValueError as exc:
            raise ValueError("scope_type 必须是 global、group 或 user") from exc
        normalized_scope_id = scope_id.strip()
        if scope is ConfigScopeType.GLOBAL:
            if normalized_scope_id:
                raise ValueError("global 作用域的 scope_id 必须为空")
        elif not normalized_scope_id:
            raise ValueError("group/user 作用域必须提供 scope_id")
        if scope not in spec.allowed_scopes:
            allowed = "、".join(item.value for item in spec.allowed_scopes)
            raise ValueError(f"该配置只允许以下作用域：{allowed}")
        return scope, normalized_scope_id

    def _audit_ref(
        self,
        actor_user_id: str,
        *,
        trigger_message_id: str,
        conversation_key: str,
    ) -> ControlAuditRef:
        return ControlAuditRef(
            user_id=actor_user_id,
            trigger_message_id=trigger_message_id,
            conversation_key=conversation_key,
        )

    async def _same_config_actor(
        self,
        original: AdminOperationEvent,
        actor: ControlAuditRef,
        *,
        session: AsyncSession | None,
    ) -> bool:
        # An independent operator credential remains its original principal.
        # A Person actor is the human, including pre-principal canonical audits.
        if original.actor_principal_kind is None and actor.principal_kind == "control":
            return original.actor_user_id == actor.principal_id
        if "control" in {original.actor_principal_kind, actor.principal_kind}:
            return bool(
                original.actor_principal_kind == actor.principal_kind == "control"
                and original.actor_principal_id is not None
                and original.actor_principal_id == actor.principal_id
            )
        if original.actor_user_id == actor.user_id:
            return True
        async with optional_session(self._database, session, write=False) as active:
            return await same_person_id(
                active,
                original.actor_principal_id or original.actor_user_id,
                actor.principal_id or actor.user_id,
            )

    async def _matches_original_state(
        self,
        session: AsyncSession,
        current: RuntimeConfigOverrideRecord | None,
        state: object,
        *,
        original_scope_id: str,
        prepared: ConfigRollbackPreparation,
    ) -> bool:
        if not self._matches_state(current, state):
            return False
        if current is None:
            # A deleted source override has no revision to migrate to another scope.
            return (
                not original_scope_id
                or await current_person_id(session, original_scope_id) == original_scope_id
                if _state_value(state, "scope_type") == "user"
                else True
            )
        if current.scope_id == original_scope_id:
            return True
        if _state_value(state, "row_id") is not None:
            return current.id == _state_value(state, "row_id")
        if prepared.lineage_scope != current.scope_id:
            raise ConfigRollbackPreparationChanged()
        source = _rollback_lineage(current.scope_id, current.id, current.version).subquery()
        wanted = func.json_each(prepared.lineage_json).table_valued("value")
        rows = tuple(
            await session.execute(
                select(
                    source.c.id, source.c.after_json.is_(func.json_extract(wanted.c.value, "$[1]"))
                )
                .select_from(
                    source.outerjoin(
                        wanted, source.c.id == func.json_extract(wanted.c.value, "$[0]")
                    )
                )
                .order_by(source.c.id)
            )
        )
        if tuple(row[0] for row in rows) != prepared.lineage_ids or not all(row[1] for row in rows):
            raise ConfigRollbackPreparationChanged()
        return prepared.lineage_matches

    @staticmethod
    def _matches_state(
        current: RuntimeConfigOverrideRecord | None,
        state: object,
    ) -> bool:
        expected_exists = bool(_state_value(state, "override_exists"))
        if not expected_exists:
            return current is None
        if current is None:
            return False
        return (
            current.value == _state_value(state, "value")
            and current.version == _state_value(state, "version")
            and (
                _state_value(state, "row_id") is None or current.id == _state_value(state, "row_id")
            )
        )

    @staticmethod
    def _safe_scope(value: str) -> ConfigScopeType:
        try:
            return ConfigScopeType(value.casefold())
        except ValueError:
            return ConfigScopeType.GLOBAL

    @staticmethod
    def _error_category(exc: Exception) -> str:
        if isinstance(exc, KeyError):
            return "unknown_key"
        if isinstance(exc, PermissionError):
            return "permission_denied"
        if isinstance(exc, ValueError):
            return "validation_error"
        return type(exc).__name__[:64]

    @staticmethod
    def _apply_detail(
        mode: ConfigApplyMode,
        *,
        pending_restart: bool = True,
    ) -> str:
        if mode is ConfigApplyMode.HOT:
            return "已保存并立即生效"
        if mode is ConfigApplyMode.FUTURE_ONLY:
            return "已保存，只影响之后新建的记录或任务"
        if mode is ConfigApplyMode.RESTART_REQUIRED:
            return (
                "已保存，重启 Bot 后生效" if pending_restart else "已保存；有效值未变化，无需重启"
            )
        return "已保存"


def _conversation_policy(value: Callable[[str], ConfigValue]) -> ConversationRuntimeConfig:
    return ConversationRuntimeConfig(
        auto_reply_enabled=bool(value("conversation.auto_reply_enabled")),
        proactive_wakeup_enabled=bool(value("conversation.proactive_wakeup_enabled")),
        autonomous_enabled=bool(value("conversation.autonomous_enabled")),
        semantic_participation_enabled=bool(value("conversation.semantic_participation_enabled")),
        autonomous_debounce_seconds=float(
            cast(float | int, value("conversation.autonomous_debounce_seconds"))
        ),
        autonomous_admission_threshold=int(
            cast(int, value("conversation.autonomous_admission_threshold"))
        ),
        autonomous_batch_limit=int(cast(int, value("conversation.autonomous_batch_limit"))),
        autonomous_presence_window_seconds=int(
            cast(int, value("conversation.autonomous_presence_window_seconds"))
        ),
        interrupt_autonomous_on_new_message=bool(
            value("conversation.interrupt_autonomous_on_new_message")
        ),
    )
