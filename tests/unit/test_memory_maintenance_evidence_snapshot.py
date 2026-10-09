"""The maintenance batch prepares all history before its first fact write."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, update
from tests.conftest import make_settings

from qq_ai_bot.memory.maintenance import MemoryMaintenanceWorker
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.models import MemoryFactModel


@pytest.mark.parametrize("race", [False, True])
async def test_maintenance_prepares_entire_invalidation_batch_before_any_write(
    database, monkeypatch, race
):
    repository = MemoryFactRepository(database)
    facts = MemoryFactService(repository)
    rows = []
    for index in range(2):
        rows.append(
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
        )
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
        worker = MemoryMaintenanceWorker(settings=make_settings(database.url), facts=facts)
        assert await worker.process_once() == 2
    finally:
        event.remove(engine, "before_cursor_execute", trace)
        event.remove(engine, "commit", reset)
        event.remove(engine, "rollback", reset)
    assert prepared == [tuple(row.id for row in rows)] * (2 if race else 1)
    assert historical_reads_after_write == []
    for row in rows:
        assert (await facts.get_fact(row.id)).status.value == "invalidated"
