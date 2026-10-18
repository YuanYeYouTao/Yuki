"""Resolve canonical control actors and targets; QQ recovery has an explicit boundary."""

from __future__ import annotations

from typing import overload

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.authority.principal import ControlPrincipal, PrincipalSource
from yuki.admin.authority.targets import PersonControlTarget, SpaceControlTarget
from yuki.admin.models import AdminActor, ControlAuditRef
from yuki.domain.control import DecisionContext
from yuki.domain.identity import PersonId, PrincipalId, RequestId, SpaceId
from yuki.identity.authority import person_is_superuser
from yuki.identity.canonical_repository import IDENTITY_PLATFORM
from yuki.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from yuki.identity.person_identity import current_person_id
from yuki.persistence.database import Database

USER_CAPABILITIES: frozenset[str] = frozenset(
    {
        "control.preference.read",
    }
)
SUPERUSER_CAPABILITIES: frozenset[str] = USER_CAPABILITIES | frozenset(
    {
        "control.preference.mutate",
        "control.group.mutate",
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
        "control.emoji.read",
        "control.emoji.mutate",
        "control.operation.read",
        "control.operation.cancel",
        "control.operation.retry",
        "control.plugin.config.content.read",
        "control.plugin.config.mutate",
        "control.config.file.content.read",
        "control.config.file.mutate",
        "control.execution.metadata.read",
        "control.execution.content.read",
        "control.work.mutate",
        "control.work.read",
        "conversation.metadata.read",
        "identity.person.create",
        "identity.person.read",
        "identity.person.enable",
        "identity.person.disable",
        "identity.person.superuser",
        "identity.person.merge",
        "identity.person.rename",
        "identity.person.forget",
        "identity.binding.attach",
        "identity.binding.read",
        "identity.binding.read_external",
        "identity.binding.enable",
        "identity.binding.disable",
        "identity.binding.transfer",
        "identity.client.pair",
        "identity.client.revoke",
        "identity.presence.read",
        "identity.presence.start",
        "identity.presence.stop",
        "identity.presence.set_ingest",
    }
)
PRINCIPAL_NOT_FOUND = "没有找到可用的控制主体。"


class ControlPrincipalLookupError(PermissionError):
    """Raised when the authenticated source has no active canonical owner."""

    def __init__(self) -> None:
        super().__init__(PRINCIPAL_NOT_FOUND)


class ControlAccess:
    """Control authorization uses already-resolved Person and Space identities."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def principal_for_person(self, person_id: str | None) -> ControlPrincipal:
        async with self._database.sessions() as session:
            person = await session.get(CanonicalPersonModel, person_id) if person_id else None
            if person is None or not person.enabled:
                raise ControlPrincipalLookupError()
            original_person_id = person.id
            person = await session.get(
                CanonicalPersonModel, await current_person_id(session, person.id)
            )
            assert person is not None
            if not person.enabled:
                raise ControlPrincipalLookupError()
            privileged = await person_is_superuser(session, original_person_id)
        return self._principal(
            PersonId.parse(person.id),
            privileged,
            PrincipalSource.PLATFORM,
            source_person_id=PersonId.parse(original_person_id),
        )

    @staticmethod
    def _principal(
        person_id: PersonId,
        privileged: bool,
        source: PrincipalSource,
        *,
        source_person_id: PersonId | None = None,
    ) -> ControlPrincipal:
        return ControlPrincipal(
            principal_id=PrincipalId.parse((source_person_id or person_id).text),
            person_id=person_id,
            source=source,
            roles=("superuser",) if privileged else (),
            granted_capabilities=SUPERUSER_CAPABILITIES if privileged else USER_CAPABILITIES,
            authenticated=True,
            active=True,
        )

    async def qq_recovery_principal(self, user_id: str) -> ControlPrincipal:
        """Resolve the real QQ sender only for the pre-ingress group recovery lane."""

        token = user_id.strip()
        if not token:
            raise ControlPrincipalLookupError()
        async with self._database.sessions() as session:
            binding = await _identity_binding(session, token)
            if binding is None or binding.status != "active":
                raise ControlPrincipalLookupError()
            original = await session.get(CanonicalPersonModel, binding.person_id)
            if original is None or not original.enabled:
                raise ControlPrincipalLookupError()
            current = await session.get(
                CanonicalPersonModel, await current_person_id(session, original.id)
            )
            assert current is not None
            if not current.enabled:
                raise ControlPrincipalLookupError()
            person_id = PersonId.parse(current.id)
            privileged = await person_is_superuser(session, person_id.text)
        return self._principal(
            person_id,
            privileged,
            PrincipalSource.QQ,
            source_person_id=PersonId.parse(original.id),
        )

    async def person_target(self, person_id: str) -> PersonControlTarget:
        async with self._database.sessions() as session:
            person = await session.get(CanonicalPersonModel, person_id)
            if person is None:
                raise ValueError("没有找到该 Person。")
            person = await session.get(
                CanonicalPersonModel, await current_person_id(session, person.id)
            )
            assert person is not None
        return PersonControlTarget(
            person_id=PersonId.parse(person.id),
            storage_user_id=person.id,
        )

    async def space_target(self, space_id: str) -> SpaceControlTarget:
        async with self._database.sessions() as session:
            space = await session.get(CanonicalSpaceModel, space_id)
            if space is None:
                raise ValueError("没有找到该 Space。")
        return SpaceControlTarget(space_id=SpaceId.parse(space.id), storage_group_id=space.id)

    async def qq_recovery_space_target(self, group_id: str) -> SpaceControlTarget:
        """The recovery transport may target a QQ group before a Space is bound."""

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
    """Copy external correlation fields without using them as authority."""

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
            IdentityBindingModel.status != "transferred",
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
