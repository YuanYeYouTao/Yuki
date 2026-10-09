"""The maintenance batch prepares all history before its first fact write."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import OperationalError
from tests.conftest import make_settings

from qq_ai_bot.memory.maintenance import MemoryMaintenanceWorker
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.models import MemoryFactModel, MemoryFactStateEventModel


async def _case(database, count=1):
    repository = MemoryFactRepository(database)
    facts = MemoryFactService(repository)
    rows = tuple(
        [
            await facts.remember(
                MemoryFactCreate(
                    scope_type="self",
                    visibility_type="global",
                    category="test",
                    memory_key=f"maintenance:{index}",
                    content=f"expired fact {index}",
                    source_type="explicit",
                    valid_until=datetime.now(UTC) - timedelta(days=1),
                )
            )
            for index in range(count)
        ]
    )
    return (
        repository,
        facts,
        rows,
        MemoryMaintenanceWorker(settings=make_settings(database.url), facts=facts),
    )


def _race(database, worker, monkeypatch, *, code, fail_attempts=4):
    invalidate = worker._invalidate_candidates
    attempts, errors = [], []

    async def interleave(rows, *, now, session):
        attempts.append((rows, now, session))
        if fail_attempts is None or len(attempts) <= fail_attempts:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(MemoryFactModel)
                    .where(MemoryFactModel.id == rows[0].id)
                    .values(updated_at=datetime.now(UTC))
                )
                if code == 5:
                    # Real concurrent writer still holds the WAL lock at DML.
                    return await invalidate(rows, now=now, session=session)
            # The other connection committed after evidence preparation; upgrading
            # this original snapshot now produces native BUSY_SNAPSHOT 517.
        return await invalidate(rows, now=now, session=session)

    def record(context):
        errors.append(getattr(context.original_exception, "sqlite_errorcode", None))

    monkeypatch.setattr(worker, "_invalidate_candidates", interleave)
    event.listen(database.engine.sync_engine, "handle_error", record)
    return attempts, errors, record


@pytest.mark.parametrize("race", [False, True])
async def test_maintenance_prepares_entire_invalidation_batch_before_any_write(
    database, monkeypatch, race
):
    _repository, facts, rows, worker = await _case(database, 2)
    prepared = []
    prepare = facts.prepare_evidence_write

    async def capture(fact_ids, *, session, targets=()):
        if targets:
            assert not session.info.get("memory_evidence_write_started")
            prepared.append(fact_ids)
        elif session.info.get("memory_evidence_write_started"):
            assert set(fact_ids) <= session.info["memory_evidence_rows"].keys()
        await prepare(fact_ids, session=session, targets=targets)
        if race and targets and len(prepared) == 1:
            # Commit on another real WAL connection after the read snapshot.
            # The original upgrade must return native SQLITE_BUSY_SNAPSHOT.
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(MemoryFactModel)
                    .where(MemoryFactModel.id == rows[0].id)
                    .values(updated_at=datetime.now(UTC))
                )

    monkeypatch.setattr(facts, "prepare_evidence_write", capture)
    historical_reads_after_write = []
    writing = False

    def trace(_connection, _cursor, statement, *_args):
        nonlocal writing
        sql = statement.lstrip().upper()
        if sql.startswith(("INSERT", "UPDATE", "DELETE")):
            writing = True
        elif writing and sql.startswith("SELECT") and "MEMORY_EVIDENCE" in sql:
            historical_reads_after_write.append(statement)

    def reset(_connection):
        nonlocal writing
        writing = False

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", trace)
    event.listen(engine, "commit", reset)
    event.listen(engine, "rollback", reset)
    try:
        assert await worker.process_once() == 2
    finally:
        event.remove(engine, "before_cursor_execute", trace)
        event.remove(engine, "commit", reset)
        event.remove(engine, "rollback", reset)
    assert prepared == [tuple(row.id for row in rows)] * (2 if race else 1)
    assert historical_reads_after_write == []
    for row in rows:
        assert (await facts.get_fact(row.id)).status.value == "invalidated"


@pytest.mark.parametrize("code", [5, 517])
async def test_maintenance_reprepares_four_native_busy_rollbacks_with_original_batch_cutoff(
    database, monkeypatch, code
):
    repository, facts, rows, worker = await _case(database, 2)
    attempts, errors, record = _race(database, worker, monkeypatch, code=code)
    scans, callbacks = [], []
    scan, apply = repository.list_expired_candidates, repository.apply_evidence_write

    async def scan_once(**kwargs):
        if kwargs.get("session") is None:
            scans.append(kwargs)
        return await scan(**kwargs)

    async def apply_once(operation):
        callbacks.append(operation)
        return await apply(operation)

    monkeypatch.setattr(repository, "list_expired_candidates", scan_once)
    monkeypatch.setattr(repository, "apply_evidence_write", apply_once)
    try:
        assert await worker.process_once() == 2
    finally:
        event.remove(database.engine.sync_engine, "handle_error", record)
    assert errors == [code] * 4 and len(attempts) == 5
    assert len(scans) == len(callbacks) == 1
    assert all(batch is attempts[0][0] for batch, _, _ in attempts)
    assert {tuple(row.id for row in batch) for batch, _, _ in attempts} == {
        tuple(row.id for row in rows)
    }
    assert all(cutoff is scans[0]["now"] for _, cutoff, _ in attempts)
    assert len({id(session) for _, _, session in attempts}) == 5
    assert worker.metrics.count("maintenance_expired") == 2
    assert worker.metrics.maintenance_last_success_at is scans[0]["now"]
    async with database.sessions() as session:
        expired = list(
            await session.scalars(
                select(MemoryFactStateEventModel).where(
                    MemoryFactStateEventModel.action == "expired"
                )
            )
        )
    assert sorted(item.fact_id for item in expired) == sorted(row.id for row in rows)
    assert len(expired) == 2
    for row in rows:
        assert (await facts.get_fact(row.id)).status.value == "invalidated"


@pytest.mark.parametrize("code", [5, 517])
async def test_cancel_exits_maintenance_busy_repreparation_without_replacing_batch(
    database, monkeypatch, code
):
    _, facts, rows, worker = await _case(database)
    attempts, errors, record = _race(database, worker, monkeypatch, code=code, fail_attempts=None)
    fourth = asyncio.Event()

    def signal(_context):
        if len(errors) >= 4:
            fourth.set()

    event.listen(database.engine.sync_engine, "handle_error", signal)
    task = asyncio.create_task(worker.process_once())
    try:
        await asyncio.wait_for(fourth.wait(), timeout=30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(database.engine.sync_engine, "handle_error", signal)
        event.remove(database.engine.sync_engine, "handle_error", record)
    assert errors[:4] == [code] * 4
    assert all(batch is attempts[0][0] for batch, _, _ in attempts)
    assert all(cutoff is attempts[0][1] for _, cutoff, _ in attempts)
    assert (await facts.get_fact(rows[0].id)).status.value == "active"
    assert worker.metrics.maintenance_last_success_at is None
    assert worker.metrics.count("maintenance_expired") == 0
    async with database.immediate_session() as writer:
        assert (
            await writer.scalar(
                select(func.count())
                .select_from(MemoryFactStateEventModel)
                .where(MemoryFactStateEventModel.action == "expired")
            )
            == 0
        )


async def test_close_cancels_running_maintenance_under_continuous_real_writer_busy(
    database, monkeypatch
):
    _, facts, rows, worker = await _case(database)
    attempts, errors, record = _race(database, worker, monkeypatch, code=5, fail_attempts=None)
    fourth = asyncio.Event()

    def signal(_context):
        if len(errors) >= 4:
            fourth.set()

    event.listen(database.engine.sync_engine, "handle_error", signal)
    await worker.start()
    running_task = worker._task
    assert running_task is not None
    worker.wake()
    try:
        await asyncio.wait_for(fourth.wait(), timeout=30)
        await asyncio.wait_for(worker.close(), timeout=5)
    finally:
        running_task.cancel()
        await asyncio.gather(running_task, return_exceptions=True)
        event.remove(database.engine.sync_engine, "handle_error", signal)
        event.remove(database.engine.sync_engine, "handle_error", record)
    assert running_task.cancelled()
    assert not worker.running and worker._task is None
    assert errors[:4] == [5] * 4
    assert all(batch is attempts[0][0] for batch, _, _ in attempts)
    assert all(cutoff is attempts[0][1] for _, cutoff, _ in attempts)
    assert (await facts.get_fact(rows[0].id)).status.value == "active"
    assert worker.metrics.maintenance_last_success_at is None
    assert worker.metrics.count("maintenance_expired") == 0
    # The writer owned by the canceled maintenance attempt has been released.
    async with database.immediate_session() as writer:
        assert (
            await writer.scalar(
                select(func.count())
                .select_from(MemoryFactStateEventModel)
                .where(MemoryFactStateEventModel.action == "expired")
            )
            == 0
        )


async def test_maintenance_idle_close_allows_existing_start_lifecycle(database):
    _, _, _, worker = await _case(database)
    await worker.close()
    await worker.start()
    first_task = worker._task
    await worker.close()
    assert first_task is not None and first_task.cancelled()
    assert worker._task is None and not worker.running
    await worker.start()
    assert worker._task is not first_task and worker.running
    await worker.close()
    await worker.close()
    assert worker._task is None and not worker.running


@pytest.mark.parametrize("phase", ["commit", "rollback"])
@pytest.mark.parametrize("error_type", ["busy", "io"])
async def test_maintenance_unknown_acknowledgement_never_retries_or_reports_success(
    database, monkeypatch, phase, error_type
):
    repository, facts, rows, worker = await _case(database)
    original = repository.transaction
    native = sqlite3.OperationalError("acknowledgement lost")
    native.sqlite_errorcode = 5
    uncertainty = (
        OperationalError("acknowledgement", {}, native)
        if error_type == "busy"
        else OSError("acknowledgement lost")
    )
    calls = 0
    attempts, errors, record = (
        _race(database, worker, monkeypatch, code=517, fail_attempts=1)
        if phase == "rollback"
        else ([], [], None)
    )
    raced_invalidate = worker._invalidate_candidates

    async def count(rows, *, now, session):
        nonlocal calls
        calls += 1
        return await raced_invalidate(rows, now=now, session=session)

    @asynccontextmanager
    async def unknown_ack(*, read_snapshot=False):
        try:
            async with original(read_snapshot=read_snapshot) as session:
                yield session
        except OperationalError:
            if phase == "rollback":
                # Actual rollback completed, but its acknowledgement is uncertain.
                raise uncertainty from None
            raise
        if phase == "commit":
            # Actual mutation committed before transport/driver acknowledgement loss.
            raise uncertainty

    monkeypatch.setattr(worker, "_invalidate_candidates", count)
    monkeypatch.setattr(repository, "transaction", unknown_ack)
    try:
        with pytest.raises(type(uncertainty)) as caught:
            await worker.process_once()
    finally:
        if record is not None:
            event.remove(database.engine.sync_engine, "handle_error", record)
    assert caught.value is uncertainty and calls == 1
    assert worker.metrics.maintenance_last_success_at is None
    assert worker.metrics.count("maintenance_expired") == 0
    assert (await facts.get_fact(rows[0].id)).status.value == (
        "invalidated" if phase == "commit" else "active"
    )
    if phase == "rollback":
        assert errors == [517] and len(attempts) == 1
    async with database.sessions() as session:
        expired = await session.scalar(
            select(func.count())
            .select_from(MemoryFactStateEventModel)
            .where(MemoryFactStateEventModel.action == "expired")
        )
    assert expired == (1 if phase == "commit" else 0)


async def test_non_busy_maintenance_operation_error_is_not_reprepared(database, monkeypatch):
    _, facts, rows, worker = await _case(database)
    native = sqlite3.OperationalError("locked, not busy")
    native.sqlite_errorcode = 6
    failure = OperationalError("operation", {}, native)
    calls = 0

    async def locked(_rows, *, now, session):
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(worker, "_invalidate_candidates", locked)
    with pytest.raises(OperationalError) as caught:
        await worker.process_once()
    assert caught.value is failure and calls == 1
    assert (await facts.get_fact(rows[0].id)).status.value == "active"
    assert worker.metrics.maintenance_last_success_at is None
