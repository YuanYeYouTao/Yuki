"""Real SQLite batching and original embedding-claim fences."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, insert, select, update

from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.memory.embedding.jobs import EmbeddingWrite, MemoryEmbeddingJobRepository
from qq_ai_bot.memory.embedding.models import EmbeddingProviderProfile
from qq_ai_bot.memory.embedding.repository import MemoryEmbeddingRepository
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder
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


@pytest.mark.parametrize("count", [1, 20, 129])
async def test_complete_pages_two_reads_hashes_outside_writer(database, monkeypatch, count):
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
        assert sum(sql.lstrip().upper().startswith("SELECT") for sql in statements) == 2 * (
            (count + 127) // 128
        )
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
        await jobs.fail(
            old, error_category="late", retryable=False, max_attempts=3, initial_delay_seconds=0
        )
    async with database.sessions() as session:
        stored = await session.get(MemoryEmbeddingJobModel, current.id)
        assert stored.status == "processing" and stored.updated_at == current.updated_at
        assert stored.attempts == current.attempts
        assert not tuple(await session.scalars(select(MemoryEmbeddingModel)))
    assert await jobs.complete((_write(current),)) == 1


async def test_ordinary_reconcile_preserves_failed_budget_and_content_change_requeues(database):
    jobs = await _jobs(database)
    await jobs.reconcile()
    (job,) = await jobs.claim(limit=1)
    await jobs.fail(
        job, error_category="permanent", retryable=False, max_attempts=1, initial_delay_seconds=0
    )
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
    await jobs.fail(
        old, error_category="permanent", retryable=False, max_attempts=1, initial_delay_seconds=0
    )
    assert await jobs.retry_failed() == 1
    # An explicit retry resets its policy budget but must advance claim identity.
    assert await jobs.claim(limit=1) == ()
    clock += timedelta(seconds=1)
    (current,) = await jobs.claim(limit=1)
    assert current.attempts == 1 and current.updated_at > old.updated_at
    assert await jobs.complete((_write(old),)) == 0
    assert await jobs.complete((_write(current),)) == 1


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
