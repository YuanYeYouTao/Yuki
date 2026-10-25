"""Due claims stream into the original executor without losing later pages."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

from sqlalchemy import insert, select, update
from tests.support.automation_claims import collect_due
from tests.support.automation_unified_delivery_helpers import setup_run

from yuki.persistence.models import AutomationModel, AutomationVersionModel


async def due_tasks(database, tmp_path, count=260):
    case = await setup_run(database, tmp_path, mode="silent")
    async with database.read_session() as reader:
        row = await reader.get(AutomationModel, case.row.id)
        version = await reader.get(AutomationVersionModel, case.row.version_id)
        definition = {column.name: getattr(row, column.name) for column in row.__table__.columns}
        body = {column.name: getattr(version, column.name) for column in version.__table__.columns}
    now = case.row.next_run_at
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(AutomationModel),
            [
                {**definition, "id": identity, "next_run_at": now - timedelta(seconds=identity % 3)}
                for identity in range(2, count + 1)
            ],
        )
        await writer.execute(
            insert(AutomationVersionModel),
            [
                {**body, "id": identity, "automation_id": identity}
                for identity in range(2, count + 1)
            ],
        )
    return case, now


async def test_first_claim_reaches_executor_before_later_pages_are_claimed(
    database, tmp_path, monkeypatch
):
    case, now = await due_tasks(database, tmp_path)
    first_started = asyncio.Event()
    all_started = asyncio.Event()
    remaining_reached = asyncio.Event()
    started = []
    allow_later_page = asyncio.Event()
    immediate = database.immediate_session
    writes = 0

    @asynccontextmanager
    async def claim_writer():
        nonlocal writes
        writes += 1
        if writes == 2:
            await allow_later_page.wait()
        async with immediate() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", claim_writer)

    async def execute(row):
        started.append(row.id)
        first_started.set()
        if len(started) == 260:
            all_started.set()
        await remaining_reached.wait()

    case.worker._process_guarded = execute
    # The real worker creates tasks as each committed page becomes available.
    case.clock.advance((now - case.clock.now()).total_seconds())
    loop = asyncio.create_task(case.worker._loop())
    try:
        await asyncio.wait_for(first_started.wait(), 5)
        async with database.read_session() as reader:
            claimed = list(
                await reader.scalars(
                    select(AutomationModel.id).where(AutomationModel.claimed_by.is_not(None))
                )
            )
        assert 0 < len(claimed) < 260
        allow_later_page.set()
        await asyncio.wait_for(all_started.wait(), 5)
        remaining_reached.set()
        assert len(set(started)) == 260
    finally:
        remaining_reached.set()
        allow_later_page.set()
        case.worker._stop.set()
        loop.cancel()
        await asyncio.gather(loop, *case.worker._running, return_exceptions=True)


async def test_competing_empty_first_claim_page_does_not_hide_later_occurrences(
    database, tmp_path, monkeypatch
):
    case, now = await due_tasks(database, tmp_path)
    immediate = database.immediate_session
    interrupted = False

    @asynccontextmanager
    async def competing():
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            async with immediate() as writer:
                ids = list(
                    await writer.scalars(
                        select(AutomationModel.id)
                        .order_by(AutomationModel.next_run_at, AutomationModel.id)
                        .limit(128)
                    )
                )
                await writer.execute(
                    update(AutomationModel)
                    .where(AutomationModel.id.in_(ids))
                    .values(claimed_by="other-owner", claimed_until=now + timedelta(seconds=60))
                )
        async with immediate() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", competing)
    claimed = await collect_due(
        case.repository, worker_id="this-owner", now=now, lease_seconds=60, clock=lambda: now
    )
    expected = sorted(
        range(1, 261), key=lambda identity: (-(identity % 3) if identity > 1 else 0, identity)
    )[128:]
    assert [row.id for row in claimed] == expected
    assert all(row.version_id == row.id for row in claimed)
    assert all(row.script == case.row.script for row in claimed)
    assert not case.provider.requests
