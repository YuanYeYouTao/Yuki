"""Bounded receipt deletion and fingerprint-local candidate expiry preserve live sources."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, update
from tests.unit.test_memory_v2 import _append_event, _claim
from tests.unit.test_self_initiative_memory import claim, finish, record, seed

from qq_ai_bot.memory.claim_candidates import MemoryClaimCandidateRepository
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.models import (
    MemoryClaimCandidateEvidenceModel,
    MemoryClaimCandidateModel,
    MemoryEvidenceModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository


async def ordinary_receipts(database, count):
    source = await _append_event(EventLedgerRepository(database), message_id="cleanup-source")
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        rows = [
            MemoryToolReceiptModel(
                conversation_key_hash="cleanup",
                trigger_event_id=source.id,
                bot_user_id="8000",
                canonical_person_id=source.author_person_id,
                provider_id="fake",
                tool_name="test",
                success=True,
                result_excerpt="verified",
                result_characters=8,
                created_at=now - timedelta(days=8),
                expires_at=now - timedelta(seconds=1),
            )
            for _ in range(count)
        ]
        session.add_all(rows)
        await session.flush()
        return tuple(row.id for row in rows)


async def test_receipt_cleanup_empty_poll_is_read_only_while_another_writer_is_active(database):
    statements = []

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement)

    async with database.immediate_session():
        event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            assert await SelfReflectionRepository(database).cleanup_receipts() == 0
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert len(statements) == 1 and statements[0].startswith("SELECT")


async def test_receipt_cleanup_deletes_only_one_bounded_page_and_retains_evidence(database):
    ids = await ordinary_receipts(database, 7)
    facts = MemoryFactService(MemoryFactRepository(database))
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type="self",
            visibility_type="global",
            memory_key="cleanup:evidence",
            category="test",
            content="verified historical evidence",
            source_type="automatic",
        ),
        evidence=MemoryEvidenceCreate(
            tool_receipt_id=ids[0],
            source_speaker_user_id="1001",
            relation="confirmation",
            authority="self_report",
            excerpt="verified",
        ),
    )
    repository = SelfReflectionRepository(database)
    assert await repository.cleanup_receipts(limit=2) == 2
    async with database.sessions() as session:
        assert list(
            await session.scalars(
                select(MemoryToolReceiptModel.id).order_by(MemoryToolReceiptModel.id)
            )
        ) == [ids[0], *ids[3:]]
    assert await repository.cleanup_receipts(limit=2) == 2
    assert await repository.cleanup_receipts(limit=2) == 2
    assert await repository.cleanup_receipts(limit=2) == 0
    assert (await facts.list_evidence(fact.id))[0].tool_receipt_id == ids[0]


@pytest.mark.parametrize("change", ["evidence", "expiry"])
async def test_receipt_cleanup_rechecks_reference_and_expiry_after_read_discovery(
    database, monkeypatch, change
):
    (receipt_id,) = await ordinary_receipts(database, 1)
    facts = MemoryFactService(MemoryFactRepository(database))
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type="self",
            visibility_type="global",
            memory_key="cleanup:race",
            category="test",
            content="receipt race",
            source_type="automatic",
        )
    )
    original_sessions = database.sessions
    discovered = False

    @asynccontextmanager
    async def interleaved_sessions():
        nonlocal discovered
        async with original_sessions() as session:
            yield session
        if not discovered:
            discovered = True
            # A different connection commits between discovery and deletion.
            async with original_sessions() as writer, writer.begin():
                if change == "evidence":
                    writer.add(
                        MemoryEvidenceModel(
                            fact_id=fact.id,
                            tool_receipt_id=receipt_id,
                            source_speaker_user_id="1001",
                            relation="confirmation",
                            authority="self_report",
                            confidence=1,
                            excerpt="verified",
                            created_at=datetime.now(UTC),
                        )
                    )
                else:
                    await writer.execute(
                        update(MemoryToolReceiptModel)
                        .where(MemoryToolReceiptModel.id == receipt_id)
                        .values(expires_at=datetime.now(UTC) + timedelta(days=1))
                    )

    monkeypatch.setattr(database, "sessions", interleaved_sessions)
    assert await SelfReflectionRepository(database).cleanup_receipts() == 0
    async with original_sessions() as session:
        assert await session.get(MemoryToolReceiptModel, receipt_id) is not None


async def test_receipt_cleanup_rechecks_new_unfinished_initiative_window(database, monkeypatch):
    _, _, source, run_id = await seed(database)
    await record(database, source, run_id)
    await finish(database, run_id)
    original_sessions = database.sessions
    async with original_sessions() as session, session.begin():
        receipt = await session.scalar(select(MemoryToolReceiptModel))
        receipt_id = receipt.id
        receipt.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    discovered = False
    selected_window = None

    @asynccontextmanager
    async def interleaved_sessions():
        nonlocal discovered, selected_window
        async with original_sessions() as session:
            yield session
        if not discovered:
            discovered = True
            # Claim while the receipt is temporarily available to the input
            # picker, then expire it again. DELETE must use the new window.
            async with original_sessions() as session, session.begin():
                await session.execute(
                    update(MemoryToolReceiptModel)
                    .where(MemoryToolReceiptModel.id == receipt_id)
                    .values(expires_at=datetime.now(UTC) + timedelta(days=1))
                )
            monkeypatch.setattr(database, "sessions", original_sessions)
            (selected_window,) = await claim(database)
            monkeypatch.setattr(database, "sessions", interleaved_sessions)
            async with original_sessions() as session, session.begin():
                await session.execute(
                    update(MemoryToolReceiptModel)
                    .where(MemoryToolReceiptModel.id == receipt_id)
                    .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )

    monkeypatch.setattr(database, "sessions", interleaved_sessions)
    assert await SelfReflectionRepository(database).cleanup_receipts() == 0
    assert selected_window.first_receipt_id == receipt_id
    async with original_sessions() as session:
        assert await session.get(MemoryToolReceiptModel, receipt_id) is not None


async def test_candidate_stage_reopens_only_its_expired_fingerprint_and_counts_distinct_events(
    database,
):
    ledger = EventLedgerRepository(database)
    first = await _append_event(ledger, message_id="candidate-old")
    second = await _append_event(ledger, message_id="candidate-new")
    jobs = MemoryJobRepository(database)
    await jobs.enqueue(first.id, "private:1001")
    await jobs.enqueue(second.id, "private:1001")
    first_job, second_job = await jobs.claim()
    candidates = MemoryClaimCandidateRepository(database)
    claim = _claim(confidence=0.5)
    original = await candidates.stage(
        claim, first, candidate_type="memory", subject_context=None, job=first_job
    )
    unrelated = await candidates.stage(
        _claim(memory_key="other:claim", content="unrelated", confidence=0.5),
        first,
        candidate_type="memory",
        subject_context=None,
        job=first_job,
    )
    old_expiry = datetime.now(UTC) - timedelta(days=1)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(MemoryClaimCandidateModel)
            .where(MemoryClaimCandidateModel.id.in_((original.id, unrelated.id)))
            .values(expires_at=old_expiry)
        )
        untouched = await session.get(MemoryClaimCandidateModel, unrelated.id)
        unrelated_updated_at = untouched.updated_at
    reopened = await candidates.stage(
        claim, second, candidate_type="memory", subject_context=None, job=second_job
    )
    assert reopened.id == original.id and reopened.evidence_count == 1
    assert reopened.status == "pending" and reopened.expires_at > datetime.now(UTC)
    duplicate = await candidates.stage(
        claim, second, candidate_type="memory", subject_context=None, job=second_job
    )
    assert duplicate.evidence_count == 1
    distinct = await candidates.stage(
        claim, first, candidate_type="memory", subject_context=None, job=first_job
    )
    assert distinct.evidence_count == 2
    async with database.sessions() as session:
        untouched = await session.get(MemoryClaimCandidateModel, unrelated.id)
        assert untouched.status == "pending" and untouched.updated_at == unrelated_updated_at
        evidence_ids = tuple(
            await session.scalars(
                select(MemoryClaimCandidateEvidenceModel.event_id).where(
                    MemoryClaimCandidateEvidenceModel.candidate_id == original.id
                )
            )
        )
        assert set(evidence_ids) == {first.id, second.id}
