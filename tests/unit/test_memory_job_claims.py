"""Late Memory workers cannot commit over a reclaimed or completed queue item."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

import pytest
from sqlalchemy import func, select, text
from tests.conftest import make_settings
from tests.support.model_executor import InjectedModelExecutor
from tests.support.projection_sql_counts import capture_sql
from tests.unit.test_memory_mutation import _service
from tests.unit.test_memory_v2 import _append_event, _claim

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.messages import ChatRequest, ChatResponse
from yuki.llm.base import LLMProvider
from yuki.memory.claim_processor import MemoryProcessingContext
from yuki.memory.enums import MemoryRebuildJobOutcome
from yuki.memory.extraction import BatchMemoryClaim, BatchMemoryExtractionOutput
from yuki.memory.job_claims import MemoryJobClaimLost
from yuki.memory.models import MemoryJob
from yuki.memory.repository import MemoryFactRepository, MemoryJobRepository
from yuki.memory.service import MemoryFactService
from yuki.memory.worker import MemoryWorker
from yuki.persistence.database import Database
from yuki.persistence.models import (
    ChatEventModel,
    MemoryClaimCandidateModel,
    MemoryFactModel,
    MemoryJobModel,
    MemoryMutationReceiptModel,
)
from yuki.persistence.repositories import EventLedgerRepository
from yuki.services.concurrency import ConcurrencyManager


class _ReclaimTime(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return datetime.now(tz) + timedelta(minutes=6)


async def test_duplicate_enqueue_returns_original_partition_without_waiting_for_writer(database):
    event = await _append_event(EventLedgerRepository(database), message_id="enqueue-original")
    jobs = MemoryJobRepository(database)
    created, original_partition = await jobs.enqueue_resolved(event.id, "ignored-input-scope")
    assert created
    async with database.read_session() as reader:
        before = (
            (
                await reader.execute(
                    select(MemoryJobModel.__table__).where(MemoryJobModel.event_id == event.id)
                )
            )
            .mappings()
            .one()
        )
    async with database.immediate_session():
        with capture_sql(database) as statements:
            assert await asyncio.wait_for(
                jobs.enqueue_resolved(event.id, "another-input-scope"), 0.5
            ) == (False, original_partition)
        assert statements and all(
            sql.lstrip().upper().startswith("SELECT") for sql, _ in statements
        )
    async with database.read_session() as reader:
        after = (
            (
                await reader.execute(
                    select(MemoryJobModel.__table__).where(MemoryJobModel.event_id == event.id)
                )
            )
            .mappings()
            .one()
        )
    assert after == before


async def test_queue_health_preserves_owner_waiting_stale_and_failure_categories(
    database,
    monkeypatch,
):
    clock = datetime.now(UTC)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    monkeypatch.setattr("yuki.memory.repository.datetime", FixedClock)
    ledger = EventLedgerRepository(database)
    events = [
        await _append_event(
            ledger,
            message_id=f"health:{index}",
            user_id="2002" if index == 2 else "1001",
            content="abc",
            group_id="3001" if index in (3, 4) else None,
        )
        for index in range(10)
    ]
    jobs = MemoryJobRepository(database)
    category = "x" * 64
    async with database.immediate_session() as session:
        await session.execute(text("PRAGMA ignore_check_constraints=ON"))
        try:
            for index, event in enumerate(events):
                owner = await session.get(ChatEventModel, event.id)
                conversation = await session.get(
                    CanonicalConversationModel, owner.canonical_conversation_id
                )
                session.add(
                    MemoryJobModel(
                        event_id=event.id,
                        conversation_key=event.scope.key,
                        canonical_person_id=conversation.person_id if index != 8 else None,
                        canonical_space_id=conversation.space_id if index != 8 else None,
                        status="processing"
                        if index in (3, 4)
                        else "failed"
                        if index in (6, 7)
                        else "done"
                        if index == 9
                        else "pending",
                        attempts=int(index == 2),
                        next_attempt_at=clock + timedelta(hours=1) if index == 5 else clock,
                        created_at=clock - timedelta(hours=4)
                        if index == 8
                        else clock - timedelta(minutes=2),
                        updated_at=clock - timedelta(minutes=6) if index == 3 else clock,
                        error_category=category + str(index) if index in (6, 7) else None,
                    )
                )
            await session.flush()
        finally:
            await session.execute(text("PRAGMA ignore_check_constraints=OFF"))
    assert await jobs.batch_health(trigger_count=3, max_characters=1000, max_wait_seconds=3600) == {
        "pending_events": 5,
        "processing_events": 2,
        "failed_events": 2,
        "stale_processing_events": 1,
        "oldest_open_age_seconds": 14400,
        "ready_owner_count": 2,
        "waiting_owner_count": 1,
        "normal_waiting_is_blocked": False,
        "invalid_owner_count": 1,
        "failure_categories": {category: 2},
    }


async def _reclaim(jobs: MemoryJobRepository, monkeypatch: pytest.MonkeyPatch) -> MemoryJob:
    # Advance only the queue's clock: do not alter the original claimant's row/token.
    with monkeypatch.context() as patch:
        patch.setattr("yuki.memory.repository.datetime", _ReclaimTime)
        (claimed,) = await jobs.claim()
    return claimed


async def _snapshot(database: Database, job: MemoryJob) -> tuple[Any, ...]:
    async with database.read_session() as session:
        row = await session.get(MemoryJobModel, job.id)
        assert row is not None
        return (
            row.status,
            row.updated_at,
            row.attempts,
            row.next_attempt_at,
            row.outcome,
            row.error_category,
            row.completed_at,
        )


async def test_actorless_context_is_rejected_by_queue_mutation_without_writing(
    database: Database,
) -> None:
    mutations, _facts, ledger, processor = _service(database, self_memory_enabled=True)
    event = await _append_event(ledger, message_id="actorless-queue-mutation")
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(event.id, "private:1001")
    (job,) = await jobs.claim()
    validated = processor.validate(_claim(), event)
    assert validated is not None
    before = await _snapshot(database, job)

    result = await mutations.mutate_validated_claim(
        validated,
        MemoryProcessingContext(event=None),
        conversation_key=job.conversation_key,
        job=job,
    )

    assert not result.ok
    assert result.reason_code == "untrusted_trigger_event"
    assert await _snapshot(database, job) == before
    async with database.read_session() as session:
        for model in (MemoryFactModel, MemoryMutationReceiptModel, MemoryClaimCandidateModel):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("late_action", ["complete", "fail"])
@pytest.mark.parametrize("new_owner_done", [False, True])
async def test_stale_queue_transition_cannot_overwrite_new_owner(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    late_action: str,
    new_owner_done: bool,
) -> None:
    jobs = MemoryJobRepository(database)
    event = await _append_event(EventLedgerRepository(database), message_id="claim-state")
    assert await jobs.enqueue(event.id, "private:1001")
    (old,) = await jobs.claim()
    current = await _reclaim(jobs, monkeypatch)
    assert old.updated_at != current.updated_at
    if new_owner_done:
        await jobs.complete(current, outcome=MemoryRebuildJobOutcome.NO_CLAIMS)
    before = await _snapshot(database, current)
    with pytest.raises(MemoryJobClaimLost):
        if late_action == "complete":
            await jobs.complete(old)
        else:
            await jobs.fail(old, "LateError")
    assert await _snapshot(database, current) == before
    if not new_owner_done:
        await jobs.fail(current, "CurrentError")
        after = await _snapshot(database, current)
        assert after[0] == "pending" and after[2] == 1 and after[5] == "CurrentError"


@pytest.mark.parametrize("result_kind", ["fact", "low_confidence_fact", "no_claims", "failure"])
async def test_reclaim_during_extraction_discards_all_old_worker_results(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    result_kind: str,
) -> None:
    ledger = EventLedgerRepository(database)
    event = await _append_event(ledger, message_id="claim-extraction")
    jobs = MemoryJobRepository(database)
    entered, release = asyncio.Event(), asyncio.Event()

    class Provider(LLMProvider):
        async def complete(self, request: ChatRequest) -> ChatResponse:
            entered.set()
            await release.wait()
            if result_kind == "failure":
                raise KeyError("late provider failure")
            claims = (
                ()
                if result_kind == "no_claims"
                else (
                    BatchMemoryClaim(
                        source_event_id=event.id,
                        claim=_claim(
                            confidence=0.5 if result_kind == "low_confidence_fact" else 0.9
                        ),
                    ),
                )
            )
            return ChatResponse(
                content=BatchMemoryExtractionOutput(claims=claims).model_dump_json(),
                latency_seconds=0,
            )

    worker = MemoryWorker(
        settings=make_settings(database.url, memory_batch_max_wait_seconds=0),
        jobs=jobs,
        facts=MemoryFactService(MemoryFactRepository(database)),
        ledger=ledger,
        model_executor=InjectedModelExecutor(Provider()),
        concurrency=ConcurrencyManager(),
    )
    assert await worker.enqueue(event.id, "private:1001")
    running = asyncio.create_task(worker.process_once())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        current = await asyncio.wait_for(_reclaim(jobs, monkeypatch), timeout=5)
        before = await _snapshot(database, current)
    finally:
        release.set()
    assert await asyncio.wait_for(running, timeout=5) == 0
    assert await _snapshot(database, current) == before
    async with database.read_session() as session:
        for model in (MemoryFactModel, MemoryMutationReceiptModel, MemoryClaimCandidateModel):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_reclaim_after_extraction_blocks_fact_and_receipt_without_writer_lock(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = EventLedgerRepository(database)
    event = await _append_event(ledger, message_id="claim-resolution")
    jobs = MemoryJobRepository(database)

    class Provider(LLMProvider):
        async def complete(self, request: ChatRequest) -> ChatResponse:
            return ChatResponse(
                content=BatchMemoryExtractionOutput(
                    claims=(BatchMemoryClaim(source_event_id=event.id, claim=_claim()),)
                ).model_dump_json(),
                latency_seconds=0,
            )

    worker = MemoryWorker(
        settings=make_settings(database.url, memory_batch_max_wait_seconds=0),
        jobs=jobs,
        facts=MemoryFactService(MemoryFactRepository(database)),
        ledger=ledger,
        model_executor=InjectedModelExecutor(Provider()),
        concurrency=ConcurrencyManager(),
    )
    original_mutate = worker.mutations.mutate_validated_claim
    current: MemoryJob | None = None

    async def mutate_after_reclaim(*args: Any, **kwargs: Any) -> Any:
        nonlocal current
        # Another writer can reclaim the job after paid extraction, before mutation.
        current = await asyncio.wait_for(_reclaim(jobs, monkeypatch), timeout=5)
        return await original_mutate(*args, **kwargs)

    monkeypatch.setattr(worker.mutations, "mutate_validated_claim", mutate_after_reclaim)
    assert await worker.enqueue(event.id, "private:1001")
    assert await worker.process_once() == 0
    assert current is not None
    assert (await _snapshot(database, current))[0:3:2] == ("processing", 0)
    async with database.read_session() as session:
        for model in (MemoryFactModel, MemoryMutationReceiptModel):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
