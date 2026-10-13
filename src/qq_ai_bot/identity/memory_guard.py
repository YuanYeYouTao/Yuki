"""Canonical live-memory safety guards."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.evidence import event_is_canonical_live
from qq_ai_bot.persistence.models import (
    ChatEventModel,
)


async def refuse_legacy_live_event(session: AsyncSession, event: ChatEventModel) -> bool:
    """True when a live v2 Memory path must not accept this chat event."""

    if not event.canonical_event_id:
        return True
    conversation_id = event.canonical_conversation_id
    if not conversation_id:
        return True
    conversation = await session.get(CanonicalConversationModel, conversation_id)
    if conversation is None:
        return True
    event_id = int(event.id)
    if event_id <= int(conversation.starts_after_event_id):
        return True
    if event_id <= int(conversation.last_generation_change_event_id):
        return True
    return False


def refuse_unreadable_v2_evidence_event(event: ChatEventModel) -> bool:
    """True when v2 must not treat this event as readable evidence/history.

    Enqueue/worker watermarks (starts_after_event_id / generation) are not a
    history-read gate. Hidden suppression and missing canonical chain are.
    """

    return not event_is_canonical_live(
        canonical_event_id=event.canonical_event_id,
        suppression_status=event.suppression_status,
        canonical_conversation_id=event.canonical_conversation_id,
        author_kind=event.author_kind,
    )
