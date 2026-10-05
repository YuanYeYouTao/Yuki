"""Restart recovery is read-only when empty and remains an atomic state pause."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event, select, text, update
from tests.unit.test_memory_rebuild import _event, _service

from qq_ai_bot.memory.enums import MemoryRebuildRunStatus
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.persistence.models import MemoryRebuildItemModel, MemoryRebuildRunModel


def _observe(database):
    statements = []

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement.lstrip().upper())

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    return statements, capture


async def _seed(database, *, status="extracting", item_status="extracting"):
    _settings, ledger, _facts, provider, service = await _service(database)
    source = await _event(ledger, message_id=f"restart-source-{status}")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    item_id, _claimed, _state = await service.repository.ensure_item(
        run.public_id, event_id=source.id, source_event_hash="original-source-hash"
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryRebuildRunModel)
            .where(MemoryRebuildRunModel.public_id == run.public_id)
            .values(
                status=status,
                extraction_requests=3,
                consolidation_requests=2,
                input_tokens=100,
                output_tokens=20,
                latency_milliseconds=45,
            )
        )
        await writer.execute(
            update(MemoryRebuildItemModel)
            .where(MemoryRebuildItemModel.id == item_id)
            .values(status=item_status, attempts=2, claim_count=1)
        )
    return service, provider, run.public_id, item_id


@pytest.mark.parametrize("held_writer", [False, True])
async def test_empty_restart_pause_never_reserves_writer(database, held_writer):
    repository = MemoryRebuildRepository(database)
    statements, capture = _observe(database)
    try:
        if held_writer:
            async with database.immediate_session():
                statements.clear()
                assert await asyncio.wait_for(repository.pause_after_restart(), timeout=1) == 0
        else:
            assert await repository.pause_after_restart() == 0
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert statements[0] == "BEGIN"
    assert sum(sql.startswith("SELECT") for sql in statements) == 2
    assert not any(
        sql.startswith(("BEGIN IMMEDIATE", "UPDATE", "INSERT", "DELETE")) for sql in statements
    )
    assert all("LIMIT" in sql for sql in statements if sql.startswith("SELECT"))


@pytest.mark.parametrize(
    ("status", "item_status", "expected", "expected_item", "count"),
    [
        ("extracting", "extracting", "extraction_paused", "pending", 1),
        ("committing", "extracting", "commit_paused", "pending", 1),
        ("review", "extracting", "review", "pending", 0),
        ("extracting", "committed", "extraction_paused", "committed", 1),
        ("review", "committed", "review", "committed", 0),
    ],
)
async def test_pause_retains_original_ids_usage_and_item_count_semantics(
    database, status, item_status, expected, expected_item, count
):
    service, provider, public_id, item_id = await _seed(
        database, status=status, item_status=item_status
    )
    async with database.sessions() as reader:
        before_run = await reader.scalar(
            select(MemoryRebuildRunModel).where(MemoryRebuildRunModel.public_id == public_id)
        )
        before_item = await reader.get(MemoryRebuildItemModel, item_id)
    statements, capture = _observe(database)
    try:
        assert await service.repository.pause_after_restart() == count
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    async with database.sessions() as reader:
        after_run = await reader.get(MemoryRebuildRunModel, before_run.id)
        after_item = await reader.get(MemoryRebuildItemModel, item_id)
    assert after_run.status == expected and after_item.status == expected_item
    assert provider.requests == 0
    for field in (
        "id",
        "public_id",
        "selection_json",
        "selection_hash",
        "snapshot_max_event_id",
        "extraction_requests",
        "consolidation_requests",
        "input_tokens",
        "output_tokens",
        "latency_milliseconds",
        "scan_checkpoint_event_id",
        "commit_checkpoint_event_id",
    ):
        assert getattr(after_run, field) == getattr(before_run, field)
    for field in ("id", "run_id", "event_id", "source_event_hash", "attempts", "claim_count"):
        assert getattr(after_item, field) == getattr(before_item, field)
    changed = status in {"extracting", "committing"} or item_status == "extracting"
    assert sum(sql.startswith("BEGIN IMMEDIATE") for sql in statements) == int(changed)
    assert sum(sql.startswith("UPDATE") for sql in statements) == (3 if changed else 0)
    assert await service.repository.pause_after_restart() == 0


async def test_candidate_committed_between_probes_is_not_mixed_into_empty_snapshot(
    database, monkeypatch
):
    service, _provider, public_id, item_id = await _seed(
        database, status="review", item_status="pending"
    )
    original = database.sessions
    injected = False

    class SnapshotSession:
        def __init__(self):
            self.session = original()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return await self.session.__aexit__(*args)

        async def execute(self, *args, **kwargs):
            return await self.session.execute(*args, **kwargs)

        async def scalar(self, *args, **kwargs):
            nonlocal injected
            value = await self.session.scalar(*args, **kwargs)
            if not injected:
                injected = True
                async with original() as writer:
                    await writer.execute(text("BEGIN IMMEDIATE"))
                    await writer.execute(
                        update(MemoryRebuildRunModel)
                        .where(MemoryRebuildRunModel.public_id == public_id)
                        .values(status="extracting")
                    )
                    await writer.execute(
                        update(MemoryRebuildItemModel)
                        .where(MemoryRebuildItemModel.id == item_id)
                        .values(status="extracting")
                    )
                    await writer.commit()
            return value

    monkeypatch.setattr(database, "sessions", SnapshotSession)
    assert await service.repository.pause_after_restart() == 0
    monkeypatch.setattr(database, "sessions", original)
    assert injected
    run = await service.repository.get_run(public_id)
    assert run.status is MemoryRebuildRunStatus.EXTRACTING
    # A fresh snapshot sees the committed candidate; no partial rewind was made.
    assert await service.repository.pause_after_restart() == 1


async def test_state_change_after_discovery_is_rechecked_under_writer(database, monkeypatch):
    service, _provider, public_id, item_id = await _seed(database)
    original = database.immediate_session
    injected = False

    @asynccontextmanager
    async def raced():
        nonlocal injected
        if not injected:
            injected = True
            async with original() as writer:
                await writer.execute(
                    update(MemoryRebuildRunModel)
                    .where(MemoryRebuildRunModel.public_id == public_id)
                    .values(status="completed")
                )
                await writer.execute(
                    update(MemoryRebuildItemModel)
                    .where(MemoryRebuildItemModel.id == item_id)
                    .values(status="committed")
                )
        async with original() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", raced)
    assert await service.repository.pause_after_restart() == 0
    run = await service.repository.get_run(public_id)
    assert run.status is MemoryRebuildRunStatus.COMPLETED
    async with database.sessions() as reader:
        item = await reader.get(MemoryRebuildItemModel, item_id)
    assert item.status == "committed" and item.error_category is None


async def test_mixed_run_pauses_and_all_items_commit_in_one_transaction(database):
    seeded = [
        await _seed(database, status=status) for status in ("extracting", "committing", "review")
    ]
    repository = seeded[0][0].repository
    statements, capture = _observe(database)
    try:
        assert await repository.pause_after_restart() == 2
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert sum(sql.startswith("BEGIN IMMEDIATE") for sql in statements) == 1
    assert sum(sql.startswith("UPDATE") for sql in statements) == 3
    for (_seeded_service, provider, public_id, item_id), expected in zip(
        seeded, ("extraction_paused", "commit_paused", "review"), strict=True
    ):
        run = await repository.get_run(public_id)
        assert run.status.value == expected
        assert provider.requests == 0
        async with database.sessions() as reader:
            stored_run = await reader.scalar(
                select(MemoryRebuildRunModel).where(MemoryRebuildRunModel.public_id == public_id)
            )
            item = await reader.get(MemoryRebuildItemModel, item_id)
        assert (stored_run.extraction_requests, stored_run.consolidation_requests) == (3, 2)
        assert item.status == "pending" and item.error_category == "process_restart"
    assert await repository.pause_after_restart() == 0


async def test_failed_second_update_rolls_back_entire_pause(database, monkeypatch):
    service, _provider, public_id, item_id = await _seed(database)
    original = database.immediate_session

    class Writer:
        def __init__(self, session):
            self.session = session
            self.count = 0

        async def execute(self, *args, **kwargs):
            self.count += 1
            if self.count == 2:
                raise RuntimeError("synthetic second update failure")
            return await self.session.execute(*args, **kwargs)

    @asynccontextmanager
    async def failing():
        async with original() as writer:
            yield Writer(writer)

    monkeypatch.setattr(database, "immediate_session", failing)
    with pytest.raises(RuntimeError, match="second update failure"):
        await service.repository.pause_after_restart()
    run = await service.repository.get_run(public_id)
    assert run.status is MemoryRebuildRunStatus.EXTRACTING
    async with database.sessions() as reader:
        item = await reader.get(MemoryRebuildItemModel, item_id)
    assert item.status == "extracting" and item.error_category is None


async def test_committed_pause_with_lost_ack_is_not_retried_or_auto_resumed(database, monkeypatch):
    service, provider, public_id, item_id = await _seed(database)
    original = database.immediate_session

    @asynccontextmanager
    async def lost_ack():
        async with original() as writer:
            yield writer
        raise OSError("synthetic commit acknowledgement lost")

    monkeypatch.setattr(database, "immediate_session", lost_ack)
    with pytest.raises(OSError, match="acknowledgement lost"):
        await service.repository.pause_after_restart()
    monkeypatch.setattr(database, "immediate_session", original)
    run = await service.repository.get_run(public_id)
    assert run.status is MemoryRebuildRunStatus.EXTRACTION_PAUSED
    assert provider.requests == 0
    statements, capture = _observe(database)
    try:
        assert await service.repository.pause_after_restart() == 0
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert not any(sql.startswith("UPDATE") for sql in statements)
    async with database.sessions() as reader:
        item = await reader.get(MemoryRebuildItemModel, item_id)
    assert item.status == "pending" and item.attempts == 2
