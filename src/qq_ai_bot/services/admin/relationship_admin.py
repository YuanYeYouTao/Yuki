"""Unified relationship administration for commands and natural-language tools."""

from __future__ import annotations

import time

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import ControlAuditRef
from qq_ai_bot.control_plane.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.control_plane.targets import PersonControlTarget
from qq_ai_bot.domain.control import DecisionContext
from qq_ai_bot.domain.relationships import RelationshipSnapshot
from qq_ai_bot.persistence.repositories import (
    RelationshipEventRecord,
    RelationshipRepository,
)
from qq_ai_bot.services.admin.control_auth import (
    is_self,
    person_storage_id,
    require_capability,
    require_self_or_capability,
)

type PersonAdminContext = DecisionContext[ControlPrincipal, PrincipalSource, PersonControlTarget]


class RelationshipAdminService:
    """Read relationship state and perform explicitly authorized manual changes."""

    def __init__(
        self,
        *,
        relationships: RelationshipRepository,
        audit: AdminAuditService,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self._relationships = relationships
        self._audit = audit
        self._runtime_config = runtime_config

    async def get_relationship(
        self,
        context: PersonAdminContext,
        audit: ControlAuditRef,
    ) -> RelationshipSnapshot:
        require_capability(context, "control.relationship.read")
        target = person_storage_id(context)
        existing = await self._relationships.get(target)
        if existing is not None:
            return existing
        if is_self(context, audit):
            return await self._get_or_create(target)
        raise ValueError("没有找到该人物的好感度记录")

    async def set_affection(
        self,
        context: PersonAdminContext,
        value: int,
        *,
        audit: ControlAuditRef,
    ) -> tuple[RelationshipSnapshot, RelationshipSnapshot]:
        require_capability(context, "control.relationship.mutate")
        target = person_storage_id(context)
        started = time.perf_counter()
        try:
            async with self._audit.transaction() as session:
                before = await self._get_or_create(target, session=session)
                after = await self._relationships.set_affection(
                    user_id=target,
                    actor_user_id=audit.user_id,
                    score=value,
                    session=session,
                )
                await self._record_change(
                    audit,
                    "set_affection",
                    target,
                    before,
                    after,
                    started,
                    session=session,
                )
        except Exception as exc:
            await self._record_failure(
                audit,
                "set_affection",
                target,
                {"requested_affection": value},
                exc,
                started,
            )
            raise
        return before, after

    async def adjust_affection(
        self,
        context: PersonAdminContext,
        delta: int,
        *,
        audit: ControlAuditRef,
    ) -> tuple[RelationshipSnapshot, RelationshipSnapshot]:
        require_capability(context, "control.relationship.mutate")
        target = person_storage_id(context)
        started = time.perf_counter()
        try:
            async with self._audit.transaction() as session:
                before = await self._get_or_create(target, session=session)
                after = await self._relationships.adjust_affection(
                    user_id=target,
                    actor_user_id=audit.user_id,
                    delta=delta,
                    session=session,
                )
                await self._record_change(
                    audit,
                    "adjust_affection",
                    target,
                    before,
                    after,
                    started,
                    session=session,
                )
        except Exception as exc:
            await self._record_failure(
                audit,
                "adjust_affection",
                target,
                {"requested_delta": delta},
                exc,
                started,
            )
            raise
        return before, after

    async def set_trust(
        self,
        context: PersonAdminContext,
        value: int,
        *,
        audit: ControlAuditRef,
    ) -> tuple[RelationshipSnapshot, RelationshipSnapshot]:
        require_capability(context, "control.relationship.mutate")
        target = person_storage_id(context)
        started = time.perf_counter()
        try:
            async with self._audit.transaction() as session:
                before = await self._get_or_create(target, session=session)
                after = await self._relationships.set_trust(
                    user_id=target,
                    actor_user_id=audit.user_id,
                    score=value,
                    session=session,
                )
                await self._record_change(
                    audit,
                    "set_trust",
                    target,
                    before,
                    after,
                    started,
                    session=session,
                )
        except Exception as exc:
            await self._record_failure(
                audit,
                "set_trust",
                target,
                {"requested_trust": value},
                exc,
                started,
            )
            raise
        return before, after

    async def get_history(
        self,
        context: PersonAdminContext,
        audit: ControlAuditRef,
        *,
        limit: int = 10,
    ) -> tuple[RelationshipEventRecord, ...]:
        require_self_or_capability(context, "control.relationship.mutate", audit)
        return await self._relationships.history(person_storage_id(context), limit=limit)

    async def _record_change(
        self,
        audit: ControlAuditRef,
        operation: str,
        target: str,
        before: RelationshipSnapshot,
        after: RelationshipSnapshot,
        started: float,
        *,
        session: AsyncSession,
    ) -> None:
        await self._audit.record(
            actor=audit,
            capability="relationship",
            operation=operation,
            target_type="user",
            target_id=target,
            before={
                "affection": before.affection_score,
                "trust": before.trust_score,
            },
            after={
                "affection": after.affection_score,
                "trust": after.trust_score,
            },
            success=True,
            duration_seconds=time.perf_counter() - started,
            session=session,
        )

    async def _record_failure(
        self,
        audit: ControlAuditRef,
        operation: str,
        target: str,
        requested: object,
        error: Exception,
        started: float,
    ) -> None:
        try:
            await self._audit.record(
                actor=audit,
                capability="relationship",
                operation=operation,
                target_type="user",
                target_id=target,
                before=None,
                after=requested,
                success=False,
                error_category=type(error).__name__,
                duration_seconds=time.perf_counter() - started,
            )
        except Exception:
            # Preserve the original mutation/audit failure; no business change committed.
            pass

    async def _get_or_create(
        self,
        target: str,
        *,
        session: AsyncSession | None = None,
    ) -> RelationshipSnapshot:
        if self._runtime_config is None:
            return await self._relationships.get_or_create(target, session=session)
        runtime = await self._runtime_config.snapshot(user_id=target)
        return await self._relationships.get_or_create(
            target,
            initial_affection=runtime.relationship.initial_affection,
            initial_trust=runtime.relationship.initial_trust,
            session=session,
        )
