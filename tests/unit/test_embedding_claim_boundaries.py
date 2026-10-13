"""Real SQLite batching and original embedding-claim fences."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, insert, select, update
from sqlalchemy.exc import OperationalError

from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.memory.authorized_scope import AuthorizedMemoryScope
from qq_ai_bot.memory.embedding.codec import Float32VectorCodec
from qq_ai_bot.memory.embedding.fake import FakeEmbeddingProvider
from qq_ai_bot.memory.embedding.jobs import EmbeddingWrite, MemoryEmbeddingJobRepository
from qq_ai_bot.memory.embedding.models import EmbeddingProviderProfile, EmbeddingVector
from qq_ai_bot.memory.embedding.provider import EmbeddingProviderError
from qq_ai_bot.memory.embedding.repository import MemoryEmbeddingRepository
from qq_ai_bot.memory.embedding.semantic import MemorySemanticIndex
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder
from qq_ai_bot.memory.embedding.worker import MemoryEmbeddingWorker
from qq_ai_bot.memory.enums import MemoryScopeType, MemoryTargetRole
from qq_ai_bot.memory.models import MemoryEntityTarget
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemoryEmbeddingJobModel,
    MemoryEmbeddingModel,
    MemoryFactModel,
)


async def _jobs(database: Database, count: int = 1) -> MemoryEmbeddingJobRepository:
    profile = await MemoryEmbeddingRepository(database).ensure_profile(
        EmbeddingProviderProfile(
            provider_id="fake",
            model_id="test",
            dimensions=2,
            document_template_version=1,
            endpoint_identity="https://example.invalid/embedding",
        )
    )
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        person = await ensure_person(session, "1001")
        await session.execute(
            insert(MemoryFactModel),
            [
                dict(
                    scope_type="person",
                    canonical_subject_person_id=person,
                    kind="fact",
                    category="profile",
                    memory_key=f"test:{index}",
                    content=f"fact {index}",
                    normalized_content=f"fact {index}",
                    source_type="explicit",
                    authority="explicit",
                    status="active",
                    conflict_state="clear",
                    review_state="verified",
                    created_at=now,
                    updated_at=now,
                )
                for index in range(count)
            ],
        )
    return MemoryEmbeddingJobRepository(
        database,
        profile=profile,
        documents=EmbeddingDocumentBuilder(template_version=1, max_characters=4000),
    )


def _write(job):
    return EmbeddingWrite(
        job_id=job.id,
        fact_id=job.fact_id,
        content_hash=job.content_hash,
        vector_blob=b"vector",
        claimed_at=job.updated_at,
    )


async def test_semantic_order_and_streamed_health_keep_stale_and_scan_coverage(
    database, monkeypatch
):
    jobs = await _jobs(database, 5)
    repository = MemoryEmbeddingRepository(database)
    codec = Float32VectorCodec()
    vectors = ((1.0, 0.0), (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (1.0, 1.0))
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        facts = tuple(await session.scalars(select(MemoryFactModel).order_by(MemoryFactModel.id)))
        for fact, values in zip(facts, vectors, strict=True):
            digest = jobs.documents.content_hash_fields(
                kind=fact.kind,
                category=fact.category,
                memory_key=fact.memory_key,
                content=fact.content,
            )
            session.add(
                MemoryEmbeddingModel(
                    fact_id=fact.id,
                    profile_id=jobs.profile.id,
                    content_hash="f" * 64 if fact.id == facts[-1].id else digest,
                    vector_blob=codec.encode(EmbeddingVector(values=values, dimensions=2)),
                    created_at=now,
                    updated_at=now,
                )
            )
        facts[3].status = "superseded"
    scope = AuthorizedMemoryScope(
        requester_person_id=facts[0].canonical_subject_person_id,
        allowed_scopes=(MemoryScopeType.PERSON,),
    )
    query = EmbeddingVector(values=(0.1, 0.2), dimensions=2)
    index = MemorySemanticIndex(repository, documents=jobs.documents)
    results, truncated, coverage = await index.search_authorized(
        scope=scope,
        query_vector=query,
        profile=jobs.profile.profile,
        profile_id=jobs.profile.id,
        candidate_limit=2,
        kinds=(),
    )
    assert [hit.fact_id for hit in results] == [facts[2].id, facts[0].id]
    assert truncated and not coverage
    for hit in results:
        values = vectors[hit.fact_id - facts[0].id]
        decoded = codec.decode(
            codec.encode(EmbeddingVector(values=values, dimensions=2)), dimensions=2
        )
        assert hit.cosine_similarity == codec.dot(query, decoded)
    target = MemoryEntityTarget(
        role=MemoryTargetRole.CURRENT_PERSON,
        scope_type=MemoryScopeType.PERSON,
        subject_user_id="1001",
        block_id="person:1001",
    )
    explicit = await index.search(
        target=target,
        query_vector=query,
        profile=jobs.profile.profile,
        profile_id=jobs.profile.id,
        candidate_limit=3,
        kinds=(),
    )
    assert [hit.fact_id for hit in explicit] == [facts[2].id, facts[0].id, facts[1].id]
    rows, scanned_out, active_count = await repository.load_authorized_vectors(
        scope=scope,
        profile_id=jobs.profile.id,
        kinds=(),
        scan_limit=2,
    )
    assert len(rows) == 2 and scanned_out and active_count == 4
    native_load = repository.load_authorized_vectors

    async def scan_page(**kwargs):
        return await native_load(**kwargs, scan_limit=2)

    monkeypatch.setattr(repository, "load_authorized_vectors", scan_page)
    scanned, truncated, coverage = await index.search_authorized(
        scope=scope,
        query_vector=query,
        profile=jobs.profile.profile,
        profile_id=jobs.profile.id,
        candidate_limit=3,
        kinds=(),
    )
    assert [hit.fact_id for hit in scanned] == [facts[0].id, facts[1].id]
    assert truncated and not coverage
    health = await repository.local_health(
        current_profile_id=jobs.profile.id, documents=jobs.documents
    )
    assert health["active_fact_count"] == 4 and health["ready_embedding_count"] == 3
    assert health["stale_embedding_count"] == 2


@pytest.mark.parametrize("count", [1, 20, 129])
async def test_complete_pages_hashes_outside_writer(database, monkeypatch, count):
    jobs = await _jobs(database, count)
    assert await jobs.reconcile() == count
    claimed = await jobs.claim(limit=count)
    statements = []
    writer = False
    original = jobs.documents.content_hash_fields

    def trace(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal writer
        if statement.startswith("BEGIN IMMEDIATE"):
            writer = True
        statements.append(statement)

    def committed(_connection):
        nonlocal writer
        writer = False

    def hash_fields(**values):
        assert not writer
        return original(**values)

    event.listen(database.engine.sync_engine, "before_cursor_execute", trace)
    event.listen(database.engine.sync_engine, "commit", committed)
    monkeypatch.setattr(jobs.documents, "content_hash_fields", hash_fields)
    try:
        assert await jobs.complete(tuple(_write(job) for job in claimed)) == count
        statements.clear()
        assert await jobs.reconcile() == 0
        assert not any("BEGIN IMMEDIATE" in sql for sql in statements)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", trace)
        event.remove(database.engine.sync_engine, "commit", committed)


@pytest.mark.parametrize("late_action", ["complete", "fail", "skip"])
async def test_late_original_claim_never_overwrites_reclaimed_job(database, late_action):
    jobs = await _jobs(database)
    await jobs.reconcile()
    (old,) = await jobs.claim(limit=1)
    assert await jobs.reconcile() == 0
    assert await jobs.recover_interrupted() == 1
    (current,) = await jobs.claim(limit=1)
    assert current.attempts == old.attempts + 1
    if late_action == "complete":
        assert await jobs.complete((_write(old),)) == 0
    elif late_action == "skip":
        await jobs.skip(old)
    else:
        await jobs.fail(old, error_category="late", retryable=False, initial_delay_seconds=0)
    async with database.sessions() as session:
        stored = await session.get(MemoryEmbeddingJobModel, current.id)
        assert stored.status == "processing" and stored.updated_at == current.updated_at
        assert stored.attempts == current.attempts
        assert not tuple(await session.scalars(select(MemoryEmbeddingModel)))
    assert await jobs.complete((_write(current),)) == 1


@pytest.mark.parametrize("action", ["fail", "skip"])
async def test_batch_failure_and_skip_leave_reclaimed_peer_processing(database, action):
    jobs = await _jobs(database, 3)
    await jobs.reconcile()
    old = await jobs.claim(limit=3)
    await jobs.recover_interrupted()
    current = await jobs.claim(limit=3)
    mixed = (old[0], *current[1:])
    if action == "skip":
        await jobs.skip_many(mixed)
    else:
        await jobs.fail_many(
            mixed, error_category="batch_failure", retryable=False, initial_delay_seconds=0
        )
    async with database.sessions() as session:
        rows = {row.id: row for row in await session.scalars(select(MemoryEmbeddingJobModel))}
    assert rows[current[0].id].status == "processing"
    assert rows[current[0].id].updated_at == current[0].updated_at
    assert rows[current[0].id].attempts == current[0].attempts
    for claim in current[1:]:
        assert rows[claim.id].status == ("done" if action == "skip" else "failed")
        assert rows[claim.id].updated_at > claim.updated_at
    assert await jobs.complete((_write(current[0]),)) == 1


async def test_ordinary_reconcile_preserves_failed_claim_and_content_change_requeues(database):
    jobs = await _jobs(database)
    await jobs.reconcile()
    (job,) = await jobs.claim(limit=1)
    await jobs.fail(job, error_category="permanent", retryable=False, initial_delay_seconds=0)
    assert await jobs.reconcile() == 0
    async with database.immediate_session() as session:
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == job.fact_id)
            .values(content="new value", updated_at=datetime.now(UTC))
        )
    assert await jobs.reconcile() == 1
    (fresh,) = await jobs.claim(limit=1)
    assert fresh.content_hash != job.content_hash and fresh.attempts == 1
    assert await jobs.complete((_write(job),)) == 0
    assert await jobs.complete((_write(fresh),)) == 1


async def test_explicit_retry_cannot_reuse_claim_timestamp_when_clock_stalls(database, monkeypatch):
    jobs = await _jobs(database)
    await jobs.reconcile()
    clock = datetime.now(UTC) + timedelta(seconds=10)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    monkeypatch.setattr("qq_ai_bot.memory.embedding.jobs.datetime", FixedClock)
    (old,) = await jobs.claim(limit=1)
    await jobs.fail(old, error_category="permanent", retryable=False, initial_delay_seconds=0)
    assert await jobs.retry_failed() == 1
    # An explicit retry resets its attempt count but must advance claim identity.
    assert await jobs.claim(limit=1) == ()
    clock += timedelta(seconds=1)
    (current,) = await jobs.claim(limit=1)
    assert current.attempts == 1 and current.updated_at > old.updated_at
    assert await jobs.complete((_write(old),)) == 0
    assert await jobs.complete((_write(current),)) == 1


async def test_retryable_embedding_failure_recovers_original_job_past_old_attempt_limit(database):
    jobs = await _jobs(database)
    await jobs.reconcile()
    provider = FakeEmbeddingProvider(
        dimensions=2,
        error=EmbeddingProviderError("unavailable", "temporary failure", retryable=True),
    )
    worker = MemoryEmbeddingWorker(
        provider=provider,
        jobs=jobs,
        interval_seconds=1,
        claim_limit=1,
        retry_initial_seconds=0,
    )
    for attempts in range(1, 7):
        assert await worker.process_once() == 0
        async with database.sessions() as reader:
            stored = await reader.get(MemoryEmbeddingJobModel, 1)
            assert stored.status == "pending" and stored.attempts == attempts
            assert stored.error_category == "unavailable"
    provider._error = None
    assert await worker.process_once() == 1
    async with database.sessions() as reader:
        stored = await reader.get(MemoryEmbeddingJobModel, 1)
        assert stored.status == "done" and stored.attempts == 7
        assert len(tuple(await reader.scalars(select(MemoryEmbeddingModel)))) == 1
    assert provider.document_requests == 7
    assert await worker.process_once() == 0


@pytest.mark.parametrize("cancelled", [False, True])
async def test_embedding_loop_database_failure_and_cancellation(database, monkeypatch, cancelled):
    jobs = await _jobs(database)
    await jobs.reconcile()
    provider = FakeEmbeddingProvider(dimensions=2)
    worker = MemoryEmbeddingWorker(
        provider=provider,
        jobs=jobs,
        interval_seconds=0.001,
        claim_limit=1,
        retry_initial_seconds=0,
    )
    claim = jobs.claim
    attempts = 0

    async def interrupted_claim(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if cancelled:
                raise asyncio.CancelledError
            raise OperationalError("claim", {}, RuntimeError("database unavailable"))
        worker._stop.set()
        return await claim(**kwargs)

    monkeypatch.setattr(jobs, "claim", interrupted_claim)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await worker._run()
        assert provider.document_requests == 0
    else:
        await asyncio.wait_for(worker._run(), timeout=2)
        assert attempts == 2 and provider.document_requests == 1
        async with database.sessions() as reader:
            stored = await reader.get(MemoryEmbeddingJobModel, 1)
            assert stored.status == "done"


async def test_input_mutation_after_prepare_requeues_original_claim_without_old_vector(
    database, monkeypatch
):
    jobs = await _jobs(database)
    await jobs.reconcile()
    (job,) = await jobs.claim(limit=1)
    original = database.immediate_session
    changed = False

    @asynccontextmanager
    async def gate():
        nonlocal changed
        if not changed:
            changed = True
            async with original() as writer:
                await writer.execute(
                    update(MemoryFactModel)
                    .where(MemoryFactModel.id == job.fact_id)
                    .values(content="new value", updated_at=datetime.now(UTC))
                )
        async with original() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", gate)
    assert await jobs.complete((_write(job),)) == 0
    async with database.sessions() as reader:
        stored = await reader.get(MemoryEmbeddingJobModel, job.id)
        assert stored.status == "pending" and stored.attempts == job.attempts
        assert stored.error_category == "embedding_input_changed"
        assert not tuple(await reader.scalars(select(MemoryEmbeddingModel)))
