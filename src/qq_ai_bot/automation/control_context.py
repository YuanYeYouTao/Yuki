"""Resolve management authority from existing canonical owners and routes."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.automation.authority import PermissionLevel, permission_for_accounts
from qq_ai_bot.automation.validator import CreationProvenance
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    PersonActiveRouteModel,
    SpaceActiveRouteModel,
)
from qq_ai_bot.domain.identity import ConversationId, PersonId
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)


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
) -> ControlAutomationContext:
    """An operator never supplies transport identity or gains owner privileges."""
    person = None
    if owner_id != "self":
        PersonId.parse(owner_id)
        person = await session.get(CanonicalPersonModel, owner_id)
        if person is None or not person.enabled:
            raise PermissionError("automation_owner_unavailable")
    if conversation_id is None:
        if person is None:
            raise ValueError("self_automation_scene_required")
        conversation = await session.scalar(
            select(CanonicalConversationModel).where(
                CanonicalConversationModel.person_id == owner_id,
                CanonicalConversationModel.kind == "private",
            )
        )
    else:
        ConversationId.parse(conversation_id)
        conversation = await session.get(CanonicalConversationModel, conversation_id)
    if conversation is None:
        raise PermissionError("automation_scene_unavailable")
    group_id = None
    owner_binding = None
    if conversation.kind == "private":
        if person is None or conversation.person_id != owner_id:
            raise PermissionError("automation_private_scene_owner_mismatch")
        person_route = await session.get(PersonActiveRouteModel, owner_id)
        if person_route is None or person_route.paused:
            raise PermissionError("automation_route_unavailable")
        owner_binding = await session.get(IdentityBindingModel, person_route.identity_binding_id)
        presence_id = person_route.presence_id
    else:
        space = await session.get(CanonicalSpaceModel, conversation.space_id)
        space_route = await session.get(SpaceActiveRouteModel, conversation.space_id)
        if space is None or not space.enabled or space_route is None or space_route.paused:
            raise PermissionError("automation_route_unavailable")
        space_binding = await session.get(SpaceBindingModel, space_route.space_binding_id)
        if (
            space_binding is None
            or space_binding.status != "active"
            or space_binding.platform != "qq"
            or space_binding.space_id != conversation.space_id
        ):
            raise PermissionError("automation_route_changed")
        group_id = space_binding.external_space_id
        presence_id = space_route.presence_id
    presence = await session.get(PresenceModel, presence_id)
    if presence is None or not presence.enabled or presence.platform != "qq":
        raise PermissionError("automation_presence_unavailable")
    accounts = (
        (
            await session.scalars(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.person_id == owner_id,
                    IdentityBindingModel.platform == "qq",
                    IdentityBindingModel.status == "active",
                )
            )
        ).all()
        if person is not None
        else []
    )
    if person is not None:
        if owner_binding is None:
            route = await session.get(PersonActiveRouteModel, owner_id)
            if route is not None and not route.paused:
                owner_binding = await session.get(IdentityBindingModel, route.identity_binding_id)
            elif len(accounts) == 1:
                owner_binding = accounts[0]
        if (
            owner_binding is None
            or owner_binding.person_id != owner_id
            or owner_binding.status != "active"
            or owner_binding.platform != presence.platform
        ):
            raise PermissionError("automation_owner_route_ambiguous")
    permission = (
        permission_for_accounts(settings, (binding.external_account_id for binding in accounts))
        if person is not None
        else PermissionLevel.SELF
    )
    return ControlAutomationContext(
        owner_id=owner_id,
        provenance=CreationProvenance(
            creator_user_id=owner_binding.external_account_id if owner_binding else "",
            bot_user_id=presence.external_account_id,
            message_id="",
            original_text="",
            current_group_id=group_id,
            mentioned_user_ids=(),
            permission=permission,
        ),
        conversation_id=conversation.id,
        generation=conversation.generation,
        presence_id=presence.id,
        space_id=conversation.space_id,
    )
