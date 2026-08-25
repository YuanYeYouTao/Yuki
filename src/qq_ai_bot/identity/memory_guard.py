"""v2 live-memory guards that do not require new schema columns."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.runtime import identity_runtime_is_complete_v2
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryToolReceiptModel,
)


async def refuse_legacy_live_event(session: AsyncSession, event: ChatEventModel) -> bool:
    """True when a NULL canonical_event_id must not enter a live v2 branch."""

    if event.canonical_event_id:
        return False
    return await identity_runtime_is_complete_v2(session)


async def refuse_legacy_live_fact(session: AsyncSession, fact_id: int) -> bool:
    """True when a fact cites a legacy-NULL event or has no v2 owner chain."""

    if not await identity_runtime_is_complete_v2(session):
        return False
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
    if fact.scope_type == "self":
        return False
    owned = any(
        (
            fact.canonical_subject_person_id,
            fact.canonical_subject_space_id,
            fact.canonical_visibility_person_id,
            fact.canonical_visibility_space_id,
        )
    )
    if owned or event_ids or tool_ids:
        return False
    return True
