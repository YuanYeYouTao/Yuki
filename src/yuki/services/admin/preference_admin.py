"""Unified person-preference administration."""

from __future__ import annotations

from yuki.admin.authority.principal import ControlPrincipal, PrincipalSource
from yuki.admin.authority.targets import PersonControlTarget
from yuki.admin.models import ControlAuditRef
from yuki.domain.control import DecisionContext
from yuki.memory.models import MemoryFact
from yuki.services.admin.control_auth import (
    person_storage_id,
    require_capability,
    require_self_or_capability,
)
from yuki.services.admin.memory_admin import MemoryAdminService, MemoryPreferenceTrigger

type PersonAdminContext = DecisionContext[ControlPrincipal, PrincipalSource, PersonControlTarget]


class PreferenceAdminService:
    """Manage a person's bounded interaction preferences."""

    def __init__(
        self,
        *,
        memory_mutations: MemoryAdminService,
    ) -> None:
        self._memory_mutations = memory_mutations

    async def list_preferences(
        self,
        context: PersonAdminContext,
        audit: ControlAuditRef,
    ) -> tuple[MemoryFact, ...]:
        require_self_or_capability(context, "control.preference.mutate", audit)
        require_capability(context, "control.preference.read")
        target = person_storage_id(context)
        return await self._memory_mutations.list_explicit_preferences(
            _preference_trigger(context, audit), target, target_person_id=target
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
        matching = [
            row
            for row in await self._memory_mutations.list_explicit_preferences(
                _preference_trigger(context, audit), target, target_person_id=target
            )
            if row.key == normalized_key
        ]
        if len(matching) > 1:
            raise ValueError("同一偏好键存在多条记忆，请按事实 ID 指定要修改的记录")
        existing = matching[0] if matching else None
        row = await self._memory_mutations.set_explicit_preference(
            _preference_trigger(context, audit),
            target,
            normalized_key,
            normalized_value,
            existing=existing,
            target_person_id=target,
        )
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
        matching = [
            row
            for row in await self._memory_mutations.list_explicit_preferences(
                _preference_trigger(context, audit), target, target_person_id=target
            )
            if row.key == normalized_key
        ]
        if len(matching) > 1:
            raise ValueError("同一偏好键存在多条记忆，请按事实 ID 指定要修改的记录")
        existing = matching[0] if matching else None
        deleted = bool(
            existing is not None
            and await self._memory_mutations.delete_explicit_preference(
                _preference_trigger(context, audit),
                target,
                existing,
                target_person_id=target,
            )
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
        trigger_event_id=audit.trigger_event_id,
        canonical_conversation_id=audit.canonical_conversation_id,
        ingress_presence_id=audit.ingress_presence_id,
        conversation_key=audit.conversation_key,
        decision_actor_type=audit.decision_actor_type,
        decision_actor_id=audit.decision_actor_id,
        actor_is_superuser="superuser" in principal.roles,
        person_id=(
            principal.principal_id.text
            if principal.source in {PrincipalSource.PLATFORM, PrincipalSource.QQ}
            else principal.person_id.text
            if principal.person_id
            else None
        ),
    )
