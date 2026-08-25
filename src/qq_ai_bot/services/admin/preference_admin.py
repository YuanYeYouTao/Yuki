"""Unified person-preference administration."""

from __future__ import annotations

import time

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.models import ControlAuditRef
from qq_ai_bot.config import Settings
from qq_ai_bot.control_plane.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.control_plane.targets import PersonControlTarget
from qq_ai_bot.domain.control import DecisionContext
from qq_ai_bot.memory.models import MemoryFact
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.services.admin.control_auth import (
    person_storage_id,
    require_capability,
    require_self_or_capability,
)
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService, MemoryPreferenceTrigger

type PersonAdminContext = DecisionContext[ControlPrincipal, PrincipalSource, PersonControlTarget]


class PreferenceAdminService:
    """Manage a person's bounded interaction preferences."""

    def __init__(
        self,
        *,
        settings: Settings,
        memories: MemoryFactService,
        audit: AdminAuditService,
        memory_mutations: MemoryAdminService | None = None,
    ) -> None:
        self._settings = settings
        self._memories = memories
        self._audit = audit
        self._memory_mutations = memory_mutations

    async def list_preferences(
        self,
        context: PersonAdminContext,
        audit: ControlAuditRef,
    ) -> tuple[MemoryFact, ...]:
        require_self_or_capability(context, "control.preference.mutate", audit)
        require_capability(context, "control.preference.read")
        return await self._memories.list_preferences(
            person_storage_id(context),
            limit=self._settings.preference_max_entries,
        )

    async def set_preference(
        self,
        context: PersonAdminContext,
        key: str,
        value: str,
        *,
        audit: ControlAuditRef,
    ) -> MemoryFact:
        require_self_or_capability(context, "control.preference.mutate", audit)
        target = person_storage_id(context)
        normalized_key = key.strip()
        normalized_value = " ".join(value.split()).strip()
        if not normalized_key or not normalized_value:
            raise ValueError("偏好键和值不能为空")
        started = time.perf_counter()
        if self._memory_mutations is not None:
            existing = {
                row.key: row
                for row in await self._memories.list_preferences(
                    target,
                    limit=self._settings.preference_max_entries,
                )
            }.get(normalized_key)
            row = await self._memory_mutations.set_explicit_preference(
                _preference_trigger(context, audit),
                target,
                normalized_key,
                normalized_value,
                existing=existing,
            )
            await self._audit.record(
                actor=audit,
                capability="preference",
                operation="set",
                target_type="user",
                target_id=target,
                before={
                    "key": normalized_key,
                    "preference_value": existing.value if existing else None,
                },
                after={"key": normalized_key, "preference_value": normalized_value},
                success=True,
                duration_seconds=time.perf_counter() - started,
            )
            return row
        async with self._audit.transaction() as session:
            existing = {
                row.key: row
                for row in await self._memories.list_preferences(
                    target,
                    limit=self._settings.preference_max_entries,
                    session=session,
                )
            }.get(normalized_key)
            row = await self._memories.set_preference(
                target,
                normalized_key,
                normalized_value,
                limit=self._settings.preference_max_entries,
                session=session,
            )
            await self._audit.record(
                actor=audit,
                capability="preference",
                operation="set",
                target_type="user",
                target_id=target,
                before={
                    "key": normalized_key,
                    "preference_value": existing.value if existing else None,
                },
                after={"key": normalized_key, "preference_value": normalized_value},
                success=True,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        await self._memories.schedule_embedding(row.id)
        return row

    async def delete_preference(
        self,
        context: PersonAdminContext,
        key: str,
        *,
        audit: ControlAuditRef,
    ) -> bool:
        require_self_or_capability(context, "control.preference.mutate", audit)
        target = person_storage_id(context)
        normalized_key = key.strip()
        started = time.perf_counter()
        if self._memory_mutations is not None:
            existing = {
                row.key: row
                for row in await self._memories.list_preferences(
                    target,
                    limit=self._settings.preference_max_entries,
                )
            }.get(normalized_key)
            deleted = bool(
                existing is not None
                and await self._memory_mutations.delete_explicit_preference(
                    _preference_trigger(context, audit),
                    target,
                    existing,
                )
            )
            await self._audit.record(
                actor=audit,
                capability="preference",
                operation="delete",
                target_type="user",
                target_id=target,
                before={
                    "key": normalized_key,
                    "preference_value": existing.value if existing else None,
                },
                after=None,
                success=deleted,
                error_category=None if deleted else "not_found",
                duration_seconds=time.perf_counter() - started,
            )
            return deleted
        async with self._audit.transaction() as session:
            existing = {
                row.key: row
                for row in await self._memories.list_preferences(
                    target,
                    limit=self._settings.preference_max_entries,
                    session=session,
                )
            }.get(normalized_key)
            deleted = await self._memories.delete_preference(
                target,
                normalized_key,
                session=session,
            )
            await self._audit.record(
                actor=audit,
                capability="preference",
                operation="delete",
                target_type="user",
                target_id=target,
                before={
                    "key": normalized_key,
                    "preference_value": existing.value if existing else None,
                },
                after=None,
                success=deleted,
                error_category=None if deleted else "not_found",
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        return deleted


def _preference_trigger(
    context: PersonAdminContext,
    audit: ControlAuditRef,
) -> MemoryPreferenceTrigger:
    principal = context.principal
    return MemoryPreferenceTrigger(
        user_id=audit.user_id,
        bot_user_id=audit.bot_user_id,
        trigger_message_id=audit.trigger_message_id,
        conversation_key=audit.conversation_key,
        decision_actor_type=audit.decision_actor_type,
        decision_actor_id=audit.decision_actor_id,
        actor_is_superuser="superuser" in principal.roles,
    )
