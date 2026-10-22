"""Actual conversation sources shared by history reads and entry discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from yuki.capabilities.invocation import current_invocation
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.db_models import (
    AccountModel,
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.person_identity import person_enabled_condition, person_family_condition
from yuki.persistence.models import ChatEventModel
from yuki.services.history_query import HistorySource, HistoryTarget
from yuki.social.db_models import SocialOperationModel
from yuki.social.models import SocialError

if TYPE_CHECKING:
    from yuki.social.service import SocialContext


def history_delivery_predicates(acp_conversation_ids: tuple[str, ...]) -> tuple[Any, ...]:
    """ACP outbound rows are chat facts only after their actual send succeeded."""
    if not acp_conversation_ids:
        return ()
    return (
        or_(
            ChatEventModel.canonical_conversation_id.not_in(acp_conversation_ids),
            ChatEventModel.direction == "inbound",
            select(SocialOperationModel.id)
            .where(
                SocialOperationModel.event_id == ChatEventModel.id,
                SocialOperationModel.status == "succeeded",
                ChatEventModel.direction == "outbound",
            )
            .exists(),
        ),
    )


async def history_source_statement(
    session: AsyncSession,
    *,
    context: SocialContext,
    target: HistoryTarget | None = None,
    source: HistorySource | None = None,
    runtime: Any = None,
    conversation_id: str | None = None,
) -> Select[Any]:
    """Select original entries; current sendability does not decide historical ownership."""
    invocation = current_invocation.get()
    runtime = runtime if runtime is not None else invocation.runtime if invocation else None
    if await session.get(CanonicalConversationModel, context.conversation_id) is None:
        raise SocialError("history_unavailable")
    actor_id = getattr(context.actor, "person_id", None)
    if actor_id is not None and not await session.scalar(
        select(person_enabled_condition(actor_id))
    ):
        raise SocialError("permission_denied")

    entry = CanonicalConversationModel
    personal = aliased(IdentityBindingModel)
    account = aliased(AccountModel)
    presence = aliased(PresenceModel)
    presence_account = aliased(AccountModel)
    space = SpaceBindingModel
    platform = func.coalesce(
        case((entry.kind == "private", account.platform), else_=space.platform),
        presence_account.platform,
    ).label("platform")
    name = case(
        (
            entry.kind == "private",
            func.coalesce(func.nullif(account.display_name, ""), CanonicalPersonModel.display_name),
        ),
        else_=func.coalesce(func.nullif(space.display_name, ""), CanonicalSpaceModel.name),
    ).label("conversation_name")
    statement = (
        select(
            entry.id.label("conversation_id"),
            entry.kind.label("conversation_kind"),
            entry.person_id,
            entry.space_id,
            func.coalesce(entry.identity_binding_id, entry.space_binding_id).label("binding_id"),
            entry.entry_presence_id.label("presence_id"),
            entry.thread_id,
            platform,
            name,
            case((entry.kind == "private", personal.status), else_=space.status).label(
                "binding_status"
            ),
            presence.status.label("presence_status"),
            entry.generation,
        )
        .select_from(entry)
        .outerjoin(personal, entry.identity_binding_id == personal.id)
        .outerjoin(account, account.id == personal.account_id)
        .outerjoin(space, entry.space_binding_id == space.id)
        .outerjoin(presence, entry.entry_presence_id == presence.id)
        .outerjoin(presence_account, presence_account.id == presence.account_id)
        .outerjoin(CanonicalPersonModel, CanonicalPersonModel.id == entry.person_id)
        .outerjoin(CanonicalSpaceModel, CanonicalSpaceModel.id == entry.space_id)
    )
    if conversation_id is not None:
        statement = statement.where(entry.id == conversation_id)
    if target is not None:
        if target.conversation_id is not None:
            statement = statement.where(entry.id == str(target.conversation_id))
        elif target.person_id is not None:
            statement = statement.where(
                person_family_condition(entry.person_id, str(target.person_id))
            )
        elif target.space_id is not None:
            statement = statement.where(entry.space_id == str(target.space_id))
        elif target.binding_id is not None:
            statement = statement.where(
                or_(
                    entry.identity_binding_id == str(target.binding_id),
                    entry.space_binding_id == str(target.binding_id),
                )
            )
        elif target.operation_id is not None:
            operation = await session.get(SocialOperationModel, str(target.operation_id))
            if operation is None or operation.source_conversation_id != context.conversation_id:
                raise SocialError("history_receipt_unavailable")
            original = (
                await session.get(ChatEventModel, operation.event_id)
                if operation.event_id is not None
                else None
            )
            original_conversation = operation.original_conversation_id or (
                original.canonical_conversation_id if original is not None else None
            )
            statement = statement.where(entry.id == original_conversation)

    allowed = getattr(runtime, "read_target_id", None)
    if allowed is not None:
        statement = statement.where(
            or_(person_family_condition(entry.person_id, allowed), entry.space_id == allowed)
        )
        granted = getattr(runtime, "read_scope", None)
        if granted is not None and granted.conversation_id:
            statement = statement.where(entry.id == granted.conversation_id)
    elif runtime is not None and getattr(runtime, "origin", None) == "plugin_background":
        statement = statement.where(entry.id == context.conversation_id)
    if source is not None:
        if source.platform is not None:
            statement = statement.where(platform == source.platform)
        if source.scope is not None:
            statement = statement.where(
                entry.kind == ("private" if source.scope == "private" else "space")
            )
    return statement
