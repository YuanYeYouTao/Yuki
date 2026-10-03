"""Real WAL empty maintenance reads, fair source windows, and recovery CAS."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, event, update
from tests.conftest import make_settings

from qq_ai_bot.memory.maintenance import MemoryMaintenanceWorker
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.reflection.models import MemoryReflectionCandidate, MemoryReflectionIssue
from qq_ai_bot.memory.reflection.repository import MemoryReflectionRepository
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.models import (
    MemoryActivationStateModel,
    MemoryFactModel,
    MemoryReflectionJobModel,
)


@asynccontextmanager
async def other_writer_and_read_only(database):
    """Fail at the actual SQL boundary instead of relying on timing thresholds."""
    statements = []

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement)
        sql = statement.lstrip().upper()
        assert not sql.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN IMMEDIATE"))

    async with database.immediate_session():
        event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            yield statements
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", capture)


async def facts(database, count=1):
    repository = MemoryFactRepository(database)
    service = MemoryFactService(repository)
    rows = []
    for index in range(count):
        rows.append(
            await service.remember(
                MemoryFactCreate(
                    scope_type="self",
                    visibility_type="global",
                    category="test",
                    memory_key=f"writer:{index}",
                    content=f"writer fact {index}",
                    source_type="explicit",
                )
            )
        )
    return repository, service, rows


async def test_empty_and_healthy_activation_windows_do_not_acquire_writer(database):
    repository, service, rows = await facts(database)
    worker = MemoryMaintenanceWorker(settings=make_settings(database.url), facts=service)
    async with other_writer_and_read_only(database):
        assert await worker.process_once() == 0
    async with repository.transaction() as session:
        await session.execute(delete(MemoryFactModel).where(MemoryFactModel.id == rows[0].id))
    async with other_writer_and_read_only(database):
        assert await worker.process_once() == 0


async def test_activation_window_advances_on_zero_repairs_wraps_and_ignores_new_tail(database):
    repository, service, rows = await facts(database, 6)
    ids = [row.id for row in rows]
    async with repository.transaction() as session:
        await session.execute(
            delete(MemoryActivationStateModel).where(MemoryActivationStateModel.fact_id == ids[-1])
        )
        await session.execute(
            update(MemoryActivationStateModel)
            .where(MemoryActivationStateModel.fact_id == ids[1])
            .values(recall_count=7, revision=4)
        )
    settings = make_settings(database.url, memory_maintenance_batch_limit=2)
    worker = MemoryMaintenanceWorker(settings=settings, facts=service)
    await worker.process_once()
    assert worker._activation_after_id == ids[1]
    assert worker._activation_through_id == ids[-1]
    appended = await service.remember(
        MemoryFactCreate(
            scope_type="self",
            visibility_type="global",
            category="test",
            memory_key="writer:new",
            content="new tail",
            source_type="explicit",
        )
    )
    async with repository.transaction() as session:
        await session.execute(
            delete(MemoryActivationStateModel).where(
                MemoryActivationStateModel.fact_id.in_((ids[0], appended.id))
            )
        )
    await worker.process_once()
    assert worker._activation_after_id == ids[3]
    await worker.process_once()
    assert worker._activation_after_id == 0 and worker._activation_through_id is None
    async with database.sessions() as reader:
        assert await reader.get(MemoryActivationStateModel, ids[-1]) is not None
        assert await reader.get(MemoryActivationStateModel, appended.id) is None
    # A completed pass revisits prior source IDs and picks up the new high-water.
    next_high_water = None
    for index in range(4):
        addition = await service.remember(
            MemoryFactCreate(
                scope_type="self",
                visibility_type="global",
                category="test",
                memory_key=f"writer:continuous:{index}",
                content=f"continuous tail {index}",
                source_type="explicit",
            )
        )
        if next_high_water is None:
            next_high_water = addition.id
        await worker.process_once()
        assert worker._activation_through_id == (next_high_water if index < 3 else None)
    async with database.sessions() as reader:
        assert await reader.get(MemoryActivationStateModel, ids[0]) is not None
        assert await reader.get(MemoryActivationStateModel, appended.id) is not None
        unchanged = await reader.get(MemoryActivationStateModel, ids[1])
        assert unchanged.recall_count == 7 and unchanged.revision == 4


async def test_activation_discovery_limits_source_before_missing_filter_and_sort(database):
    repository, _, _ = await facts(database, 5)
    captured = []

    def capture(_connection, _cursor, statement, parameters, *_args):
        if statement.startswith("SELECT") and "memory_facts" in statement:
            captured.append((statement, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        identities, missing, _ = await repository.activation_repair_window(
            after_id=0, through_id=None, limit=2
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert len(identities) == 2 and missing == ()
    window_sql, parameters = next((sql, args) for sql, args in captured if "LIMIT" in sql)
    assert "NOT (EXISTS" not in window_sql and "CASE" not in window_sql
    async with database.engine.connect() as connection:
        window_plan = (
            await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + window_sql, parameters)
        ).all()
        missing_sql, parameters = next((sql, args) for sql, args in captured if "CASE" in sql)
        missing_plan = (
            await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + missing_sql, parameters)
        ).all()
    assert any("SEARCH memory_facts USING INTEGER PRIMARY KEY" in row[3] for row in window_plan)
    assert not any("TEMP B-TREE" in row[3] for row in window_plan)
    assert any("SEARCH memory_facts USING INTEGER PRIMARY KEY" in row[3] for row in missing_plan)
    assert not any(row[3] == "SCAN memory_facts" for row in missing_plan)


@pytest.mark.parametrize("race", ["activation", "delete", "source"])
async def test_activation_commit_rechecks_candidate_and_preserves_concurrent_usage(database, race):
    repository, _, rows = await facts(database)
    identity = rows[0].id
    async with repository.transaction() as session:
        await session.execute(
            delete(MemoryActivationStateModel).where(MemoryActivationStateModel.fact_id == identity)
        )
    _, missing, _ = await repository.activation_repair_window(after_id=0, through_id=None, limit=2)
    async with repository.transaction() as session:
        if race == "activation":
            session.add(
                MemoryActivationStateModel(
                    fact_id=identity,
                    activation=0.99,
                    activation_updated_at=datetime.now(UTC),
                    recall_count=8,
                    revision=3,
                )
            )
        elif race == "delete":
            await session.execute(delete(MemoryFactModel).where(MemoryFactModel.id == identity))
        else:
            await session.execute(
                update(MemoryFactModel)
                .where(MemoryFactModel.id == identity)
                .values(source_type="automatic", authority="agent_reflection", kind="preference")
            )
    async with repository.transaction() as session:
        assert await repository.repair_missing_activation(fact_ids=missing, session=session) == (
            1 if race == "source" else 0
        )
    async with database.sessions() as reader:
        state = await reader.get(MemoryActivationStateModel, identity)
        if race == "activation":
            assert state.activation == 0.99 and state.recall_count == 8 and state.revision == 3
        elif race == "delete":
            assert state is None
        else:
            assert state.activation == 0.8 and state.recall_count == 0


async def governance(database, count=1):
    _, _, rows = await facts(database, count)
    repository = MemoryReflectionRepository(database)
    candidates = tuple(
        MemoryReflectionCandidate(MemoryReflectionIssue.CONTESTED, row.id) for row in rows
    )
    old = datetime.now(UTC) - timedelta(hours=1)
    assert await repository.enqueue(candidates, now=old) == count
    jobs = await repository.claim(limit=count, now=old)
    assert len(jobs) == count
    return repository, candidates, jobs


async def test_governance_empty_recovery_and_duplicate_enqueue_are_read_only(database):
    repository, candidates, _ = await governance(database)
    async with other_writer_and_read_only(database):
        assert await repository.recover_stale(before=datetime.now(UTC) - timedelta(days=1)) == 0
        assert await repository.enqueue(candidates) == 0


async def test_governance_recovery_pages_preserve_attempts_and_exhaustion(database):
    repository, _, jobs = await governance(database, 5)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryReflectionJobModel)
            .where(MemoryReflectionJobModel.id == jobs[-1].id)
            .values(attempts=3)
        )
    before = datetime.now(UTC) - timedelta(minutes=10)
    assert [await repository.recover_stale(before=before, limit=2) for _ in range(4)] == [
        2,
        2,
        1,
        0,
    ]
    assert [(await repository.get(job.id)).attempts for job in jobs] == [1, 1, 1, 1, 3]
    assert (await repository.get(jobs[-1].id)).status == "failed"
    assert [(await repository.get(job.id)).status for job in jobs[:-1]] == ["pending"] * 4


@pytest.mark.parametrize("change", ["completed", "new_claim", "attempts"])
async def test_governance_recovery_does_not_overwrite_changed_claim(database, monkeypatch, change):
    repository, _, jobs = await governance(database)
    original = database.immediate_session

    @asynccontextmanager
    async def interleaved_writer():
        async with original() as writer:
            values = (
                {"status": "completed"}
                if change == "completed"
                else (
                    {"claimed_at": datetime.now(UTC)} if change == "new_claim" else {"attempts": 2}
                )
            )
            await writer.execute(
                update(MemoryReflectionJobModel)
                .where(MemoryReflectionJobModel.id == jobs[0].id)
                .values(**values)
            )
        async with original() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", interleaved_writer)
    assert await repository.recover_stale(before=datetime.now(UTC) - timedelta(minutes=10)) == 0
    current = await repository.get(jobs[0].id)
    assert current.status == ("completed" if change == "completed" else "processing")
    assert current.attempts == (2 if change == "attempts" else 1)
