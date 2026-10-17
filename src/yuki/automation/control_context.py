"""Resolve task authority from its permanent owner and original execution scene."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from yuki.automation.authority import PermissionLevel
from yuki.config import Settings
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    PersonActiveRouteModel,
    SpaceActiveRouteModel,
)
from yuki.conversation.hydrate import conversation_for_binding
from yuki.domain.identity import ConversationId, PersonId
from yuki.identity.authority import person_is_superuser
from yuki.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.person_identity import current_person_id, same_person_id


@dataclass(frozen=True, slots=True)
class CreationProvenance:
    creator_user_id: str
    bot_user_id: str
    current_group_id: str | None
    permission: PermissionLevel


@dataclass(frozen=True, slots=True)
class ControlAutomationContext:
    owner_id: str
    provenance: CreationProvenance
    conversation_id: str
    generation: int
    presence_id: str
    space_id: str | None


async def resolve_control_context(
    session: AsyncSession,
    settings: Settings,
    *,
    owner_id: str,
    conversation_id: str | None,
    presence_id: str | None = None,
) -> ControlAutomationContext:
    """An operator chooses a canonical scene, never an impersonated account."""
    if owner_id != "self":
        PersonId.parse(owner_id)
        person = await session.get(CanonicalPersonModel, owner_id)
        if person is None or not person.enabled:
            raise PermissionError("automation_owner_unavailable")
    if conversation_id is None:
        route = await session.get(PersonActiveRouteModel, owner_id) if owner_id != "self" else None
        conversation = (
            await conversation_for_binding(
                session, route.identity_binding_id, presence_id=route.presence_id
            )
            if route
            else None
        )
    else:
        ConversationId.parse(conversation_id)
        conversation = await session.get(CanonicalConversationModel, conversation_id)
    if conversation is None:
        raise PermissionError("automation_scene_unavailable")
    group_id = None
    binding = None
    if conversation.kind == "private":
        if (
            owner_id == "self"
            or not conversation.person_id
            or not await same_person_id(session, conversation.person_id, owner_id)
        ):
            raise PermissionError("automation_private_scene_owner_mismatch")
        route = await session.get(PersonActiveRouteModel, owner_id)
        presence_id = (
            presence_id or conversation.entry_presence_id or (route.presence_id if route else None)
        )
    else:
        space = await session.get(CanonicalSpaceModel, conversation.space_id)
        space_route = await session.get(SpaceActiveRouteModel, conversation.space_id)
        binding_id = conversation.space_binding_id or (
            space_route.space_binding_id if space_route else None
        )
        binding = await session.get(SpaceBindingModel, binding_id) if binding_id else None
        if (
            space is None
            or not space.enabled
            or binding is None
            or binding.space_id != conversation.space_id
        ):
            raise PermissionError("automation_route_unavailable")
        group_id = binding.external_space_id
        presence_id = presence_id or (space_route.presence_id if space_route else None)
    presence = await session.get(PresenceModel, presence_id) if presence_id else None
    if presence is None:
        raise PermissionError("automation_presence_unavailable")
    if binding is not None and binding.platform != presence.platform:
        raise PermissionError("automation_route_changed")
    account, permission = (
        await resolve_execution_identity(
            session,
            settings,
            owner_id=owner_id,
            conversation_id=conversation.id,
            presence_id=presence.id,
        )
        if owner_id != "self"
        else ("", PermissionLevel.SELF)
    )
    return ControlAutomationContext(
        owner_id=owner_id,
        provenance=CreationProvenance(account, presence.external_account_id, group_id, permission),
        conversation_id=conversation.id,
        generation=conversation.generation,
        presence_id=presence.id,
        space_id=conversation.space_id,
    )


async def resolve_execution_identity(
    session: AsyncSession,
    settings: Settings,
    *,
    owner_id: str,
    conversation_id: str | None = None,
    presence_id: str | None = None,
) -> tuple[str, PermissionLevel]:
    """Project the permanent owner within the original entry, not its latest send route."""
    person = await session.get(CanonicalPersonModel, owner_id)
    if person is None or not person.enabled:
        raise PermissionError("automation_owner_unavailable")
    conversation = (
        await session.get(CanonicalConversationModel, conversation_id) if conversation_id else None
    )
    root_id = await current_person_id(session, owner_id)
    root = await session.get(CanonicalPersonModel, root_id)
    if root is None or not root.enabled:
        raise PermissionError("automation_owner_unavailable")
    account = ""
    if conversation is not None and conversation.kind == "private":
        binding = await session.get(IdentityBindingModel, conversation.identity_binding_id)
        if binding is not None:
            account = binding.external_account_id
    permission = (
        PermissionLevel.SUPERUSER
        if await person_is_superuser(session, owner_id, settings.superusers)
        else PermissionLevel.USER
    )
    return account, permission
