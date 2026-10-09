"""Bounded backfill and atomic preparation of real SQLite evidence compaction."""

import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, update
from tests.conftest import make_settings
from tests.unit.test_memory_v2 import _append_event

from qq_ai_bot.memory.evidence_compaction import EvidenceCompactionService
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryMutationReceiptModel,
    MemorySelfReflectionResultModel,
    MemorySelfReflectionRunModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository


async def _seed(database, *, count=13):
    facts = MemoryFactService(MemoryFactRepository(database))
    ledger = EventLedgerRepository(database)
    sources = [
        await _append_event(ledger, message_id=str(uuid4()), content="evidence")
        for _ in range(count)
    ]
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type="self",
            visibility_type="global",
            memory_key=str(uuid4()),
            category="test",
            content="episode",
            kind="episode",
            source_type="automatic",
            authority="agent_reflection",
        )
    )
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        run = MemorySelfReflectionRunModel(
            conversation_key_hash="test",
            bot_user_id="8000",
            canonical_person_id=sources[0].author_person_id,
            scheduled_slot=str(uuid4()),
            trigger_reason="test",
            first_event_id=sources[0].id,
            last_event_id=sources[-1].id,
            status="completed",
            started_at=now,
        )
        session.add(run)
        await session.flush()
        session.add(
            MemorySelfReflectionResultModel(
                run_id=run.id,
                fact_id=fact.id,
                result_kind="episode",
                result_index=0,
                created_at=now,
            )
        )
        session.add(
            MemoryMutationReceiptModel(
                mutation_id=str(uuid4()),
                idempotency_key=str(uuid4()),
                claim_fingerprint=str(uuid4()),
                target_fingerprint="test",
                trigger_event_id=sources[0].id,
                conversation_key="test",
                turn_origin="memory_self_reflection",
                delegation_mode=f"self_episode:{sources[0].id}:{sources[-1].id}",
                trigger_actor_user_id="8000",
                decision_actor_type="reflection",
                executed_by_bot_user_id="8000",
                requested_operation="create",
                applied_operation="create",
                new_fact_id=fact.id,
                outcome="committed",
                reason_code="test",
                created_at=now,
            )
        )
        session.add_all(
            [
                MemoryEvidenceModel(
                    fact_id=fact.id,
                    event_id=source.id,
                    source_speaker_user_id="1001",
                    relation="agent_reflection",
                    confidence=0.05,
                    authority="agent_reflection",
                    excerpt="evidence",
                    created_at=now,
                )
                for source in sources
            ]
        )
    compaction = EvidenceCompactionService(
        settings=make_settings(database.url), database=database, facts=facts
    )
    return facts, compaction, fact, sources, run


@pytest.mark.parametrize("change", ["hidden", "cascade", "evidence_delete"])
async def test_compaction_reprepares_stale_snapshot_and_stops_history_reads_after_delete(
    database, monkeypatch, change
):
    facts, service, fact, sources, _ = await _seed(database)
    prepare = facts.prepare_evidence_metadata
    attempts = 0

    async def mutate_after_preparation(current, evidence):
        nonlocal attempts
        prepared = await prepare(current, evidence)
        attempts += 1
        if attempts == 1:
            async with database.immediate_session() as session:
                if change == "hidden":
                    await session.execute(
                        update(ChatEventModel)
                        .where(ChatEventModel.id == sources[-1].id)
                        .values(suppression_status="duplicate", utterance_fingerprint="a" * 64)
                    )
                elif change == "cascade":
                    await session.execute(
                        delete(ChatEventModel).where(ChatEventModel.id == sources[-1].id)
                    )
                else:
                    await session.execute(
                        delete(MemoryEvidenceModel).where(
                            MemoryEvidenceModel.event_id == sources[-1].id
                        )
                    )
        return prepared

    monkeypatch.setattr(facts, "prepare_evidence_metadata", mutate_after_preparation)
    statements = []

    def observe(_connection, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        assert await service.run_batch() == 1
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
    assert attempts == 2
    assert len(await facts.list_evidence(fact.id)) <= 3
    last_delete = max(
        i
        for i, statement in enumerate(statements)
        if statement.startswith("DELETE FROM memory_evidence")
    )
    assert not any(
        statement.startswith("SELECT") and re.search(r"\bmemory_evidence\b", statement)
        for statement in statements[last_delete + 1 :]
    )
    current = await facts.get_fact(fact.id)
    remaining = await facts.list_evidence(fact.id)
    assert current.evidence_count == len(remaining)
    assert current.confidence == fact.confidence


async def test_compaction_aggregates_all_retained_evidence_beyond_a_read_page(
    database, monkeypatch
):
    facts, service, fact, _, _ = await _seed(database)
    read_evidence = facts.repository.list_evidence

    async def bounded_public_page(fact_id, *, limit=100, session=None):
        # Scale the public page boundary down instead of constructing 100001 rows.
        return await read_evidence(
            fact_id, limit=None if limit is None else min(limit, 3), session=session
        )

    monkeypatch.setattr(facts.repository, "list_evidence", bounded_public_page)
    assert await service.run_batch() == 1
    current = await facts.get_fact(fact.id)
    remaining = await read_evidence(fact.id, limit=None)
    assert current.evidence_count == len(remaining)
    assert current.confidence == fact.confidence
