"""Real WAL contention reprepares the original database plan without model replay."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import OperationalError
from tests.conftest import make_settings
from tests.unit.test_memory_dream import _empty_dream_statistics, _fact_with_evidence, _services

from qq_ai_bot.memory.dream.db_models import (
    MemoryDreamClusterModel,
    MemoryDreamOperationModel,
    MemoryDreamRunModel,
)
from qq_ai_bot.memory.dream.models import (
    DreamAction,
    DreamClusterStatus,
    DreamOperationType,
    DreamOutput,
    DreamRunMode,
)
from qq_ai_bot.memory.dream.service import DreamService
from qq_ai_bot.memory.dream.worker import DreamWorker


async def _case(database, monkeypatch, *, clusters=1):
    mutations, facts, ledger, dreams = _services(database)
    rows = tuple(
        [
            await _fact_with_evidence(
                facts,
                ledger,
                message_id=f"busy-{index}",
                memory_key=f"busy:{index}",
                content=f"source {index}",
            )
            for index in range(clusters * 2)
        ]
    )
    service = object.__new__(DreamService)
    settings = make_settings(database.url)
    service._settings = settings
    service._facts, service._mutations, service._repository = facts, mutations, dreams
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=_empty_dream_statistics(),
        clusters=tuple(
            (
                f"cluster-{index}",
                "partition",
                "8000",
                "fact",
                tuple(row.id for row in rows[index * 2 : index * 2 + 2]),
                service._cluster_fingerprint(rows[index * 2 : index * 2 + 2]),
            )
            for index in range(clusters)
        ),
        snapshot_max_fact_id=rows[-1].id,
        actor_user_id=None,
        scheduled_slot=None,
    )
    assert await dreams.start_run(run.public_id)
    calls = []

    async def decide(_payload, *, run, cluster, **_kwargs):
        assert await dreams.reserve_model_call(
            run_public_id=run.public_id,
            cluster_id=cluster.id,
            maximum=settings.memory_dream_max_model_calls_per_run,
        )
        calls.append(cluster.id)
        return DreamOutput(
            actions=(
                DreamAction(operation=DreamOperationType.KEEP, source_refs=("memory_1",)),
                DreamAction(operation=DreamOperationType.KEEP, source_refs=("memory_2",)),
            )
        ), 1

    monkeypatch.setattr(service, "_decide", decide)
    worker = DreamWorker(settings=settings, repository=dreams, service=service)
    return facts, dreams, service, run, worker, calls


def _race(database, dreams, monkeypatch, *, code=517, fail_attempts=4):
    create = dreams.create_operation
    attempts = []
    errors = []

    async def interleaved_create(**kwargs):
        # Every attempt's evidence preparation has already read its snapshot.
        # The unrelated committed timestamp leaves model inputs unchanged.
        attempts.append((kwargs["cluster_id"], kwargs["public_id"]))
        if fail_attempts is None or len(attempts) <= fail_attempts:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(MemoryDreamRunModel).values(updated_at=datetime.now(UTC))
                )
                if code == 5:
                    # The unrelated writer still holds its WAL lock when the
                    # prepared reader first attempts to upgrade: real BUSY 5.
                    return await create(**kwargs)
        return await create(**kwargs)

    def record(context):
        errors.append(getattr(context.original_exception, "sqlite_errorcode", None))

    monkeypatch.setattr(dreams, "create_operation", interleaved_create)
    event.listen(database.engine.sync_engine, "handle_error", record)
    return attempts, errors, record


@pytest.mark.parametrize("code", [5, 517])
async def test_four_real_busy_rollbacks_complete_original_cluster_and_continue_next(
    database, monkeypatch, code
):
    _, dreams, _, run, worker, calls = await _case(database, monkeypatch, clusters=2)
    attempts, errors, record = _race(database, dreams, monkeypatch, code=code)
    try:
        await worker._drain_active()
    finally:
        event.remove(database.engine.sync_engine, "handle_error", record)
    assert errors == [code] * 4
    assert len(attempts) == 8 and len(set(attempts[:5])) == 1
    async with database.sessions() as reader:
        clusters = list(
            await reader.scalars(
                select(MemoryDreamClusterModel).order_by(MemoryDreamClusterModel.id)
            )
        )
        assert calls == [row.id for row in clusters]
        assert [
            (row.status, row.model_calls, row.attempts, row.operation_count) for row in clusters
        ] == [("completed", 1, 1, 2), ("completed", 1, 1, 2)]
        assert clusters[0].error_category is None
        assert await reader.scalar(select(func.count()).select_from(MemoryDreamOperationModel)) == 4
    current = await dreams.get_run(run.public_id)
    assert (current.status.value, current.model_calls, current.failed_clusters) == (
        "completed",
        2,
        0,
    )
    await worker._drain_active()
    assert len(calls) == 2


async def test_original_committed_receipt_is_not_overwritten_as_zero_failure(database, monkeypatch):
    _, dreams, service, run, worker, calls = await _case(database, monkeypatch)
    cluster = await dreams.claim_next_cluster(run.public_id)
    await service.process_cluster(run, cluster)
    await worker._finish_cluster_failure(
        cluster.id, status=DreamClusterStatus.FAILED, error_category="test_exhaustion"
    )
    async with database.sessions() as reader:
        current = await reader.get(MemoryDreamClusterModel, cluster.id)
        assert (current.status, current.operation_count, current.model_calls) == ("completed", 2, 1)
        assert current.error_category == "recovered_committed_operation"
        assert await reader.scalar(select(func.count()).select_from(MemoryDreamOperationModel)) == 2
    assert calls == [cluster.id]


@pytest.mark.parametrize("category", ["OperationalError", "OSError"])
async def test_process_commit_acknowledgement_loss_preserves_actual_operations(
    database, monkeypatch, category
):
    facts, dreams, _, run, worker, calls = await _case(database, monkeypatch)
    transaction = facts.repository.transaction
    native = sqlite3.OperationalError("commit acknowledgement lost")
    native.sqlite_errorcode = 5
    failure = (
        OperationalError("COMMIT", {}, native)
        if category == "OperationalError"
        else OSError("commit acknowledgement lost")
    )

    @asynccontextmanager
    async def lost_confirmation(*, read_snapshot=False):
        async with transaction(read_snapshot=read_snapshot) as session:
            yield session
            wrote = session.info.get("memory_evidence_write_started")
        if wrote:
            raise failure

    async def no_schedule():
        return None

    monkeypatch.setattr(facts.repository, "transaction", lost_confirmation)
    monkeypatch.setattr(worker, "_schedule_if_due", no_schedule)
    if category == "OperationalError":
        worker._task = asyncio.create_task(worker._run())
        with pytest.raises(OperationalError) as caught:
            await worker._task
        assert caught.value is failure
    else:
        await worker._drain_active()
    assert len(calls) == 1
    async with database.sessions() as reader:
        cluster = await reader.scalar(select(MemoryDreamClusterModel))
        assert (cluster.status, cluster.model_calls, cluster.operation_count) == (
            "processing" if category == "OperationalError" else "completed",
            1,
            0 if category == "OperationalError" else 2,
        )
        assert await reader.scalar(select(func.count()).select_from(MemoryDreamOperationModel)) == 2
    assert (await dreams.get_run(run.public_id)).model_calls == 1
    assert not (await worker.health()).running


@pytest.mark.parametrize("code", [5, 517])
@pytest.mark.parametrize("phase", ["commit", "cleanup"])
async def test_phase_busy_is_not_classified_as_rolled_back_operation(
    database, monkeypatch, phase, code
):
    facts, _, _, _, _, _ = await _case(database, monkeypatch)
    original = facts.repository.transaction
    native = sqlite3.OperationalError("phase failure")
    native.sqlite_errorcode = code
    phase_error = OperationalError("phase", {}, native)
    calls = 0

    @asynccontextmanager
    async def failure_context(*, read_snapshot=False):
        try:
            async with original(read_snapshot=read_snapshot) as session:
                yield session
        except OperationalError:
            if phase == "cleanup":
                raise phase_error from None
            raise
        if phase == "commit":
            # The real transaction committed before acknowledgement was lost.
            raise phase_error

    async def operation(session):
        nonlocal calls
        calls += 1
        if phase == "cleanup":
            failure = sqlite3.OperationalError("operation snapshot")
            failure.sqlite_errorcode = code
            raise OperationalError("operation", {}, failure)
        await session.execute(update(MemoryDreamRunModel).values(model_calls=7))

    monkeypatch.setattr(facts.repository, "transaction", failure_context)
    with pytest.raises(OperationalError) as caught:
        await facts.repository.apply_evidence_write(operation)
    assert caught.value is phase_error
    assert calls == 1
    async with database.sessions() as reader:
        count = await reader.scalar(select(MemoryDreamRunModel.model_calls))
        assert count == (7 if phase == "commit" else 0)


@pytest.mark.parametrize("code", [6, 262])
async def test_other_native_busy_codes_keep_original_error_without_retry(
    database, monkeypatch, code
):
    facts, _, _, _, _, _ = await _case(database, monkeypatch)
    native = sqlite3.OperationalError("other contention")
    native.sqlite_errorcode = code
    failure = OperationalError("operation", {}, native)
    calls = 0

    async def operation(_session):
        nonlocal calls
        calls += 1
        raise failure

    with pytest.raises(OperationalError) as caught:
        await facts.repository.apply_evidence_write(operation)
    assert caught.value is failure and calls == 1


@pytest.mark.parametrize("code", [5, 517])
async def test_deferred_orm_first_write_reprepares_four_rollbacks_before_physical_commit(
    database, monkeypatch, code
):
    facts, _, _, _, _, _ = await _case(database, monkeypatch)
    calls = 0
    errors = []

    async def operation(session):
        nonlocal calls
        calls += 1
        row = await session.scalar(select(MemoryDreamRunModel))
        row.model_calls = 7  # Defer this ORM DML to the helper's flush.
        if calls <= 4:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(MemoryDreamRunModel).values(updated_at=datetime.now(UTC))
                )
                if code == 5:
                    await session.flush()

    def record(context):
        errors.append(getattr(context.original_exception, "sqlite_errorcode", None))

    event.listen(database.engine.sync_engine, "handle_error", record)
    try:
        await facts.repository.apply_evidence_write(operation)
    finally:
        event.remove(database.engine.sync_engine, "handle_error", record)
    assert errors == [code] * 4 and calls == 5
    async with database.sessions() as reader:
        assert await reader.scalar(select(MemoryDreamRunModel.model_calls)) == 7


@pytest.mark.parametrize("code", [5, 517])
async def test_cancel_exits_real_busy_repreparation_without_replaying_model(
    database, monkeypatch, code
):
    _, dreams, _, run, worker, calls = await _case(database, monkeypatch)
    attempts, errors, record = _race(database, dreams, monkeypatch, code=code, fail_attempts=None)
    fourth = asyncio.Event()

    def signal(_context):
        if len(errors) >= 4:
            fourth.set()

    engine = database.engine.sync_engine
    event.listen(engine, "handle_error", signal)
    task = asyncio.create_task(worker._drain_active())
    try:
        await asyncio.wait_for(fourth.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "handle_error", signal)
        event.remove(engine, "handle_error", record)
    assert errors[:4] == [code] * 4 and len(set(attempts)) == 1
    assert len(calls) == 1
    async with database.immediate_session() as writer:
        cluster = await writer.scalar(select(MemoryDreamClusterModel))
        assert cluster.status == "processing" and cluster.model_calls == 1
        assert await writer.scalar(select(func.count()).select_from(MemoryDreamOperationModel)) == 0
    assert (await dreams.get_run(run.public_id)).model_calls == 1


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY_RECOVERY, sqlite3.SQLITE_BUSY_TIMEOUT])
async def test_native_busy_extended_codes_reprepare_original_callback(database, monkeypatch, code):
    facts, _, _, _, _, _ = await _case(database, monkeypatch)
    calls = 0

    async def operation(session):
        nonlocal calls
        calls += 1
        if calls <= 4:
            failure = sqlite3.OperationalError("native extended busy")
            failure.sqlite_errorcode = code
            raise OperationalError("operation", {}, failure)
        await session.execute(update(MemoryDreamRunModel).values(model_calls=7))

    await facts.repository.apply_evidence_write(operation)
    assert calls == 5
    async with database.sessions() as reader:
        assert await reader.scalar(select(MemoryDreamRunModel.model_calls)) == 7
