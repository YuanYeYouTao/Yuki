"""Resolve ControlPrincipal and canonical admin targets at the adapter boundary.

SUPERUSERS is read only here. Core relationship/group/preference/private-access
services never see AdminActor or Settings.superusers. Missing Bindings fail
closed; this module never invents Person or Space identities.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import overload

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.models import AdminActor, ControlAuditRef
from qq_ai_bot.control_plane.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.control_plane.targets import PersonControlTarget, SpaceControlTarget
from qq_ai_bot.domain.control import DecisionContext
from qq_ai_bot.domain.identity import PersonId, PrincipalId, RequestId, SpaceId
from qq_ai_bot.identity.canonical_repository import IDENTITY_PLATFORM
from qq_ai_bot.identity.db_models import IdentityBindingModel, SpaceBindingModel
from qq_ai_bot.persistence.database import Database

USER_CAPABILITIES: frozenset[str] = frozenset(
    {
        "control.relationship.read",
        "control.preference.read",
    }
)
SUPERUSER_CAPABILITIES: frozenset[str] = USER_CAPABILITIES | frozenset(
    {
        "control.relationship.mutate",
        "control.preference.mutate",
        "control.group.mutate",
        "control.private_access.mutate",
        "control.config.read",
        "control.config.mutate",
        "control.audit.read",
        "control.health.read",
        "control.system.read",
        "control.memory.metadata.read",
        "control.memory.content.read",
        "control.memory.mutate",
        "control.memory.rebuild",
        "control.memory.dream",
        "control.memory.maintenance",
        "control.automation.read",
        "control.automation.mutate",
        "control.plugin.read",
        "control.plugin.mutate",
        "control.mcp.read",
        "control.mcp.mutate",
        "control.emoji.read",
        "control.emoji.mutate",
        "control.speech.read",
        "control.speech.mutate",
        "control.operation.read",
        "control.operation.cancel",
        "control.operation.retry",
    }
)
PRINCIPAL_NOT_FOUND = "没有找到可用的控制主体。"


class ControlPrincipalLookupError(PermissionError):
    """Raised when a QQ account has no canonical Binding/Person."""

    def __init__(self) -> None:
        super().__init__(PRINCIPAL_NOT_FOUND)


class ControlAccess:
    """Adapter-only identity resolution for QQ-originated control calls."""

    def __init__(self, database: Database, *, superuser_ids: Iterable[str]) -> None:
        self._database = database
        self._superuser_ids = frozenset(str(item) for item in superuser_ids)

    async def principal_for_qq(self, user_id: str) -> ControlPrincipal:
        """Resolve one QQ account to a ControlPrincipal. Fail closed if unbound."""

        token = user_id.strip()
        if not token:
            raise ControlPrincipalLookupError()
        async with self._database.sessions() as session:
            binding = await _identity_binding(session, token)
            if binding is None or binding.status != "active":
                raise ControlPrincipalLookupError()
            person_id = PersonId.parse(binding.person_id)
        privileged = token in self._superuser_ids
        return ControlPrincipal(
            principal_id=PrincipalId.parse(person_id.text),
            person_id=person_id,
            source=PrincipalSource.QQ,
            roles=("superuser",) if privileged else (),
            granted_capabilities=SUPERUSER_CAPABILITIES if privileged else USER_CAPABILITIES,
            authenticated=True,
            active=True,
        )

    async def person_target(self, user_id: str) -> PersonControlTarget:
        """Build a person target from a proven storage id. Do not invent Person."""

        token = user_id.strip()
        if not token:
            raise ValueError("storage_user_id is required")
        async with self._database.sessions() as session:
            binding = await _identity_binding(session, token)
        person_id = PersonId.parse(binding.person_id) if binding is not None else None
        return PersonControlTarget(
            person_id=person_id,
            storage_user_id=token,
            lockout_protected=token in self._superuser_ids,
        )

    async def space_target(self, group_id: str) -> SpaceControlTarget:
        """Build a space target from a proven group storage id. Do not invent Space."""

        token = group_id.strip()
        if not token:
            raise ValueError("storage_group_id is required")
        async with self._database.sessions() as session:
            binding = await _space_binding(session, token)
        space_id = SpaceId.parse(binding.space_id) if binding is not None else None
        return SpaceControlTarget(space_id=space_id, storage_group_id=token)

    @overload
    @staticmethod
    def context(
        principal: ControlPrincipal,
        target: PersonControlTarget,
        *,
        request_id: RequestId | None = None,
    ) -> DecisionContext[ControlPrincipal, PrincipalSource, PersonControlTarget]: ...

    @overload
    @staticmethod
    def context(
        principal: ControlPrincipal,
        target: SpaceControlTarget,
        *,
        request_id: RequestId | None = None,
    ) -> DecisionContext[ControlPrincipal, PrincipalSource, SpaceControlTarget]: ...

    @staticmethod
    def context(
        principal: ControlPrincipal,
        target: PersonControlTarget | SpaceControlTarget,
        *,
        request_id: RequestId | None = None,
    ) -> DecisionContext[
        ControlPrincipal, PrincipalSource, PersonControlTarget | SpaceControlTarget
    ]:
        return DecisionContext(
            request_id=request_id or RequestId.new(),
            principal=principal,
            source=principal.source,
            canonical_target=target,
        )


def audit_ref_from_actor(actor: AdminActor) -> ControlAuditRef:
    """Copy already-proven QQ correlation fields without forging authority."""

    return ControlAuditRef(
        user_id=actor.user_id,
        trigger_message_id=actor.trigger_message_id,
        trigger_event_id=actor.trigger_event_id,
        canonical_conversation_id=actor.canonical_conversation_id,
        ingress_presence_id=actor.ingress_presence_id,
        conversation_key=actor.conversation_key,
        bot_user_id=actor.bot_user_id,
        decision_actor_type=actor.decision_actor_type,
        decision_actor_id=actor.decision_actor_id,
    )


async def _identity_binding(session: AsyncSession, external_id: str) -> IdentityBindingModel | None:
    row = await session.scalar(
        select(IdentityBindingModel).where(
            IdentityBindingModel.platform == IDENTITY_PLATFORM,
            IdentityBindingModel.external_account_id == external_id,
        )
    )
    return row if isinstance(row, IdentityBindingModel) else None


async def _space_binding(session: AsyncSession, group_id: str) -> SpaceBindingModel | None:
    row = await session.scalar(
        select(SpaceBindingModel).where(
            SpaceBindingModel.platform == IDENTITY_PLATFORM,
            SpaceBindingModel.external_space_id == group_id,
        )
    )
    return row if isinstance(row, SpaceBindingModel) else None
