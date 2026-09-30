"""Dream owner preparation stays readonly and run creation is a complete atomic plan."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import IntegrityError
from tests.conftest import make_settings
from tests.unit.test_memory_dream import _empty_dream_statistics, _fact_with_evidence, _services

from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.memory.dream.db_models import (
    MemoryDreamClusterModel,
    MemoryDreamOperationModel,
    MemoryDreamRunModel,
)
from qq_ai_bot.memory.dream.models import DreamOperationType, DreamRunMode, DreamRunStatus
from qq_ai_bot.memory.dream.repository import DreamCandidate, DreamCandidateLoad, fact_signature
from qq_ai_bot.memory.dream.service import plan_full_core, prepare_full_core
from qq_ai_bot.memory.embedding.models import EmbeddingVector
from qq_ai_bot.persistence.models import MemoryFactModel


async def sources(database):
    _, facts, ledger, dreams = _services(database)
    rows = tuple(
        [
            await _fact_with_evidence(
                facts,
                ledger,
                message_id=f"plan-{index}",
                memory_key=f"plan:{index}",
                content=f"prepared source {index}",
            )
            for index in range(2)
        ]
    )
    return dreams, rows


def specs(rows, count=1):
    return tuple(
        (
            f"cluster-{index}",
            "partition",
            "8000",
            "fact",
            tuple(row.id for row in rows),
            f"fp-{index}",
        )
        for index in range(count)
    )


async def create(dreams, clusters, *, session=None, mode=DreamRunMode.FULL):
    return await dreams.create_run(
        mode=mode,
        statistics=_empty_dream_statistics(),
        clusters=clusters,
        snapshot_max_fact_id=1000,
        actor_user_id=None,
        scheduled_slot=None,
        session=session,
    )


@pytest.mark.parametrize("count", [1, 5, 10, 300])
async def test_owner_queries_are_batched_and_no_source_reads_follow_run_insert(database, count):
    dreams, rows = await sources(database)
    dreams._facts.get_fact = AsyncMock(side_effect=AssertionError("full fact read during plan"))
    statements = []

    def capture(_connection, _cursor, sql, _parameters, _context, many):
        statements.append((sql, many))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        run = await create(dreams, specs(rows, count))
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert run.status is DreamRunStatus.PLANNED
    selects = [sql for sql, _ in statements if sql.startswith("SELECT")]
    assert len(selects) == 2  # readonly prepare + bounded version/shape recheck
    assert all(
        "memory_evidence" not in sql and "memory_facts.content" not in sql for sql in selects
    )
    first_insert = next(
        index for index, (sql, _) in enumerate(statements) if sql.startswith("INSERT")
    )
    assert not any(sql.startswith("SELECT") for sql, _ in statements[first_insert:])
    inserts = [
        (sql, many)
        for sql, many in statements
        if sql.startswith("INSERT INTO memory_dream_clusters")
    ]
    assert len(inserts) == (count + 255) // 256
    if count > 1:
        assert all(many for _, many in inserts)
    async with database.sessions() as reader:
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryDreamClusterModel)) == count
        )


@pytest.mark.parametrize("change", ["version", "owner", "delete"])
async def test_prepared_source_change_rejected_without_half_run_in_shared_session(database, change):
    dreams, rows = await sources(database)
    prepared = await dreams.prepare_clusters(specs(rows))
    async with database.sessions() as writer, writer.begin():
        if change == "delete":
            await writer.execute(delete(MemoryFactModel).where(MemoryFactModel.id == rows[0].id))
        elif change == "owner":
            person = await ensure_person(writer, "1002", display_name="other owner")
            await writer.execute(
                update(MemoryFactModel)
                .where(MemoryFactModel.id == rows[0].id)
                .values(canonical_subject_person_id=person)
            )
        else:
            await writer.execute(
                update(MemoryFactModel)
                .where(MemoryFactModel.id == rows[0].id)
                .values(updated_at=datetime.now(UTC) + timedelta(seconds=1))
            )
    async with database.sessions() as writer, writer.begin():
        with pytest.raises(ValueError, match="dream_plan_source_changed"):
            await create(dreams, prepared, session=writer)
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryDreamRunModel)) == 0
        assert await reader.scalar(select(func.count()).select_from(MemoryDreamClusterModel)) == 0


async def test_preparation_does_not_reserve_writer_and_shared_bulk_error_has_no_half_run(database):
    dreams, rows = await sources(database)
    selected, release = asyncio.Event(), asyncio.Event()
    original = dreams.prepare_clusters

    async def blocked(clusters):
        result = await original(clusters)
        selected.set()
        await release.wait()
        return result

    dreams.prepare_clusters = blocked
    task = asyncio.create_task(create(dreams, specs(rows)))
    try:
        await selected.wait()
        async with database.immediate_session() as writer:
            await writer.execute(
                update(MemoryFactModel)
                .where(MemoryFactModel.id == rows[0].id)
                .values(last_audited_at=datetime.now(UTC))
            )
        release.set()
        run = await task
        assert run.status is DreamRunStatus.PLANNED
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    prepared = await original(specs(rows))
    # A deliberately malformed prepared batch exercises savepoint cleanup after run INSERT.
    async with database.sessions() as writer, writer.begin():
        with pytest.raises(IntegrityError):
            await create(dreams, prepared + prepared, session=writer)
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryDreamRunModel)) == 1


async def test_shared_session_requires_prepared_sources(database):
    dreams, rows = await sources(database)
    prepared = await dreams.prepare_clusters(specs(rows))
    async with database.sessions() as writer, writer.begin():
        with pytest.raises(ValueError, match="prepared before opening a writer"):
            await create(dreams, specs(rows), session=writer)
        run = await create(dreams, prepared, session=writer, mode=DreamRunMode.INCREMENTAL)
        assert run.status is DreamRunStatus.RUNNING
    assert await dreams.get_run(run.public_id) is not None


async def test_full_service_plan_reuses_loaded_fact_versions_in_shared_writer(database):
    dreams, rows = await sources(database)
    candidates = tuple(
        DreamCandidate(
            row, "8000", EmbeddingVector(values=(1.0, 0.0), dimensions=2), fact_signature(row)
        )
        for row in rows
    )
    dreams.load_candidates = AsyncMock(
        return_value=DreamCandidateLoad(
            candidates, tuple((row.id, fact_signature(row)) for row in rows), 2, 0, 0
        )
    )
    dreams.prepare_clusters = AsyncMock(
        side_effect=AssertionError("service must reuse loaded facts")
    )
    embeddings = SimpleNamespace(profile_id=1, dimensions=2, documents=object(), jobs=None)
    settings = make_settings(database.url, memory_embedding_enabled=True)
    prepared = await prepare_full_core(settings=settings, repository=dreams, embeddings=embeddings)
    assert len(prepared.clusters) == 1
    async with database.immediate_session() as writer:
        run = await plan_full_core(
            settings=settings,
            repository=dreams,
            embeddings=embeddings,
            actor_user_id="1001",
            prepared=prepared,
            session=writer,
        )
    assert run.status is DreamRunStatus.PLANNED
    assert await dreams.start_run(run.public_id)


async def test_baseline_and_checkpoint_writes_batch_and_marker_follows_all_rows(database):
    dreams, rows = await sources(database)
    signatures = tuple((rows[index % 2].id, f"signature-{index}") for index in range(600))
    calls = []

    def capture(_connection, _cursor, sql, parameters, _context, many):
        if sql.startswith("INSERT"):
            calls.append((sql, len(parameters) if many else 1))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert await dreams.initialize_baseline(signatures)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert [size for sql, size in calls if "memory_dream_fact_checkpoints" in sql] == [256, 256, 88]
    assert "memory_dream_runtime" in calls[-1][0]
    assert await dreams.checkpoint_map() == {
        rows[0].id: "signature-598",
        rows[1].id: "signature-599",
    }
    assert not await dreams.initialize_baseline(signatures)
    candidates = tuple(
        DreamCandidate(
            row, "8000", EmbeddingVector(values=(1.0, 0.0), dimensions=2), fact_signature(row)
        )
        for row in rows
    )
    await dreams.checkpoint_candidates(candidates)
    assert await dreams.checkpoint_map() == {row.id: fact_signature(row) for row in rows}


async def test_recovery_batches_aggregate_original_committed_operations_once(database):
    dreams, rows = await sources(database)
    run = await create(dreams, specs(rows, 260), mode=DreamRunMode.INCREMENTAL)
    async with database.sessions() as writer, writer.begin():
        await writer.execute(update(MemoryDreamClusterModel).values(status="processing"))
        clusters = tuple(
            await writer.scalars(
                select(MemoryDreamClusterModel).order_by(MemoryDreamClusterModel.id)
            )
        )
        for cluster in clusters[:130]:
            operation = await dreams.create_operation(
                cluster_id=cluster.id,
                action_index=0,
                operation_type=DreamOperationType.KEEP,
                source_facts=rows,
                anchor_fact_id=None,
                session=writer,
            )
            operation.status = "committed"
    reads = []

    def capture(_connection, _cursor, sql, *_args):
        if sql.startswith("SELECT"):
            reads.append(sql)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert await dreams.reset_processing_after_restart() == 260
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert len(reads) == 13  # 3 bounded recovery pages plus the final empty discovery.
    assert sum("GROUP BY memory_dream_operations.cluster_id" in sql for sql in reads) == 3
    assert await dreams.reset_processing_after_restart() == 0
    current = await dreams.get_run(run.public_id)
    assert current.completed_clusters == 130
    async with database.sessions() as reader:
        assert (
            await reader.scalar(
                select(func.count())
                .select_from(MemoryDreamOperationModel)
                .where(MemoryDreamOperationModel.status == "committed")
            )
            == 130
        )
        assert (
            await reader.scalar(
                select(func.count())
                .select_from(MemoryDreamClusterModel)
                .where(MemoryDreamClusterModel.status == "pending")
            )
            == 130
        )


async def test_baseline_failure_rolls_back_prior_batches_before_initialization_marker(database):
    dreams, rows = await sources(database)
    valid = tuple((rows[index % 2].id, f"signature-{index}") for index in range(513))
    with pytest.raises(IntegrityError):
        await dreams.initialize_baseline((*valid, (999999, "deleted fact")))
    assert not await dreams.baseline_exists()
    assert await dreams.checkpoint_map() == {}
    assert await dreams.initialize_baseline(valid)
    assert await dreams.baseline_exists()
