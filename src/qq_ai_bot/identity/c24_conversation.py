"""C24b Conversation correlation for telemetry writers.

canonical_conversation_id is a correlation shadow, not an owner. This module
never inserts Conversation, Person, or Space and never treats conversation_key,
hashes, or raw QQ/group ids as Conversation identity.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    PresenceModel,
)
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.identity.shadows import assign_shadow
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_helpers import keeper_event_clause

CANONICAL_KIND_MISMATCH = "canonical_kind_mismatch"
MISSING_CANONICAL_CONVERSATION = "missing_canonical_conversation"


class ConversationCorrelationRow(Protocol):
    canonical_conversation_id: str | None


def _token(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


async def require_live_conversation(session: AsyncSession, conversation_id: str | None) -> str:
    """Read a Conversation by canonical id only. Person/Space/Presence ids are wrong-kind."""

    token = _token(conversation_id)
    if token is None:
        raise IdentityDualWriteError(MISSING_CANONICAL_CONVERSATION)
    if await session.get(PresenceModel, token) is not None:
        raise IdentityDualWriteError(CANONICAL_KIND_MISMATCH)
    if await session.get(CanonicalPersonModel, token) is not None:
        raise IdentityDualWriteError(CANONICAL_KIND_MISMATCH)
    if await session.get(CanonicalSpaceModel, token) is not None:
        raise IdentityDualWriteError(CANONICAL_KIND_MISMATCH)
    conversation = await session.get(CanonicalConversationModel, token)
    if conversation is None:
        raise IdentityDualWriteError(MISSING_CANONICAL_CONVERSATION)
    return conversation.id


async def try_live_conversation_id(
    session: AsyncSession, conversation_id: str | None
) -> str | None:
    """Return a live Conversation id, or None when the optional shadow is absent."""

    if _token(conversation_id) is None:
        return None
    try:
        return await require_live_conversation(session, conversation_id)
    except IdentityDualWriteError as exc:
        if exc.category == MISSING_CANONICAL_CONVERSATION:
            return None
        raise


async def stamp_conversation_correlation(
    session: AsyncSession,
    row: ConversationCorrelationRow,
    proven: str | None,
) -> None:
    """Assign an already-resolved Conversation correlation. Conflict fails closed."""

    conversation_id = await try_live_conversation_id(session, proven)
    row.canonical_conversation_id = await assign_shadow(
        row.canonical_conversation_id, conversation_id
    )


async def resolve_conversation_id_for_event(
    session: AsyncSession,
    event_id: int | None,
) -> str | None:
    """Resolve from a persisted chat-event primary key. Missing event stays null."""

    if event_id is None:
        return None
    event = await session.get(ChatEventModel, event_id)
    if event is None:
        return None
    return await try_live_conversation_id(session, event.canonical_conversation_id)


async def load_unique_live_chat_event(
    session: AsyncSession,
    *,
    platform_message_id: str | None,
    bot_user_id: str | None = None,
    ingress_presence_id: str | None = None,
    require_bot_or_presence: bool = False,
) -> ChatEventModel | None:
    """Load one live chat event. Zero or multiple matches return None. Never first-row."""

    message_id = _token(platform_message_id)
    bot = _token(bot_user_id)
    presence = _token(ingress_presence_id)
    if message_id is None:
        return None
    if require_bot_or_presence and bot is None and presence is None:
        return None
    statement = select(ChatEventModel).where(
        ChatEventModel.platform_message_id == message_id,
        keeper_event_clause(),
    )
    if presence is not None:
        statement = statement.where(ChatEventModel.ingress_presence_id == presence)
    if bot is not None:
        statement = statement.where(ChatEventModel.bot_user_id == bot)
    rows = list(await session.scalars(statement))
    if len(rows) != 1:
        return None
    return rows[0]


async def resolve_conversation_id_for_chat_event(
    session: AsyncSession,
    *,
    platform_message_id: str | None,
    bot_user_id: str | None = None,
    ingress_presence_id: str | None = None,
    require_bot_or_presence: bool = False,
) -> str | None:
    """Deterministic Conversation from one unique persisted live chat event."""

    event = await load_unique_live_chat_event(
        session,
        platform_message_id=platform_message_id,
        bot_user_id=bot_user_id,
        ingress_presence_id=ingress_presence_id,
        require_bot_or_presence=require_bot_or_presence,
    )
    if event is None:
        return None
    return await try_live_conversation_id(session, event.canonical_conversation_id)


__all__ = [
    "CANONICAL_KIND_MISMATCH",
    "MISSING_CANONICAL_CONVERSATION",
    "load_unique_live_chat_event",
    "require_live_conversation",
    "resolve_conversation_id_for_chat_event",
    "resolve_conversation_id_for_event",
    "stamp_conversation_correlation",
    "try_live_conversation_id",
]
