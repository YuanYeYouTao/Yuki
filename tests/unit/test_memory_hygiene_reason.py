"""Hygiene records missing provenance rather than a fictitious administrator."""

from sqlalchemy import select, text, update
from tests.unit.test_memory_mutation import _context, _event, _service

from qq_ai_bot.memory.enums import MemoryInvalidationReason, MemoryScopeType, MemoryStatus
from qq_ai_bot.memory.mutation.models import (
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from qq_ai_bot.memory.quality.hygiene import MemoryProvenanceHygiene
from qq_ai_bot.persistence.models import MemoryFactModel, MemoryFactStateEventModel


async def test_invalid_provenance_reason_matches_fact_and_state_event(database):
    service, facts, ledger, _ = _service(database)
    event = await _event(
        ledger, message_id="legacy-provenance", sender_user_id="1001", content="准备考研"
    )
    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker", scope_type=MemoryScopeType.PERSON
            ),
            new_content="准备考研",
            memory_key="education:plan",
            category="education",
        ),
        _context(event),
    )
    assert result.ok
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == result.new_fact_id)
            .values(source_type="automatic")
        )
        await writer.execute(
            text("DELETE FROM memory_evidence WHERE fact_id=:id"), {"id": result.new_fact_id}
        )
    hygiene = MemoryProvenanceHygiene(database)
    plan = await hygiene.scan()
    assert result.new_fact_id in plan.invalid_fact_ids
    await hygiene.apply(plan.fingerprint)
    current = await facts.get_fact(result.new_fact_id)
    assert current.status is MemoryStatus.INVALIDATED
    assert current.invalidated_reason is MemoryInvalidationReason.INVALID_PROVENANCE
    async with database.sessions() as reader:
        state = await reader.scalar(
            select(MemoryFactStateEventModel)
            .where(MemoryFactStateEventModel.fact_id == current.id)
            .order_by(MemoryFactStateEventModel.id.desc())
            .limit(1)
        )
        assert state.reason_code == current.invalidated_reason.value
        assert state.actor_user_id is None
