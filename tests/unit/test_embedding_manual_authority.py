"""Manual embedding queue changes retain their original Person through every batch."""

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select, update
from tests.unit.test_embedding_claim_boundaries import _jobs
from tests.unit.test_memory_rebuild import _admin

from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.models import MemoryEmbeddingJobModel


async def test_manual_reconcile_stops_after_role_revoked_between_pages(database, monkeypatch):
    jobs = await _jobs(database, 129)
    actor = await _admin(database)
    enqueue = jobs._enqueue_prepared
    calls = 0

    async def enqueue_and_revoke(*args, **kwargs):
        nonlocal calls
        count = await enqueue(*args, **kwargs)
        calls += 1
        async with database.immediate_session() as writer:
            (await writer.get(CanonicalPersonModel, actor)).is_superuser = False
        return count

    monkeypatch.setattr(jobs, "_enqueue_prepared", enqueue_and_revoke)
    with pytest.raises(PermissionError, match="current_superuser"):
        await jobs.reconcile(force=True, actor_person_id=actor)
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryEmbeddingJobModel)) == 128
    assert calls == 1
    # Normal system reconciliation owns the index independently of a requesting Person.
    monkeypatch.setattr(jobs, "_enqueue_prepared", enqueue)
    assert await jobs.reconcile() == 1


async def test_manual_retry_checks_role_inside_original_writer(database, monkeypatch):
    jobs = await _jobs(database)
    actor = await _admin(database)
    assert await jobs.reconcile() == 1
    async with database.immediate_session() as writer:
        await writer.execute(update(MemoryEmbeddingJobModel).values(status="failed", attempts=3))
    immediate_session = database.immediate_session

    @asynccontextmanager
    async def revoke_before_writer():
        async with immediate_session() as writer:
            (await writer.get(CanonicalPersonModel, actor)).is_superuser = False
        async with immediate_session() as writer:
            yield writer

    with monkeypatch.context() as revoked:
        revoked.setattr(database, "immediate_session", revoke_before_writer)
        with pytest.raises(PermissionError, match="current_superuser"):
            await jobs.retry_failed(actor_person_id=actor)
    async with database.read_session() as reader:
        job = await reader.scalar(select(MemoryEmbeddingJobModel))
        assert job.status == "failed" and job.attempts == 3
