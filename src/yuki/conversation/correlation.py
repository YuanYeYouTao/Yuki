"""Canonical Conversation correlation for telemetry and media writers."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.canonical_repository import assert_same_shadow
from yuki.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel, PresenceModel
from yuki.identity.errors import CanonicalIdentityError
from yuki.persistence.models import ChatEventModel

CANONICAL_KIND_MISMATCH = "canonical_kind_mismatch"
MISSING_CANONICAL_CONVERSATION = "missing_canonical_conversation"


class ConversationCorrelationRow(Protocol):
    canonical_conversation_id: str | None


def _token(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


async def require_live_conversation(
    session: AsyncSession, conversation_id: str | None
) -> CanonicalConversationModel:
    """Resolve a live Conversation and reject canonical IDs of another kind."""

    token = _token(conversation_id)
    if token is None:
        raise CanonicalIdentityError(MISSING_CANONICAL_CONVERSATION)
    conversation = await session.get(CanonicalConversationModel, token)
    if conversation is not None:
        return conversation
    if (
        await session.get(PresenceModel, token) is not None
        or await session.get(CanonicalPersonModel, token) is not None
        or await session.get(CanonicalSpaceModel, token) is not None
    ):
        raise CanonicalIdentityError(CANONICAL_KIND_MISMATCH)
    raise CanonicalIdentityError(MISSING_CANONICAL_CONVERSATION)


async def try_live_conversation(
    session: AsyncSession, conversation_id: str | None
) -> CanonicalConversationModel | None:
    if _token(conversation_id) is None:
        return None
    try:
        return await require_live_conversation(session, conversation_id)
    except CanonicalIdentityError as exc:
        if exc.category == MISSING_CANONICAL_CONVERSATION:
            return None
        raise


async def stamp_conversation_correlation(
    session: AsyncSession,
    row: ConversationCorrelationRow,
    proven: str | None,
) -> None:
    conversation = await try_live_conversation(session, proven)
    conversation_id = conversation.id if conversation is not None else None
    assert_same_shadow(row.canonical_conversation_id, conversation_id)
    if row.canonical_conversation_id is None:
        row.canonical_conversation_id = conversation_id


async def load_correlated_chat_event(
    session: AsyncSession,
    *,
    trigger_event_id: int | None,
    canonical_conversation_id: str | None,
    bot_user_id: str | None = None,
    ingress_presence_id: str | None = None,
) -> ChatEventModel | None:
    """Use the trusted ledger primary key; never reconstruct it from a QQ id."""
    if trigger_event_id is None:
        return None
    event = await session.get(ChatEventModel, trigger_event_id)
    if event is None or event.suppression_status not in {None, "keeper"}:
        return None
    if (
        (canonical_conversation_id and event.canonical_conversation_id != canonical_conversation_id)
        or (bot_user_id and event.bot_user_id != bot_user_id)
        or (ingress_presence_id and event.ingress_presence_id != ingress_presence_id)
    ):
        raise CanonicalIdentityError("trigger_event_correlation_mismatch")
    return event


__all__ = [
    "CANONICAL_KIND_MISMATCH",
    "MISSING_CANONICAL_CONVERSATION",
    "load_correlated_chat_event",
    "require_live_conversation",
    "stamp_conversation_correlation",
    "try_live_conversation",
]
