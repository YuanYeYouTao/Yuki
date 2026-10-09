"""Compaction retries only rolled-back database work under its original provenance."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import make_settings
from tests.unit.test_evidence_compaction_preparation import _seed
from tests.unit.test_memory_dream import _empty_dream_statistics, _services

from qq_ai_bot.memory.dream.db_models import (
    MemoryDreamOperationModel,
    MemoryEvidenceCompactionItemModel,
    MemoryEvidenceCompactionRunModel,
)
from qq_ai_bot.memory.dream.models import DreamAction, DreamOperationType, DreamOutput, DreamRunMode
from qq_ai_bot.memory.dream.repository import fact_signature
from qq_ai_bot.memory.dream.service import DreamService
from qq_ai_bot.persistence.models import (
    MemoryMutationReceiptModel,
    MemorySelfReflectionResultModel,
    MemorySelfReflectionRunModel,
)


async def _case(database, *, provenance="self_reflection"):
    facts, compaction, source, _, reflection = await _seed(database)
    source = await facts.get_fact(source.id)
    decide = AsyncMock()
    if provenance == "dream":
        mutations, _, _, dreams = _services(database)
        service = object.__new__(DreamService)
        service._settings = make_settings(database.url)
        service._facts, service._mutations, service._repository = facts, mutations, dreams
        decide.return_value = (
            DreamOutput(
                actions=(
                    DreamAction(
                        operation=DreamOperationType.SYNTHESIZE,
                        source_refs=("memory_1",),
                        anchor_ref="memory_1",
                        content="the same recoverable episode",
                        importance=3,
                    ),
                )
            ),
            1,
        )
        service._decide = decide
        run = await dreams.create_run(
            mode=DreamRunMode.FULL,
            statistics=_empty_dream_statistics(),
            clusters=(
                (
                    "original-cluster",
                    "partition",
                    "8000",
                    "episode",
                    (source.id,),
                    service._cluster_fingerprint((source,)),
                ),
            ),
            snapshot_max_fact_id=source.id,
            actor_user_id=None,
            scheduled_slot=None,
        )
        assert await dreams.start_run(run.public_id)
        cluster = await dreams.claim_next_cluster(run.public_id)
        assert await service.process_cluster(run, cluster) == (1, 1, True)
        # This case targets the Dream output; retain all source evidence and receipts.
        async with database.immediate_session() as session:
            await session.execute(
                delete(MemorySelfReflectionResultModel).where(
                    MemorySelfReflectionResultModel.fact_id == source.id
                )
            )
    async with database.sessions() as session:
        operation = await session.scalar(select(MemoryDreamOperationModel))
        receipts = tuple(await session.scalars(select(MemoryMutationReceiptModel.mutation_id)))
    return facts, compaction, reflection.id, decide, operation, receipts


def _race(database, patch, *, code, failures=4):
    execute = AsyncSession.execute
    attempts, errors = [], []
    reached_four = asyncio.Event()

    async def contested(session, statement, *args, **kwargs):
        if str(statement).startswith("DELETE FROM memory_evidence"):
            attempts.append(tuple(statement.compile().params.values()))
            if failures is None and len(attempts) > 4:
                # Four real rollbacks have finished; cancel the next prepared
                # unit before DML, rather than the test's competing writer cleanup.
                reached_four.set()
                await asyncio.Event().wait()
            if failures is None or len(attempts) <= failures:
                async with database.immediate_session() as writer:
                    await execute(
                        writer,
                        update(MemorySelfReflectionRunModel).values(started_at=datetime.now(UTC)),
                    )
                    if code == sqlite3.SQLITE_BUSY:
                        # Keep the competing writer locked at the reader's first DML.
                        return await execute(session, statement, *args, **kwargs)
                # The competing commit makes this prepared WAL snapshot stale.
        return await execute(session, statement, *args, **kwargs)

    def record(context):
        errors.append(getattr(context.original_exception, "sqlite_errorcode", None))

    patch.setattr(AsyncSession, "execute", contested)
    event.listen(database.engine.sync_engine, "handle_error", record)
    return attempts, errors, reached_four, record


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_BUSY_SNAPSHOT])
@pytest.mark.parametrize("provenance", ["self_reflection", "dream"])
async def test_four_real_busy_rollbacks_finish_original_compaction_and_keep_provenance(
    database, monkeypatch, code, provenance
):
    facts, compaction, reflection_id, decide, operation, receipts = await _case(
        database, provenance=provenance
    )
    keep_ids = []
    keep = compaction._dream_keep_ids

    async def original_operation(**kwargs):
        keep_ids.append(kwargs["operation_id"])
        return await keep(**kwargs)

    monkeypatch.setattr(compaction, "_dream_keep_ids", original_operation)
    with monkeypatch.context() as patch:
        attempts, errors, _, record = _race(database, patch, code=code)
        try:
            assert await compaction.run_batch() == 1
        finally:
            event.remove(database.engine.sync_engine, "handle_error", record)
    assert errors == [code] * 4 and len(attempts) == 5
    assert all(attempt == attempts[0] for attempt in attempts)
    assert decide.call_count == (1 if provenance == "dream" else 0)
    async with database.sessions() as session:
        runs = tuple(await session.scalars(select(MemoryEvidenceCompactionRunModel)))
        items = tuple(await session.scalars(select(MemoryEvidenceCompactionItemModel)))
        assert len(runs) == len(items) == 1
        item = items[0]
        assert item.run_id == runs[0].id and item.status == "completed"
        assert item.error_category is None and item.evidence_before == 13
        assert item.evidence_after == 2 and item.deleted_count == 11
        assert await session.get(MemorySelfReflectionRunModel, reflection_id) is not None
        assert (
            tuple(await session.scalars(select(MemoryMutationReceiptModel.mutation_id))) == receipts
        )
        if provenance == "dream":
            current = await session.scalar(select(MemoryDreamOperationModel))
            assert (current.id, current.public_id, current.status) == (
                operation.id,
                operation.public_id,
                "committed",
            )
            assert keep_ids == [operation.id] * 5
            assert item.dream_operation_id == operation.id
    assert (await facts.get_fact(item.fact_id)).evidence_count == 2
    assert await compaction.run_batch() == 0
    assert decide.call_count == (1 if provenance == "dream" else 0)


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_BUSY_SNAPSHOT])
async def test_cancel_real_busy_preparation_then_recover_original_claim(
    database, monkeypatch, code
):
    facts, compaction, _, decide, _, receipts = await _case(database)
    with monkeypatch.context() as patch:
        attempts, errors, reached_four, record = _race(database, patch, code=code, failures=None)
        task = asyncio.create_task(compaction.run_batch())
        try:
            await asyncio.wait_for(reached_four.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            event.remove(database.engine.sync_engine, "handle_error", record)
    assert errors[:4] == [code] * 4
    assert all(attempt == attempts[0] for attempt in attempts)
    async with database.immediate_session() as session:
        run = await session.scalar(select(MemoryEvidenceCompactionRunModel))
        item = await session.scalar(select(MemoryEvidenceCompactionItemModel))
        identity = run.id, run.public_id, item.id
        assert run.status == "running" and item.status == "processing"
    assert (await facts.get_fact(item.fact_id)).evidence_count == 13
    assert await compaction.run_batch() == 1
    async with database.sessions() as session:
        run = await session.scalar(select(MemoryEvidenceCompactionRunModel))
        current = await session.scalar(select(MemoryEvidenceCompactionItemModel))
        assert (run.id, run.public_id, current.id) == identity
        assert current.status == "completed" and current.evidence_after == 2
        assert (
            tuple(await session.scalars(select(MemoryMutationReceiptModel.mutation_id))) == receipts
        )
    assert decide.call_count == 0


@pytest.mark.parametrize(
    "phase,provenance",
    [("commit", "self_reflection"), ("commit", "dream"), ("cleanup", "self_reflection")],
)
@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_BUSY_SNAPSHOT])
@pytest.mark.parametrize("error_type", ["busy", "io", "cancel"])
async def test_uncertain_acknowledgement_does_not_repeat_compaction(
    database, monkeypatch, phase, provenance, code, error_type
):
    facts, compaction, _, decide, operation, receipts = await _case(database, provenance=provenance)
    transaction = facts.repository.transaction
    execute = AsyncSession.execute
    native = sqlite3.OperationalError("uncertain transaction acknowledgement")
    native.sqlite_errorcode = code
    failure = (
        OperationalError(phase, {}, native)
        if error_type == "busy"
        else OSError("uncertain transaction acknowledgement")
        if error_type == "io"
        else asyncio.CancelledError()
    )
    writes = []

    async def observe_delete(session, statement, *args, **kwargs):
        if str(statement).startswith("DELETE FROM memory_evidence"):
            writes.append(statement)
            if phase == "cleanup":
                original = sqlite3.OperationalError("operation snapshot")
                original.sqlite_errorcode = code
                raise OperationalError("operation", {}, original)
        return await execute(session, statement, *args, **kwargs)

    @asynccontextmanager
    async def uncertain(*, read_snapshot=False):
        try:
            async with transaction(read_snapshot=read_snapshot) as session:
                yield session
        except OperationalError:
            if phase == "cleanup" and read_snapshot:
                raise failure from None
            raise
        if phase == "commit" and read_snapshot:
            # Durable DELETE succeeded; the physical commit confirmation is lost.
            raise failure

    monkeypatch.setattr(facts.repository, "transaction", uncertain)
    monkeypatch.setattr(AsyncSession, "execute", observe_delete)
    if error_type == "io":
        # The outer error path must not overwrite the committed item as failed.
        assert await compaction.run_batch() == 1
    else:
        with pytest.raises(type(failure)) as caught:
            await compaction.run_batch()
        assert caught.value is failure
    assert len(writes) == 1
    model_calls = int(provenance == "dream")
    assert decide.call_count == model_calls
    async with database.immediate_session() as session:
        item = await session.scalar(select(MemoryEvidenceCompactionItemModel))
        run = await session.scalar(select(MemoryEvidenceCompactionRunModel))
        identity = run.id, run.public_id, item.id
        assert item.status == (
            "completed" if phase == "commit" else "failed" if error_type == "io" else "processing"
        )
        assert item.error_category == (
            "OSError" if phase == "cleanup" and error_type == "io" else None
        )
        assert (
            tuple(await session.scalars(select(MemoryMutationReceiptModel.mutation_id))) == receipts
        )
    current_fact = await facts.get_fact(item.fact_id)
    assert current_fact.evidence_count == len(await facts.list_evidence(item.fact_id))
    assert current_fact.evidence_count == (2 if phase == "commit" else 13)
    if phase == "commit":
        assert await compaction.run_batch() == 0
        async with database.sessions() as session:
            current = await session.scalar(select(MemoryEvidenceCompactionItemModel))
            run = await session.scalar(select(MemoryEvidenceCompactionRunModel))
            assert (run.id, run.public_id, current.id) == identity
            assert current.status == run.status == "completed"
            assert current.evidence_after == 2 and current.deleted_count == 11
            assert run.completed_items == run.scanned_facts == 1
            assert run.failed_items == run.skipped_items == 0
            assert (run.evidence_before, run.evidence_after) == (13, 2)
            assert current.error_category is run.error_category is None
            if provenance == "dream":
                original = await session.get(MemoryDreamOperationModel, operation.id)
                assert original.public_id == operation.public_id and original.status == "committed"
                assert original.result_signature == fact_signature(current_fact)
        assert len(writes) == 1 and decide.call_count == model_calls


@pytest.mark.parametrize("code", [sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_LOCKED_SHAREDCACHE])
async def test_locked_failure_is_not_retried_or_written_as_success(database, monkeypatch, code):
    facts, compaction, _, _, _, _ = await _case(database)
    execute = AsyncSession.execute
    original = sqlite3.OperationalError("locked operation")
    original.sqlite_errorcode = code
    failure = OperationalError("DELETE", {}, original)
    attempts = []

    async def locked(session, statement, *args, **kwargs):
        if str(statement).startswith("DELETE FROM memory_evidence"):
            attempts.append(statement)
            raise failure
        return await execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", locked)
    with pytest.raises(OperationalError) as caught:
        await compaction.run_batch()
    assert caught.value is failure and len(attempts) == 1
    async with database.sessions() as session:
        item = await session.scalar(select(MemoryEvidenceCompactionItemModel))
        assert item.status == "processing" and item.deleted_count == 0
    assert (await facts.get_fact(item.fact_id)).evidence_count == 13
