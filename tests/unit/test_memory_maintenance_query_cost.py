"""Maintenance keeps due order, requested limits and original paid reports."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import insert
from tests.conftest import make_settings

from yuki.memory.repository import MemoryFactRepository
from yuki.memory.self_reflection.control import ReflectionControlRepository
from yuki.persistence.models import MemoryFactModel, MemorySelfReflectionCycleModel

NOW = datetime(2026, 10, 20, tzinfo=UTC)


async def test_expired_candidates_keep_due_order_and_requested_limit(database):
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(MemoryFactModel),
            [
                dict(
                    scope_type="self",
                    visibility_type="global",
                    kind="fact",
                    memory_key=f"expiry:{index}",
                    category="fixture",
                    content="original fact",
                    normalized_content="original fact",
                    source_type="automatic",
                    created_at=NOW,
                    updated_at=NOW,
                    valid_until=expiry,
                )
                for index, expiry in enumerate(
                    [None, NOW + timedelta(days=1), *(NOW - timedelta(days=n) for n in (1, 2, 3))]
                )
            ],
        )
    expired = await MemoryFactRepository(database).list_expired_candidates(now=NOW, limit=2)
    assert [fact.memory_key for fact in expired] == ["expiry:4", "expiry:3"]


async def test_reflection_claim_keeps_due_order_and_original_report(database):
    repository = ReflectionControlRepository(database, make_settings(database.url))
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
    assert (await repository.claim())["id"] == "original-running"
    reports = await repository.pending_reports()
    assert [report["id"] for report in reports] == ["original-report"]
    assert reports[0]["original"] == "paid-result"
