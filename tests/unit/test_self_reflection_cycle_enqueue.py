"""Repeated schedule slots reuse their durable cycle without taking the writer."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.support.projection_sql_counts import capture_sql
from tests.unit.test_memory_v2 import _append_event

from yuki.memory.self_reflection.control import ReflectionControlRepository
from yuki.persistence.models import MemorySelfReflectionCycleModel as Cycle
from yuki.persistence.repositories import EventLedgerRepository


async def _stored_cycle(database, key):
    async with database.read_session() as session:
        return (
            (await session.execute(select(Cycle.__table__).where(Cycle.source_key == key)))
            .mappings()
            .one()
        )


@pytest.mark.parametrize("trigger", ["scheduled", "manual"])
async def test_existing_cycle_skips_snapshot_and_writer(database, monkeypatch, trigger):
    event = await _append_event(EventLedgerRepository(database), message_id="cycle-original")
    repository = ReflectionControlRepository(database, make_settings(database.url))
    snapshot = AsyncMock(return_value={"calls_today": 7})
    monkeypatch.setattr(repository, "snapshot", snapshot)
    arguments = {
        "trigger": trigger,
        "key": "original-cycle",
        "source_event_id": event.id if trigger == "manual" else None,
        "conversation_id": event.canonical_conversation_id if trigger == "manual" else None,
    }
    original = await repository.enqueue(**arguments)
    snapshot.assert_awaited_once()
    before = await _stored_cycle(database, arguments["key"])
    snapshot.side_effect = AssertionError("existing cycle must not aggregate a new snapshot")

    async with database.immediate_session():
        with capture_sql(database) as statements:
            assert await asyncio.wait_for(repository.enqueue(**arguments), 0.5) == original
        assert statements and all(
            sql.lstrip().upper().startswith("SELECT") for sql, _ in statements
        )

    snapshot.assert_awaited_once()
    assert await _stored_cycle(database, arguments["key"]) == before


@pytest.mark.parametrize("invalid_source", ["missing", "other_conversation"])
async def test_existing_manual_cycle_still_validates_real_source(
    database, monkeypatch, invalid_source
):
    ledger = EventLedgerRepository(database)
    event = await _append_event(ledger, message_id="manual-original")
    repository = ReflectionControlRepository(database, make_settings(database.url))
    snapshot = AsyncMock(return_value={})
    monkeypatch.setattr(repository, "snapshot", snapshot)
    original = await repository.enqueue(
        trigger="manual",
        key="manual-cycle",
        source_event_id=event.id,
        conversation_id=event.canonical_conversation_id,
    )
    snapshot.side_effect = AssertionError("invalid source must not aggregate a snapshot")

    async with database.immediate_session():
        with capture_sql(database) as statements:
            with pytest.raises(ValueError, match="original internal event"):
                await asyncio.wait_for(
                    repository.enqueue(
                        trigger="manual",
                        key="manual-cycle",
                        source_event_id=event.id if invalid_source != "missing" else 999999,
                        conversation_id=(
                            event.canonical_conversation_id
                            if invalid_source != "other_conversation"
                            else "other-conversation"
                        ),
                    ),
                    0.5,
                )
        assert statements and all(
            sql.lstrip().upper().startswith("SELECT") for sql, _ in statements
        )

    snapshot.assert_awaited_once()
    assert await repository.get(original["id"]) == original


async def test_new_manual_cycle_race_keeps_one_original_cycle(database, monkeypatch):
    event = await _append_event(EventLedgerRepository(database), message_id="manual-race")
    repository = ReflectionControlRepository(database, make_settings(database.url))
    both_prepared = asyncio.Event()
    prepared = 0

    async def snapshot():
        nonlocal prepared
        prepared += 1
        if prepared == 2:
            both_prepared.set()
        await asyncio.wait_for(both_prepared.wait(), 2)
        return {"calls_today": 4}

    monkeypatch.setattr(repository, "snapshot", snapshot)
    arguments = {
        "trigger": "manual",
        "key": "racing-cycle",
        "source_event_id": event.id,
        "conversation_id": event.canonical_conversation_id,
    }
    first, second = await asyncio.wait_for(
        asyncio.gather(repository.enqueue(**arguments), repository.enqueue(**arguments)), 5
    )
    assert first == second
    assert first["source_event_id"] == event.id
    assert first["conversation_id"] == event.canonical_conversation_id
    assert first["before"] == {"calls_today": 4}
    assert first["delivery_state"] == "pending"
    assert prepared == 2
    async with database.read_session() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Cycle).where(Cycle.source_key == "racing-cycle")
            )
            == 1
        )


@pytest.mark.parametrize("change", ["delete", "identity"])
async def test_manual_cycle_rechecks_original_event_after_snapshot(database, monkeypatch, change):
    from sqlalchemy import delete, update

    from yuki.persistence.models import ChatEventModel

    event = await _append_event(EventLedgerRepository(database), message_id="manual-source-race")
    repository = ReflectionControlRepository(database, make_settings(database.url))

    async def snapshot():
        async with database.immediate_session() as writer:
            if change == "delete":
                await writer.execute(delete(ChatEventModel).where(ChatEventModel.id == event.id))
            else:
                from uuid import uuid4

                await writer.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == event.id)
                    .values(canonical_event_id=str(uuid4()))
                )
        return {}

    monkeypatch.setattr(repository, "snapshot", snapshot)
    with pytest.raises(ValueError, match="original internal event"):
        await repository.enqueue(
            trigger="manual",
            key="removed-event",
            source_event_id=event.id,
            conversation_id=event.canonical_conversation_id,
        )
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count()).select_from(Cycle)) == 0


async def test_request_day_uses_time_after_writer_admission(database, monkeypatch):
    from datetime import UTC, datetime

    from tests.unit.test_self_reflection_priority import reflection_worker

    from yuki.memory.self_reflection import control
    from yuki.persistence.models import MemorySelfReflectionRequestModel as Request

    worker, repository, _, _ = await reflection_worker(database)
    await repository.scan_new_events()
    (batch,) = await repository.claim_due(
        scheduled_slot="clock",
        event_threshold=1,
        character_threshold=1,
        max_wait_seconds=0,
        max_events=10,
        max_characters=10000,
    )

    class Clock(datetime):
        value = datetime(2026, 10, 22, 15, 59, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)

    monkeypatch.setattr(control, "datetime", Clock)
    async with database.immediate_session():
        task = asyncio.create_task(worker.control.reserve_request(batch.run_id))
        await asyncio.sleep(0)
        assert not task.done()
        Clock.value = datetime(2026, 10, 22, 16, 1, tzinfo=UTC)
    request_id = await task
    async with database.read_session() as reader:
        request = await reader.get(Request, request_id)
        assert request.run_id == batch.run_id and request.local_date == "2026-10-23"
