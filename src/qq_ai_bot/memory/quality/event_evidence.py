"""Inspect event evidence using the same text projection as its writer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.memory.quality.models import ProductionAuditIssue
from qq_ai_bot.memory.validation import normalize_memory_text
from qq_ai_bot.persistence.models import ChatEventModel, MemoryEvidenceModel, MemoryFactModel
from qq_ai_bot.persistence.repository_helpers import _event_record, suppression_is_canonical_live


@dataclass(frozen=True)
class EventEvidenceCheck:
    source_valid: bool
    excerpt_valid: bool
    speaker_valid: bool

    @property
    def valid(self) -> bool:
        return self.source_valid and self.excerpt_valid and self.speaker_valid


def inspect_event_evidence(
    fact: MemoryFactModel, evidence: MemoryEvidenceModel, event: ChatEventModel
) -> EventEvidenceCheck:
    reflection = (
        fact.scope_type == "self"
        and evidence.relation == "agent_reflection"
        and evidence.authority == "agent_reflection"
    )
    human = event.direction == "inbound" and event.author_kind == "person"
    yuki = event.direction == "outbound" and event.author_kind == "yuki"
    source = (
        ChatEventPromptRenderer.event_content(_event_record(event), None, "")
        if reflection
        else event.content
    )
    normalized = normalize_memory_text(source, maximum=4000)
    quote = evidence.excerpt
    return EventEvidenceCheck(
        source_valid=(
            (human or (reflection and yuki))
            and suppression_is_canonical_live(event.suppression_status)
            and bool(normalized)
        ),
        # Additional evidence can retain the raw excerpt; mutation quotes are
        # flattened before storage. Neither path admits unrelated text.
        excerpt_valid=bool(quote.strip()) and (quote in source or quote in normalized),
        speaker_valid=evidence.source_speaker_user_id == event.sender_user_id,
    )


async def _event_rows(
    session: AsyncSession, fact_ids: tuple[int, ...] | None = None
) -> AsyncIterator[tuple[MemoryFactModel, MemoryEvidenceModel, ChatEventModel]]:
    query = (
        select(MemoryFactModel, MemoryEvidenceModel, ChatEventModel)
        .join(MemoryEvidenceModel, MemoryEvidenceModel.fact_id == MemoryFactModel.id)
        .join(ChatEventModel, ChatEventModel.id == MemoryEvidenceModel.event_id)
        .order_by(MemoryEvidenceModel.id)
    )
    if fact_ids is not None:
        query = query.where(MemoryFactModel.id.in_(fact_ids))
    rows = await session.stream(query.execution_options(yield_per=100))
    try:
        async for fact, evidence, event in rows:
            yield fact, evidence, event
    finally:
        await rows.close()


async def audit_event_evidence(session: AsyncSession) -> tuple[ProductionAuditIssue, ...]:
    codes = ("evidence_source_invalid", "evidence_excerpt_missing", "evidence_speaker_mismatch")
    counts = dict.fromkeys(codes, 0)
    samples: dict[str, list[int]] = {code: [] for code in codes}
    async for fact, evidence, event in _event_rows(session):
        checked = inspect_event_evidence(fact, evidence, event)
        for code, valid in zip(
            codes, (checked.source_valid, checked.excerpt_valid, checked.speaker_valid), strict=True
        ):
            if not valid:
                counts[code] += 1
                if len(samples[code]) < 20:
                    samples[code].append(evidence.id)
    return tuple(
        ProductionAuditIssue(
            issue_code=code, severity="error", count=counts[code], sample_ids=tuple(samples[code])
        )
        for code in codes
    )


async def facts_with_valid_event_evidence(
    session: AsyncSession, fact_ids: tuple[int, ...]
) -> set[int]:
    valid: set[int] = set()
    if not fact_ids:
        return valid
    async for fact, evidence, event in _event_rows(session, fact_ids):
        if inspect_event_evidence(fact, evidence, event).valid:
            valid.add(fact.id)
    return valid
