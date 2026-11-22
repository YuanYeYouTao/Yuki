"""Evidence preparation must not hold a SQLite writer or duplicate a claim."""

import asyncio
import importlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, update
from tests.conftest import make_settings
from tests.support.canonical_ingress import append_user_event
from tests.support.model_executor import InjectedModelExecutor

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.messages import ChatRequest, ChatResponse
from yuki.llm.base import LLMProvider, LLMUnavailableError
from yuki.memory.extraction import BatchMemoryExtractionOutput
from yuki.memory.repository import MemoryFactRepository, MemoryJobRepository
from yuki.memory.service import MemoryFactService
from yuki.memory.worker import MemoryWorker
from yuki.persistence.models import ChatEventModel, MemoryJobModel
from yuki.persistence.repositories import EventLedgerRepository
from yuki.services.concurrency import ConcurrencyManager


@pytest.mark.parametrize("kind", ["memory", "memory_batch"])
async def test_memory_claim_lease_starts_after_waiting_for_the_writer(database, monkeypatch, kind):
    identity = await append_user_event(database, message_id="queued-claim-clock")
    repository = MemoryJobRepository(database)
    assert await repository.enqueue(identity, "private:1001")
    module = importlib.import_module("yuki.memory.repository")
    uow = importlib.import_module("yuki.persistence.unit_of_work")
    instant = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(uow, "datetime", Clock)
    prepared = asyncio.Event()
    original = module.commit_job_claims

    async def prepared_claim(*args):
        prepared.set()
        return await original(*args)

    monkeypatch.setattr(module, "commit_job_claims", prepared_claim)

    async def claim():
        if kind == "memory_batch":
            return await repository.claim_ready_batch(
                limit=10, trigger_count=1, max_characters=8000, max_wait_seconds=3600
            )
        return await repository.claim()

    async with database.immediate_session():
        pending = asyncio.create_task(claim())
        await asyncio.wait_for(prepared.wait(), 3)
        instant += timedelta(minutes=6)
    (job,) = await pending
    assert job.updated_at.replace(tzinfo=UTC) == instant
    assert not await claim(), "a freshly admitted claim must not already be stale"
    await repository.complete(job)
    async with database.read_session() as reader:
        persisted = await reader.get(MemoryJobModel, job.id)
        assert persisted.status == "done" and persisted.attempts == job.attempts == 0
        assert persisted.event_id == identity


@pytest.mark.parametrize("kind", ["memory", "memory_batch"])
@pytest.mark.parametrize("stale_processing", [False, True])
async def test_claim_prepares_without_writer_and_competing_claims_are_disjoint(
    database, monkeypatch, kind, stale_processing
):
    ids = [await append_user_event(database, message_id=f"claim-{i}") for i in range(3)]
    repository = MemoryJobRepository(database)
    for identity in ids:
        assert await repository.enqueue(identity, "private:1001")
    module = importlib.import_module("yuki.memory.repository")

    if stale_processing:
        async with database.immediate_session() as session:
            await session.execute(
                update(MemoryJobModel).values(
                    status="processing", updated_at=datetime.now(UTC) - timedelta(minutes=6)
                )
            )

    writers = set()

    def observe(conn, cursor, sql, params, context, many):
        normalized = sql.lstrip().upper()
        if normalized.startswith(("UPDATE", "INSERT", "DELETE")):
            writers.add(id(conn))
        if normalized.startswith("SELECT") and "CHAT_EVENTS" in normalized:
            assert id(conn) not in writers, "evidence SELECT after acquiring the writer"

    def clear(conn):
        writers.discard(id(conn))

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", observe)
    event.listen(engine, "commit", clear)
    event.listen(engine, "rollback", clear)
    original = module.commit_job_claims
    ready = asyncio.Event()
    arrivals = 0

    async def overlap(*args):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 3)
        return await original(*args)

    monkeypatch.setattr(module, "commit_job_claims", overlap)

    async def claim():
        if kind == "memory_batch":
            return await repository.claim_ready_batch(
                limit=10, trigger_count=1, max_characters=8000, max_wait_seconds=3600
            )
        return await repository.claim(limit=10)

    try:
        first, second = await asyncio.gather(claim(), claim())
    finally:
        event.remove(engine, "before_cursor_execute", observe)
        event.remove(engine, "commit", clear)
        event.remove(engine, "rollback", clear)
    claimed = [getattr(job, "job_id", None) or job.id for job in (*first, *second)]
    assert len(claimed) == len(set(claimed)) == 3


@pytest.mark.parametrize("kind", ["memory", "memory_batch"])
@pytest.mark.parametrize("watermark", ["last_generation_change_event_id", "starts_after_event_id"])
async def test_memory_claim_rechecks_reset_between_prepare_and_commit(
    database, monkeypatch, kind, watermark
):
    identity = await append_user_event(database, message_id="reset-claim")
    repository = MemoryJobRepository(database)
    assert await repository.enqueue(identity, "private:1001")
    module = importlib.import_module("yuki.memory.repository")
    original = module.commit_job_claims

    async def reset_then_commit(*args):
        async with database.immediate_session() as session:
            source = await session.get(ChatEventModel, identity)
            values = {watermark: identity}
            await session.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == source.canonical_conversation_id)
                .values(**values)
            )
        return await original(*args)

    monkeypatch.setattr(module, "commit_job_claims", reset_then_commit)
    claimed = (
        await repository.claim()
        if kind == "memory"
        else await repository.claim_ready_batch(
            limit=10, trigger_count=1, max_characters=8000, max_wait_seconds=3600
        )
    )
    assert not claimed
    async with database.read_session() as session:
        job = await session.get(MemoryJobModel, 1)
        assert job is not None and job.status == "pending"


async def test_memory_worker_recovers_original_job_after_repeated_provider_failure(database):
    identity = await append_user_event(database, message_id="memory-repeated-provider-failure")
    repository = MemoryJobRepository(database)

    class Provider(LLMProvider):
        calls = 0

        async def complete(self, request: ChatRequest) -> ChatResponse:
            self.calls += 1
            if self.calls <= 4:
                raise LLMUnavailableError("temporary provider failure")
            return ChatResponse(
                content=BatchMemoryExtractionOutput().model_dump_json(), latency_seconds=0
            )

    provider = Provider()
    worker = MemoryWorker(
        settings=make_settings(database.url, memory_batch_max_wait_seconds=0),
        jobs=repository,
        facts=MemoryFactService(MemoryFactRepository(database)),
        ledger=EventLedgerRepository(database),
        model_executor=InjectedModelExecutor(provider),
        concurrency=ConcurrencyManager(),
    )
    assert await worker.enqueue(identity, "private:1001")
    for attempts in range(1, 5):
        assert await worker.process_once() == 0
        async with database.read_session() as session:
            job = await session.get(MemoryJobModel, 1)
            assert job is not None and job.event_id == identity
            assert job.status == "pending" and job.attempts == attempts
            assert job.error_category == "LLMUnavailableError"
            assert job.next_attempt_at.replace(tzinfo=UTC) > datetime.now(UTC)
        # Advance readiness only; the next activation must reclaim the original ID.
        async with database.immediate_session() as session:
            await session.execute(
                update(MemoryJobModel)
                .where(MemoryJobModel.id == 1)
                .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    assert await worker.process_once() == 1
    async with database.read_session() as session:
        job = await session.get(MemoryJobModel, 1)
        assert job is not None and job.event_id == identity and job.attempts == 4
        assert job.status == "done" and job.outcome == "no_claims"
        assert job.error_category is None and job.completed_at is not None
    assert provider.calls == 5
