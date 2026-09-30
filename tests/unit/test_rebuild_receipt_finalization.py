"""Bounded SQLite receipt finalization, including real prepare/write races."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import insert, select, update
from tests.unit.test_memory_rebuild import _service

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import MemoryRebuildRunStatus
from qq_ai_bot.memory.extraction import MemoryClaim, source_event_fingerprint
from qq_ai_bot.memory.rebuild import receipt_finalization
from qq_ai_bot.memory.rebuild.models import MemoryRebuildPlanStatistics, MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.memory.repository import MemoryJobRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryJobModel,
    MemoryRebuildItemModel,
    MemoryRebuildProposalModel,
    MemoryRebuildRunModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.persistence.repository_helpers import _event_record


@dataclass(frozen=True)
class _Seed:
    public_id: str
    run_id: int
    item_ids: tuple[int, ...]
    event_ids: tuple[int, ...]
    conversation_id: str


async def _seed(
    database: Database, count: int, *, proposals: bool = False, include_failed: bool = False
) -> _Seed:
    """Bulk staging fixture, preserving a ledger-created canonical owner/binding."""
    now = datetime.now(UTC)
    first, _ = await EventLedgerRepository(database).append(
        bot_user_id="8000",
        platform_message_id=str(uuid.uuid4()),
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        private_peer_user_id="1001",
        direction="inbound",
        content="我住在杭州",
    )
    repository = MemoryRebuildRepository(database)
    selection = MemoryRebuildSelection(all_events=True, include_failed_live_jobs=include_failed)
    run = await repository.create_run(
        selection=selection,
        selection_json=selection.model_dump_json(),
        selection_hash="a" * 64,
        snapshot_max_event_id=first.id + count,
        fingerprint="b" * 64,
        statistics=MemoryRebuildPlanStatistics(
            matched_events=count,
            eligible_events=count,
            already_processed=0,
            live_pending_processing=0,
            failed_live_jobs=0,
            private_events=count,
            group_events=0,
            input_characters=count * 6,
            estimated_extraction_requests=count,
        ),
        actor_user_id="9000",
    )
    async with database.sessions() as session, session.begin():
        original = await session.get(ChatEventModel, first.id)
        assert original is not None
        conversation_id = original.canonical_conversation_id
        assert conversation_id is not None
        event_ids = [first.id]
        if count > 1:
            template = {
                column.key: getattr(original, column.key)
                for column in ChatEventModel.__table__.columns
                if column.key not in {"id", "canonical_event_id", "platform_message_id"}
            }
            event_ids.extend(
                await session.scalars(
                    insert(ChatEventModel).returning(ChatEventModel.id),
                    [
                        dict(
                            template,
                            canonical_event_id=str(uuid.uuid4()),
                            platform_message_id=str(uuid.uuid4()),
                        )
                        for _ in range(count - 1)
                    ],
                )
            )
        run_id = await session.scalar(
            select(MemoryRebuildRunModel.id).where(MemoryRebuildRunModel.public_id == run.public_id)
        )
        assert run_id is not None
        await session.execute(
            update(MemoryRebuildRunModel)
            .where(MemoryRebuildRunModel.id == run_id)
            .values(status="committing")
        )
        hashes = {
            source.id: source_event_fingerprint(
                replace(_event_record(source), mentioned_user_ids=(), reply_sender_user_id=None)
            )
            for source in await session.scalars(
                select(ChatEventModel).where(ChatEventModel.id.in_(event_ids))
            )
        }
        item_ids = tuple(
            await session.scalars(
                insert(MemoryRebuildItemModel).returning(MemoryRebuildItemModel.id),
                [
                    dict(
                        run_id=run_id,
                        event_id=event_id,
                        status="staged" if proposals else "no_claims",
                        source_event_hash=hashes[event_id],
                        claim_count=int(proposals),
                        created_at=now,
                        updated_at=now,
                    )
                    for event_id in event_ids
                ],
            )
        )
        if proposals:
            await session.execute(
                insert(MemoryRebuildProposalModel),
                [
                    dict(
                        run_id=run_id,
                        item_id=item_id,
                        event_id=event_id,
                        claim_index=0,
                        claim_json='{"body":"must not be loaded by finalization"}',
                        claim_hash="d" * 64,
                        scope_type="person",
                        subject_user_id="1001",
                        operation="assert",
                        kind="fact",
                        authority="observed",
                        confidence=0.9,
                        review_status="approved",
                        commit_status="committed",
                        created_at=now,
                        updated_at=now,
                    )
                    for item_id, event_id in zip(item_ids, event_ids, strict=True)
                ],
            )
    return _Seed(run.public_id, run_id, item_ids, tuple(event_ids), conversation_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 10, 100, 1000])
async def test_finalization_read_budget_is_per_page_and_second_pass_is_readonly(
    database: Database, count: int
) -> None:
    seed = await _seed(database, count, proposals=True)
    repository = MemoryRebuildRepository(database)
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    sqlalchemy_event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        completed = await repository.complete_item_receipts(
            seed.public_id, include_failed_live_jobs=False, limit=128
        )
        assert completed == min(count, 128)
        # Run lookup/discovery and two three-read batches; no per-item owner,
        # evidence, proposal-body or receipt SELECTs after the first DML.
        selects = [statement for statement in statements if statement.startswith("SELECT")]
        assert len(selects) <= 10
        assert not any("CLAIM_JSON" in statement for statement in selects)
        first_write = next(
            index
            for index, statement in enumerate(statements)
            if statement.startswith(("INSERT", "UPDATE", "DELETE"))
        )
        assert not any(statement.startswith("SELECT") for statement in statements[first_write:])
        while await repository.remaining_item_receipt_count(seed.public_id):
            assert (
                await repository.complete_item_receipts(
                    seed.public_id, include_failed_live_jobs=False, limit=128
                )
                > 0
            )
        statements.clear()
        assert (
            await repository.complete_item_receipts(seed.public_id, include_failed_live_jobs=False)
            == 0
        )
        assert not any(
            statement.startswith(("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE"))
            for statement in statements
        )
    finally:
        sqlalchemy_event.remove(database.engine.sync_engine, "before_cursor_execute", capture)


@pytest.mark.asyncio
async def test_page_and_affected_item_boundaries_keep_other_run_untouched(
    database: Database,
) -> None:
    seed = await _seed(database, 129)
    other = await _seed(database, 1)
    repository = MemoryRebuildRepository(database)
    assert (
        await repository.complete_item_receipts(
            seed.public_id,
            include_failed_live_jobs=False,
            item_ids=(seed.item_ids[-1], other.item_ids[0]),
        )
        == 1
    )
    assert await repository.remaining_item_receipt_count(seed.public_id) == 128
    assert (
        await repository.complete_item_receipts(
            seed.public_id, include_failed_live_jobs=False, limit=128
        )
        == 128
    )
    assert await repository.remaining_item_receipt_count(seed.public_id) == 0
    assert await repository.remaining_item_receipt_count(other.public_id) == 1
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(MemoryRebuildItemModel.status).where(
                    MemoryRebuildItemModel.id == other.item_ids[0]
                )
            )
            == "no_claims"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "include_failed", "expected_item", "expected_job"),
    [
        ("pending", False, "skipped", "pending"),
        ("processing", True, "skipped", "processing"),
        ("done", True, "skipped", "done"),
        ("failed", False, "skipped", "failed"),
        ("failed", True, "committed", "done"),
        ("own_done", False, "committed", "done"),
    ],
)
async def test_live_receipt_arriving_after_prepare_is_never_blindly_overwritten(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    include_failed: bool,
    expected_item: str,
    expected_job: str,
) -> None:
    seed = await _seed(database, 1, include_failed=include_failed)
    original = receipt_finalization._read_batch
    injected = False

    async def read_and_enqueue(session, run_id, item_ids):
        nonlocal injected
        result = await original(session, run_id, item_ids)
        if not injected:
            injected = True
            assert await MemoryJobRepository(database).enqueue(seed.event_ids[0], "private:1001")
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(MemoryJobModel)
                    .where(MemoryJobModel.event_id == seed.event_ids[0])
                    .values(
                        status="done" if status == "own_done" else status,
                        attempts=7,
                        processing_source="rebuild" if status == "own_done" else "live",
                        rebuild_run_id=seed.run_id if status == "own_done" else None,
                    )
                )
        return result

    monkeypatch.setattr(receipt_finalization, "_read_batch", read_and_enqueue)
    completed = await MemoryRebuildRepository(database).complete_item_receipts(
        seed.public_id, include_failed_live_jobs=include_failed
    )
    assert completed == int(expected_item == "committed")
    async with database.sessions() as session:
        item = await session.get(MemoryRebuildItemModel, seed.item_ids[0])
        job = await session.scalar(
            select(MemoryJobModel).where(MemoryJobModel.event_id == seed.event_ids[0])
        )
        assert item is not None and item.status == expected_item
        assert job is not None and job.status == expected_job and job.attempts == 7
        assert job.processing_source == ("rebuild" if expected_item == "committed" else "live")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["review", "commit", "generation", "revision", "selection", "source"]
)
async def test_source_or_review_race_requires_fresh_preparation(
    database: Database, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    seed = await _seed(database, 1, proposals=True)
    original = receipt_finalization._read_batch
    injected = False

    async def read_and_change(session, run_id, item_ids):
        nonlocal injected
        result = await original(session, run_id, item_ids)
        if not injected:
            injected = True
            async with database.immediate_session() as writer:
                if mutation in {"review", "commit"}:
                    values = (
                        {"review_status": "pending"}
                        if mutation == "review"
                        else {"commit_status": "pending"}
                    )
                    statement = (
                        update(MemoryRebuildProposalModel)
                        .where(MemoryRebuildProposalModel.item_id == seed.item_ids[0])
                        .values(**values)
                    )
                elif mutation == "selection":
                    statement = (
                        update(MemoryRebuildRunModel)
                        .where(MemoryRebuildRunModel.id == seed.run_id)
                        .values(selection_hash="e" * 64)
                    )
                elif mutation == "source":
                    statement = (
                        update(ChatEventModel)
                        .where(ChatEventModel.id == seed.event_ids[0])
                        .values(content="来源已经变更")
                    )
                else:
                    column = (
                        CanonicalConversationModel.generation
                        if mutation == "generation"
                        else CanonicalConversationModel.prompt_source_revision
                    )
                    statement = (
                        update(CanonicalConversationModel)
                        .where(CanonicalConversationModel.id == seed.conversation_id)
                        .values({column: column + 1})
                    )
                await writer.execute(statement)
        return result

    monkeypatch.setattr(receipt_finalization, "_read_batch", read_and_change)
    assert (
        await MemoryRebuildRepository(database).complete_item_receipts(
            seed.public_id, include_failed_live_jobs=False
        )
        == 0
    )
    async with database.sessions() as session:
        item = await session.get(MemoryRebuildItemModel, seed.item_ids[0])
        assert item is not None and item.status == "staged"
        assert await session.scalar(select(MemoryJobModel.id)) is None


@pytest.mark.asyncio
async def test_successful_proposals_over_twenty_finish_original_items_across_ticks(
    database: Database,
) -> None:
    seed = await _seed(database, 45, proposals=True)
    claim = MemoryClaim(
        subject_ref="speaker",
        scope_type="person",
        memory_key="profile:location",
        category="profile",
        content="我住在杭州",
        evidence_quote="我住在杭州",
        confidence=0.9,
    )
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(MemoryRebuildProposalModel)
            .where(MemoryRebuildProposalModel.run_id == seed.run_id)
            .values(commit_status="pending", claim_json=claim.model_dump_json())
        )
    _settings, _ledger, facts, _provider, service = await _service(database)
    processed: list[int] = []
    for _ in range(3):
        run = await service.repository.get_run(seed.public_id)
        assert run is not None and run.status is MemoryRebuildRunStatus.COMMITTING
        processed.append(await service.process_commit_once(run))
        current = await service.repository.get_run(seed.public_id)
        assert current is not None
        assert current.status is (
            MemoryRebuildRunStatus.COMPLETED
            if len(processed) == 3
            else MemoryRebuildRunStatus.COMMITTING
        )
    assert processed == [20, 20, 5]
    assert len(await facts.list_person("1001")) == 1
    async with database.sessions() as session:
        items = tuple(
            await session.execute(
                select(MemoryRebuildItemModel.id, MemoryRebuildItemModel.status).where(
                    MemoryRebuildItemModel.run_id == seed.run_id
                )
            )
        )
        jobs = tuple(
            await session.scalars(
                select(MemoryJobModel).where(MemoryJobModel.rebuild_run_id == seed.run_id)
            )
        )
    assert {row.id for row in items} == set(seed.item_ids)
    assert all(row.status == "committed" for row in items)
    assert len(jobs) == 45
    assert all(job.status == "done" and job.outcome == "claims_applied" for job in jobs)


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_sweep_without_commit_rows_keeps_run_active_until_last_page(
    database: Database, rejected: bool
) -> None:
    seed = await _seed(database, 45, proposals=rejected)
    if rejected:
        async with database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryRebuildProposalModel)
                .where(MemoryRebuildProposalModel.run_id == seed.run_id)
                .values(review_status="rejected", commit_status="pending")
            )
    _settings, _ledger, _facts, _provider, service = await _service(database)
    for tick in range(3):
        run = await service.repository.get_run(seed.public_id)
        assert run is not None and run.status is MemoryRebuildRunStatus.COMMITTING
        # This public return counts proposals, while receipt-only work still
        # progresses and must not end the run before all pages are settled.
        assert await service.process_commit_once(run) == 0
        assert await service.repository.remaining_item_receipt_count(seed.public_id) == max(
            0, 45 - (tick + 1) * 20
        )
        current = await service.repository.get_run(seed.public_id)
        assert current is not None
        assert current.status is (
            MemoryRebuildRunStatus.COMPLETED if tick == 2 else MemoryRebuildRunStatus.COMMITTING
        )
    async with database.sessions() as session:
        outcomes = tuple(
            await session.scalars(
                select(MemoryJobModel.outcome).where(MemoryJobModel.rebuild_run_id == seed.run_id)
            )
        )
    assert len(outcomes) == 45
    assert set(outcomes) == {"all_rejected" if rejected else "no_claims"}


@pytest.mark.asyncio
async def test_caller_cannot_expand_persisted_failed_job_selection(database: Database) -> None:
    seed = await _seed(database, 1, include_failed=False)
    assert await MemoryJobRepository(database).enqueue(seed.event_ids[0], "private:1001")
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(MemoryJobModel)
            .where(MemoryJobModel.event_id == seed.event_ids[0])
            .values(status="failed")
        )
    assert (
        await MemoryRebuildRepository(database).complete_item_receipts(
            seed.public_id, include_failed_live_jobs=True
        )
        == 0
    )
    async with database.sessions() as session:
        job = await session.scalar(
            select(MemoryJobModel).where(MemoryJobModel.event_id == seed.event_ids[0])
        )
        assert job is not None and job.status == "failed" and job.processing_source == "live"
        item = await session.get(MemoryRebuildItemModel, seed.item_ids[0])
        assert item is not None and item.error_category == "failed_live_job_not_selected"
