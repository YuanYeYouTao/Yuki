"""Idle compaction never creates a writer; interrupted work keeps its IDs."""

from sqlalchemy import event, select
from tests.conftest import make_settings
from tests.unit.test_evidence_compaction_preparation import _seed

from qq_ai_bot.memory.dream.db_models import (
    MemoryEvidenceCompactionItemModel,
    MemoryEvidenceCompactionRunModel,
)
from qq_ai_bot.memory.evidence_compaction import EvidenceCompactionService
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService


async def _idle_statements(database, service):
    statements = []

    def observe(_connection, _cursor, statement, *_args):
        statements.append(statement.lstrip().upper())

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        assert await service.run_batch() == 0
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
    assert not any(
        statement.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN IMMEDIATE"))
        for statement in statements
    )
    return statements


async def test_repeated_idle_polls_have_no_dml_or_empty_runs(database):
    service = EvidenceCompactionService(
        settings=make_settings(database.url),
        database=database,
        facts=MemoryFactService(MemoryFactRepository(database)),
    )
    for _ in range(3):
        statements = await _idle_statements(database, service)
        assert sum(statement.startswith("SELECT") for statement in statements) == 3
    async with database.sessions() as session:
        assert list(await session.scalars(select(MemoryEvidenceCompactionRunModel))) == []


async def test_running_run_without_processing_items_reuses_id_without_dml(database):
    service = EvidenceCompactionService(
        settings=make_settings(database.url),
        database=database,
        facts=MemoryFactService(MemoryFactRepository(database)),
    )
    run_id = await service._ensure_run()
    statements = []

    def observe(_connection, _cursor, statement, *_args):
        statements.append(statement.lstrip().upper())

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        assert await service._ensure_run() == run_id
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
    assert not any(statement.startswith(("INSERT", "UPDATE")) for statement in statements)
    # Closing genuinely unfinished state is one necessary transition. Later
    # polls must leave its original public ID, counters and timestamps intact.
    assert await service.run_batch() == 0
    async with database.sessions() as session:
        row = await session.get(MemoryEvidenceCompactionRunModel, run_id)
        before = (row.public_id, row.status, row.updated_at, row.completed_at)
        assert row.status == "completed"
    await _idle_statements(database, service)
    async with database.sessions() as session:
        rows = list(await session.scalars(select(MemoryEvidenceCompactionRunModel)))
        assert len(rows) == 1
        assert (
            rows[0].public_id,
            rows[0].status,
            rows[0].updated_at,
            rows[0].completed_at,
        ) == before


async def test_interrupted_compaction_resumes_original_run_and_item(database):
    _, service, fact, _, _ = await _seed(database)
    await service._backfill_reflection_results()
    run_id = await service._ensure_run()
    item_id = await service._claim_item(
        run_id=run_id,
        fact_id=fact.id,
        provenance="self_reflection",
        operation_id=None,
        evidence_before=13,
    )
    # A new service instance models restart, with no in-memory recovery state.
    recovered = EvidenceCompactionService(
        settings=make_settings(database.url),
        database=database,
        facts=MemoryFactService(MemoryFactRepository(database)),
    )
    assert await recovered.run_batch() == 1
    assert await recovered.run_batch() == 0
    async with database.sessions() as session:
        rows = list(await session.scalars(select(MemoryEvidenceCompactionRunModel)))
        items = list(await session.scalars(select(MemoryEvidenceCompactionItemModel)))
        assert len(rows) == len(items) == 1
        assert rows[0].id == run_id
        assert rows[0].status == "completed"
        assert rows[0].completed_items == 1
        assert (rows[0].evidence_before, rows[0].evidence_after) == (13, 8)
        assert items[0].id == item_id
        assert items[0].status == "completed"
    await _idle_statements(database, recovered)
