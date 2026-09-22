"""Canonical live-memory safety guards."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.evidence import event_is_canonical_live, fact_conversation_aligns
from qq_ai_bot.memory.partition import canonical_fact_owner_complete
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryToolReceiptModel,
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


async def refuse_legacy_live_fact(session: AsyncSession, fact_id: int) -> bool:
    """True when a fact cites a legacy-NULL event or has no v2 owner chain."""

    fact = await session.get(MemoryFactModel, fact_id)
    if fact is None:
        return True
    event_ids = list(
        await session.scalars(
            select(ChatEventModel.canonical_event_id).where(
                ChatEventModel.id.in_(
                    select(MemoryEvidenceModel.event_id).where(
                        MemoryEvidenceModel.fact_id == fact_id,
                        MemoryEvidenceModel.event_id.is_not(None),
                    )
                )
            )
        )
    )
    if any(item is None for item in event_ids):
        return True
    tool_ids = list(
        await session.scalars(
            select(ChatEventModel.canonical_event_id)
            .select_from(MemoryToolReceiptModel)
            .join(
                ChatEventModel,
                ChatEventModel.id == MemoryToolReceiptModel.trigger_event_id,
            )
            .where(
                MemoryToolReceiptModel.id.in_(
                    select(MemoryEvidenceModel.tool_receipt_id).where(
                        MemoryEvidenceModel.fact_id == fact_id,
                        MemoryEvidenceModel.tool_receipt_id.is_not(None),
                    )
                )
            )
        )
    )
    if any(item is None for item in tool_ids):
        return True
    return not canonical_fact_owner_complete(fact)


def _fact_aligns_conversation(
    fact: MemoryFactModel, conversation: CanonicalConversationModel
) -> bool:
    return fact_conversation_aligns(
        scope_type=str(fact.scope_type or ""),
        visibility_type=fact.visibility_type,
        subject_person_id=fact.canonical_subject_person_id,
        subject_space_id=fact.canonical_subject_space_id,
        visibility_person_id=fact.canonical_visibility_person_id,
        visibility_space_id=fact.canonical_visibility_space_id,
        conversation_person_id=conversation.person_id,
        conversation_space_id=conversation.space_id,
    )


async def refuse_unreadable_v2_evidence_event(session: AsyncSession, event: ChatEventModel) -> bool:
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


async def v2_evidence_event_chain_readable(
    session: AsyncSession,
    fact: MemoryFactModel,
    event: ChatEventModel,
) -> bool:
    if not canonical_fact_owner_complete(fact):
        return False
    if await refuse_unreadable_v2_evidence_event(session, event):
        return False
    if not event.canonical_conversation_id:
        return False
    conversation = await session.get(CanonicalConversationModel, event.canonical_conversation_id)
    if conversation is None:
        return False
    return _fact_aligns_conversation(fact, conversation)


async def v2_evidence_row_readable(
    session: AsyncSession,
    fact: MemoryFactModel,
    evidence: MemoryEvidenceModel,
) -> bool:
    if evidence.event_id is not None:
        event = await session.get(ChatEventModel, evidence.event_id)
        if event is None:
            return False
        return await v2_evidence_event_chain_readable(session, fact, event)
    if evidence.tool_receipt_id is None:
        return False
    receipt = await session.get(MemoryToolReceiptModel, evidence.tool_receipt_id)
    if receipt is None:
        return False
    if receipt.initiative_run_id is not None:
        from qq_ai_bot.memory.self_origin import receipt_evidence_readable

        return await receipt_evidence_readable(
            session, fact=fact, evidence=evidence, receipt=receipt
        )
    if receipt.trigger_event_id is None:
        return False
    trigger = await session.get(ChatEventModel, receipt.trigger_event_id)
    if trigger is None:
        return False
    return await v2_evidence_event_chain_readable(session, fact, trigger)
