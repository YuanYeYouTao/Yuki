"""Invalid reflection mappings stay read-only; surviving mappings recheck under writer."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select
from tests.unit.test_evidence_compaction_preparation import _seed

from qq_ai_bot.persistence.models import (
    MemorySelfReflectionResultModel,
    MemorySelfReflectionRunModel,
)


async def _duplicate_run(session, run):
    session.add(
        MemorySelfReflectionRunModel(
            conversation_key_hash="duplicate",
            bot_user_id=run.bot_user_id,
            canonical_person_id=run.canonical_person_id,
            scheduled_slot=str(uuid4()),
            trigger_reason="test",
            first_event_id=run.first_event_id,
            last_event_id=run.last_event_id,
            status="completed",
            started_at=datetime.now(UTC),
        )
    )


@pytest.mark.parametrize("mapping", ["ambiguous", "missing", "mixed_invalid"])
async def test_invalid_reflection_backfill_never_opens_writer(database, mapping):
    _, service, _, _, run = await _seed(database)
    async with database.immediate_session() as session:
        if mapping != "missing":
            await _duplicate_run(session, run)
        else:
            await session.execute(
                delete(MemorySelfReflectionRunModel).where(
                    MemorySelfReflectionRunModel.id == run.id
                )
            )
    if mapping == "mixed_invalid":
        _, _, _, _, other = await _seed(database)
        async with database.immediate_session() as session:
            await session.execute(
                delete(MemorySelfReflectionRunModel).where(
                    MemorySelfReflectionRunModel.id == other.id
                )
            )
    statements = []

    def observe(_connection, _cursor, statement, *_args):
        statements.append(statement.lstrip().upper())

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        await service._backfill_reflection_results()
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
    assert sum(statement.startswith("SELECT") for statement in statements) == 2
    assert not any(
        statement.startswith(("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE", "REPLACE"))
        for statement in statements
    )
    async with database.sessions() as session:
        assert list(await session.scalars(select(MemorySelfReflectionResultModel))) == []


@pytest.mark.parametrize("change", ["ambiguous", "missing"])
async def test_unique_mapping_changed_before_writer_is_rejected(database, monkeypatch, change):
    _, service, _, _, run = await _seed(database)
    original = database.immediate_session
    attempts = 0

    @asynccontextmanager
    async def concurrent_change():
        nonlocal attempts
        attempts += 1
        # The preparation session has closed. A separate committed writer wins
        # before this operation acquires its writer; no mock query results.
        async with original() as competitor:
            if change == "ambiguous":
                await _duplicate_run(competitor, run)
            else:
                await competitor.execute(
                    delete(MemorySelfReflectionRunModel).where(
                        MemorySelfReflectionRunModel.id == run.id
                    )
                )
        async with original() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", concurrent_change)
    await service._backfill_reflection_results()
    assert attempts == 1
    async with database.sessions() as session:
        assert list(await session.scalars(select(MemorySelfReflectionResultModel))) == []


async def test_unique_mapping_still_backfills_once_and_retains_original_ids(database):
    _, service, fact, _, run = await _seed(database)
    await service._backfill_reflection_results()
    await service._backfill_reflection_results()
    async with database.sessions() as session:
        results = list(await session.scalars(select(MemorySelfReflectionResultModel)))
        assert [(row.fact_id, row.run_id, row.result_kind) for row in results] == [
            (fact.id, run.id, "episode")
        ]
