"""One old-profile delete owns its current Person check and FK cascade."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select
from tests.conftest import make_settings
from tests.support.canonical_identity import ensure_person
from tests.support.person_merge import merge_persons
from tests.unit.test_embedding_claim_boundaries import _jobs
from tests.unit.test_memory_mutation import _service
from tests.unit.test_memory_rebuild import _admin

from yuki.admin.audit import AdminAuditService
from yuki.admin.models import AdminActor
from yuki.identity.db_models import CanonicalPersonModel
from yuki.memory.embedding.fake import FakeEmbeddingProvider
from yuki.memory.embedding.runtime import MemoryEmbeddingRuntime
from yuki.memory.fts import SQLiteMemoryFTSIndex
from yuki.memory.retrieval import MemoryRetriever
from yuki.persistence.models import (
    MemoryEmbeddingJobModel,
    MemoryEmbeddingModel,
    MemoryEmbeddingProfileModel,
)
from yuki.services.admin.memory_admin import MemoryAdminService


async def scene(database):
    old = await _jobs(database)
    await old.reconcile()
    mutations, facts, ledger, _ = _service(database)
    settings = make_settings(database.url)
    settings.memory_embedding_enabled = True
    settings.memory_embedding_worker_enabled = False
    runtime = MemoryEmbeddingRuntime(
        settings=settings,
        database=database,
        facts=facts,
        retriever=MemoryRetriever(
            repository=facts.repository, lexical_index=SQLiteMemoryFTSIndex(database)
        ),
        provider=FakeEmbeddingProvider(dimensions=2),
    )
    await runtime.start()
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        for job in await writer.scalars(select(MemoryEmbeddingJobModel)):
            writer.add(
                MemoryEmbeddingModel(
                    fact_id=job.fact_id,
                    profile_id=job.profile_id,
                    content_hash=job.content_hash,
                    vector_blob=b"original-vector",
                    created_at=now,
                    updated_at=now,
                )
            )
    owner = await _admin(database)
    admin = MemoryAdminService(
        settings=settings,
        memories=facts,
        audit=AdminAuditService(database),
        mutations=mutations,
        ledger=ledger,
        memory_embeddings=runtime,
    )
    actor = AdminActor("9000", True, "purge", "private:9000", person_id=owner)
    return runtime, admin, actor


async def counts(database):
    async with database.read_session() as reader:
        values = []
        for model in (MemoryEmbeddingProfileModel, MemoryEmbeddingJobModel, MemoryEmbeddingModel):
            values.append(await reader.scalar(select(func.count()).select_from(model)))
        return tuple(values)


async def test_profile_purge_rolls_back_then_atomically_cascades_old_profiles(database):
    runtime, admin, actor = await scene(database)

    def fail_after_delete(_conn, _cursor, sql, *_args):
        if sql.startswith("DELETE FROM memory_embedding_profiles"):
            raise RuntimeError("original profile delete failure")

    try:
        event.listen(database.engine.sync_engine, "after_cursor_execute", fail_after_delete)
        try:
            with pytest.raises(RuntimeError, match="original profile delete failure"):
                await admin.embedding_purge_old(actor)
        finally:
            event.remove(database.engine.sync_engine, "after_cursor_execute", fail_after_delete)
        assert await counts(database) == (2, 2, 2)
        assert await admin.embedding_purge_old(actor) == 1
        assert await counts(database) == (1, 1, 1)
        async with database.read_session() as reader:
            for model in (MemoryEmbeddingJobModel, MemoryEmbeddingModel):
                assert (await reader.scalar(select(model))).profile_id == runtime.profile_id
        assert await admin.embedding_purge_old(actor) == 0
    finally:
        await runtime.close()


async def test_revoke_ahead_in_fifo_prevents_original_actor_purge(database, monkeypatch):
    runtime, admin, actor = await scene(database)
    entered = asyncio.Event()
    purge = runtime.repository.purge_old_profiles

    async def record_original_actor(**kwargs):
        assert kwargs["actor_person_id"] == actor.person_id
        entered.set()
        return await purge(**kwargs)

    monkeypatch.setattr(runtime.repository, "purge_old_profiles", record_original_actor)

    async def revoke():
        async with database.immediate_session() as writer:
            (await writer.get(CanonicalPersonModel, actor.person_id)).is_superuser = False

    try:
        async with database.immediate_session():
            revocation = asyncio.create_task(revoke())
            await asyncio.sleep(0)
            deletion = asyncio.create_task(admin.embedding_purge_old(actor))
            await asyncio.wait_for(entered.wait(), 5)
            assert not revocation.done() and not deletion.done()
        await revocation
        with pytest.raises(PermissionError, match="current_superuser"):
            await deletion
        assert await counts(database) == (2, 2, 2)
    finally:
        await runtime.close()


async def test_purge_keeps_original_person_qualification_after_same_human_merge(database):
    runtime, admin, actor = await scene(database)
    try:
        async with database.immediate_session() as writer:
            target = await ensure_person(writer, "new-admin-entry")
            await merge_persons(writer, actor.person_id, target)
            (await writer.get(CanonicalPersonModel, actor.person_id)).enabled = False
        with pytest.raises(PermissionError):
            await admin.embedding_purge_old(actor)
        assert await counts(database) == (2, 2, 2)
        async with database.immediate_session() as writer:
            (await writer.get(CanonicalPersonModel, actor.person_id)).enabled = True
        assert await admin.embedding_purge_old(actor) == 1
        assert await counts(database) == (1, 1, 1)
    finally:
        await runtime.close()
