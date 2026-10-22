"""Expiry uses current short SQL and preserves one atomic batch."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import OperationalError
from tests.conftest import make_settings

from yuki.memory.maintenance import MemoryMaintenanceWorker
from yuki.memory.models import MemoryFactCreate
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.service import MemoryFactService
from yuki.persistence.models import (
    MemoryFactModel,
    MemoryFactRelationModel,
    MemoryFactStateEventModel,
)


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


async def test_expiry_extension_and_two_hop_conflicts_follow_current_batch_rows(
    database, monkeypatch
):
    repository, facts, rows, worker = await _case(database, 3)
    first, middle, last = rows
    async with database.immediate_session() as session:
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == middle.id)
            .values(conflict_state="contested")
        )
        session.add_all(
            [
                MemoryFactRelationModel(
                    source_fact_id=source.id,
                    target_fact_id=target.id,
                    relation_type="contradicts",
                    confidence=1,
                    created_at=datetime.now(UTC),
                )
                for source, target in ((first, middle), (middle, last))
            ]
        )
    original = repository.list_expired_candidates

    async def extend_after_discovery(**kwargs):
        discovered = await original(**kwargs)
        async with database.immediate_session() as session:
            await session.execute(
                update(MemoryFactModel)
                .where(MemoryFactModel.id.in_((middle.id, last.id)))
                .values(valid_until=datetime.now(UTC) + timedelta(days=1))
            )
        return discovered

    monkeypatch.setattr(repository, "list_expired_candidates", extend_after_discovery)
    assert await worker.process_once() == 1
    assert (await facts.get_fact(first.id)).status.value == "invalidated"
    assert (await facts.get_fact(middle.id)).conflict_state.value == "contested"
    assert (await facts.get_fact(last.id)).status.value == "active"
    monkeypatch.setattr(repository, "list_expired_candidates", original)
    async with database.immediate_session() as session:
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == last.id)
            .values(valid_until=datetime.now(UTC) - timedelta(days=1))
        )
    assert await worker.process_once() == 1
    kept = await facts.get_fact(middle.id)
    assert kept.status.value == "active" and kept.conflict_state.value == "clear"
    async with database.read_session() as session:
        events = tuple(
            await session.scalars(
                select(MemoryFactStateEventModel).where(
                    MemoryFactStateEventModel.action.in_(("expired", "conflict_cleared"))
                )
            )
        )
    assert [(item.fact_id, item.action) for item in events] == [
        (first.id, "expired"),
        (last.id, "expired"),
        (middle.id, "conflict_cleared"),
    ]


@pytest.mark.parametrize("changed_status", [None, "superseded"])
async def test_maintenance_does_not_prepare_evidence_or_expire_superseded_rows(
    database, monkeypatch, changed_status
):
    repository, facts, rows, worker = await _case(database, 2)
    discover = repository.list_expired_candidates

    async def capture(**kwargs):
        discovered = await discover(**kwargs)
        if changed_status is not None:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(MemoryFactModel)
                    .where(MemoryFactModel.id == rows[0].id)
                    .values(status=changed_status)
                )
        return discovered

    async def no_history(*args, **kwargs):
        raise AssertionError("expiry does not depend on evidence history")

    monkeypatch.setattr(repository, "list_expired_candidates", capture)
    monkeypatch.setattr(facts, "prepare_write", no_history)
    body_reads_after_writer = []
    writing = False

    def trace(_connection, _cursor, statement, *_args):
        nonlocal writing
        sql = statement.lstrip().upper()
        if sql.startswith("BEGIN IMMEDIATE"):
            writing = True
        if (
            writing
            and sql.startswith("SELECT")
            and "MEMORY_EVIDENCE.EXCERPT" in sql.split("FROM")[0]
        ):
            body_reads_after_writer.append(statement)

    def reset(_connection):
        nonlocal writing
        writing = False

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", trace)
    event.listen(engine, "commit", reset)
    event.listen(engine, "rollback", reset)
    try:
        assert await worker.process_once() == (2 if changed_status is None else 1)
    finally:
        event.remove(engine, "before_cursor_execute", trace)
        event.remove(engine, "commit", reset)
        event.remove(engine, "rollback", reset)
    assert body_reads_after_writer == []
    for row in rows:
        assert (await facts.get_fact(row.id)).status.value == (
            changed_status if row.id == rows[0].id and changed_status else "invalidated"
        )


@pytest.mark.parametrize("stop", ["cancel", "close", "complete"])
async def test_maintenance_waits_for_shared_writer_and_cancels_cleanly(database, monkeypatch, stop):
    repository, facts, rows, worker = await _case(database)
    discover, prepared = repository.list_expired_candidates, asyncio.Event()

    async def capture(*args, **kwargs):
        value = await discover(*args, **kwargs)
        prepared.set()
        return value

    monkeypatch.setattr(repository, "list_expired_candidates", capture)
    async with database.immediate_session():
        if stop == "close":
            await worker.start()
            task = worker._task
            worker.wake()
        else:
            task = asyncio.create_task(worker.process_once())
        await asyncio.wait_for(prepared.wait(), timeout=5)
        assert not task.done()
        if stop == "close":
            await asyncio.wait_for(worker.close(), timeout=5)
            assert not worker.running
        elif stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    if stop == "complete":
        assert await task == 1
    else:
        assert worker.metrics.maintenance_last_success_at is None
    assert (await facts.get_fact(rows[0].id)).status.value == (
        "invalidated" if stop == "complete" else "active"
    )
    async with database.immediate_session() as writer:
        count = await writer.scalar(
            select(func.count())
            .select_from(MemoryFactStateEventModel)
            .where(MemoryFactStateEventModel.action == "expired")
        )
        assert count == int(stop == "complete")


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
async def test_maintenance_unknown_acknowledgement_never_replays_or_reports_success(
    database, monkeypatch, phase, error_type
):
    _repository, facts, rows, worker = await _case(database)
    original, invalidate = database.immediate_session, worker._invalidate_candidates
    native = sqlite3.OperationalError("acknowledgement lost")
    native.sqlite_errorcode = 5
    uncertainty = (
        OperationalError("acknowledgement", {}, native)
        if error_type == "busy"
        else OSError("acknowledgement lost")
    )
    calls = []

    async def apply(*args, **kwargs):
        calls.append(True)
        count = await invalidate(*args, **kwargs)
        if phase == "rollback":
            raise OSError("operation failure")
        return count

    @asynccontextmanager
    async def unknown_ack():
        try:
            async with original() as writer:
                yield writer
        except OSError:
            if phase == "rollback":
                raise uncertainty from None
            raise
        if phase == "commit":
            raise uncertainty

    monkeypatch.setattr(database, "immediate_session", unknown_ack)
    monkeypatch.setattr(worker, "_invalidate_candidates", apply)
    with pytest.raises(type(uncertainty)) as caught:
        await worker.process_once()
    assert caught.value is uncertainty and len(calls) == 1
    assert worker.metrics.maintenance_last_success_at is None
    assert (await facts.get_fact(rows[0].id)).status.value == (
        "invalidated" if phase == "commit" else "active"
    )
