"""Idle maintenance seeks due facts/runs, rather than traversing retained history."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select
from tests.conftest import make_settings
from tests.support.sqlite_cost import capture_reads, read_vm_steps

from yuki.identity.db_models import CanonicalPersonModel
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.self_reflection.control import ReflectionControlRepository
from yuki.persistence.models import (
    MemoryFactModel,
    MemorySelfReflectionCycleModel,
    MemorySelfReflectionRunModel,
)

NOW = datetime(2026, 10, 20, tzinfo=UTC)


async def _facts(database, start, stop, *, expired=False):
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(MemoryFactModel),
            [
                {
                    "scope_type": "self",
                    "visibility_type": "global",
                    "kind": "fact",
                    "memory_key": f"retained:{index}",
                    "category": "fixture",
                    "content": f"retained fact {index}",
                    "normalized_content": f"retained fact {index}",
                    "source_type": "automatic",
                    "created_at": NOW,
                    "updated_at": NOW,
                    "valid_until": NOW - timedelta(days=index + 1)
                    if expired
                    else NOW + timedelta(days=1)
                    if index % 2
                    else None,
                }
                for index in range(start, stop)
            ],
        )


async def test_expiry_empty_seek_does_not_walk_nonexpiring_or_future_facts(database):
    repository = MemoryFactRepository(database)
    costs = []
    for start, stop in ((0, 50), (50, 2000)):
        await _facts(database, start, stop)
        with capture_reads(database) as statements:
            assert await repository.list_expired_candidates(now=NOW, limit=10) == ()
        costs.append(read_vm_steps(database, statements))
    assert costs[1] <= costs[0] * 3

    await _facts(database, 2000, 2003, expired=True)
    expired = await repository.list_expired_candidates(now=NOW, limit=2)
    assert [fact.memory_key for fact in expired] == ["retained:2002", "retained:2001"]


async def _reflection_history(database, start, stop):
    async with database.immediate_session() as writer:
        person = await writer.scalar(select(CanonicalPersonModel.id).limit(1))
        await writer.execute(
            insert(MemorySelfReflectionCycleModel),
            [
                {
                    "id": f"retained-cycle-{index}",
                    "source_key": f"retained-cycle-{index}",
                    "trigger": "manual",
                    "status": "completed",
                    "created_at": NOW,
                    "delivery_state": "delivered",
                }
                for index in range(start, stop)
            ],
        )
        await writer.execute(
            insert(MemorySelfReflectionRunModel),
            [
                {
                    "conversation_key_hash": "retained-person",
                    "bot_user_id": "fixture",
                    "canonical_person_id": person,
                    "scheduled_slot": f"retained:{index}",
                    "trigger_reason": "fixture",
                    "first_event_id": 1,
                    "last_event_id": 1,
                    "status": "completed",
                    "started_at": NOW,
                }
                for index in range(start, stop)
            ],
        )


@pytest.mark.parametrize("method", ["claim", "pending_reports"])
async def test_idle_reflection_seeks_ignore_closed_history(database, method):
    repository = ReflectionControlRepository(database, make_settings(database.url))
    costs = []
    expected = {"claim": None, "pending_reports": []}[method]
    for start, stop in ((0, 50), (50, 2000)):
        await _reflection_history(database, start, stop)
        with capture_reads(database) as statements:
            assert await getattr(repository, method)() == expected
        costs.append(read_vm_steps(database, statements))
    assert costs[1] <= costs[0] * 3


async def test_reflection_indexes_preserve_due_order_retry_and_original_report(database):
    repository = ReflectionControlRepository(database, make_settings(database.url))
    await _reflection_history(database, 0, 1)
    async with database.immediate_session() as writer:
        writer.add_all(
            [
                MemorySelfReflectionCycleModel(
                    id=identity,
                    source_key=identity,
                    trigger="manual",
                    status=status,
                    created_at=created,
                    report_json='{"original":"paid-result"}',
                    delivery_state="pending",
                )
                for identity, status, created in (
                    ("newer-queued", "queued", NOW),
                    ("original-running", "running", NOW - timedelta(days=1)),
                    ("original-report", "completed", NOW - timedelta(days=2)),
                )
            ]
        )
        run = await writer.scalar(select(MemorySelfReflectionRunModel))
        run.retry_state = "waiting"
        run.next_attempt_at = NOW - timedelta(days=1)
    claimed = await repository.claim()
    assert claimed["id"] == "original-running"
    reports = await repository.pending_reports()
    assert [report["id"] for report in reports] == ["original-report"]
    assert reports[0]["original"] == "paid-result"
