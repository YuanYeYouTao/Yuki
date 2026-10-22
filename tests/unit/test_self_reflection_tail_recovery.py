"""A paid completion retains the exact pending tail under SQLite's parameter budget."""

import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.unit.test_memory_v2 import _append_event

from yuki.memory.self_reflection.repository import SelfReflectionRepository
from yuki.memory.self_reflection.service import _CHECKPOINT, ReflectionCheckpoint
from yuki.persistence.models import (
    ChatEventModel,
    MemorySelfReflectionRunModel,
    MemorySelfReflectionStateModel,
)
from yuki.persistence.repositories import EventLedgerRepository


@pytest.mark.parametrize("change_during_admission", [False, True])
async def test_paid_completion_recovers_once_without_losing_a_large_tail(
    database, monkeypatch, change_during_admission
):
    repository = SelfReflectionRepository(database)
    ledger = EventLedgerRepository(database)
    await repository.scan_new_events()
    first = await _append_event(ledger, message_id="completed-first", group_id="2001")
    await repository.scan_new_events()
    (batch,) = await repository.claim_due(
        scheduled_slot="tail",
        event_threshold=1,
        character_threshold=1,
        max_wait_seconds=0,
        max_events=1,
        max_characters=16000,
    )
    await repository.save_checkpoint(
        batch.run_id,
        _CHECKPOINT.dump_json(
            ReflectionCheckpoint({"proposals": [], "episodes": []}, {"event_1": first}, {}, (0, 0))
        ).decode(),
    )
    contents = [f"pending {index}: " + "unfinished source " * 80 for index in range(64)]
    tail = [
        await _append_event(ledger, message_id=f"tail-{index}", group_id="2001", content=content)
        for index, content in enumerate(contents)
    ]
    await repository.scan_new_events()
    immediate = database.immediate_session
    changed = False

    @asynccontextmanager
    async def admission():
        nonlocal changed
        if change_during_admission and not changed:
            changed = True
            contents[-1] = "current tail after source edit"
            async with immediate() as writer:
                await writer.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == tail[-1].id)
                    .values(content=contents[-1])
                )
        async with immediate() as writer:
            yield writer

    async with database.engine.connect() as connection:
        await connection.run_sync(
            lambda sync: sync.connection.dbapi_connection.run_async(
                lambda driver: driver._execute(
                    driver._conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 256
                )
            )
        )
        monkeypatch.setattr(
            database, "_sessions", async_sessionmaker(connection, expire_on_commit=False)
        )
        monkeypatch.setattr(database, "immediate_session", admission)
        assert await repository.recover_stale_runs(started_before=datetime.now(UTC)) == 1
        assert await repository.recover_stale_runs(started_before=datetime.now(UTC)) == 0
        async with database.read_session() as reader:
            run = await reader.get(MemorySelfReflectionRunModel, batch.run_id)
            assert run.status == "completed"
            assert run.checkpoint_json is None
            assert run.error_category == "recovered:stale_processing"
            state = (await reader.scalars(select(MemorySelfReflectionStateModel))).one()
            assert state.last_event_id == first.id
            assert state.latest_event_id == tail[-1].id
            assert state.pending_events == len(tail)
            assert state.pending_characters == sum(map(len, contents))
