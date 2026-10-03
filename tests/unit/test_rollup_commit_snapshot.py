"""Real WAL races after source preparation, before Rollup publication."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError
from tests.unit.rollup_test_helpers import candidate_summary
from tests.unit.test_conversation_rollup_370 import _append, _policy

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupJobModel,
)
from qq_ai_bot.conversation.rollup.errors import RollupLeaseLostError, RollupSourceChangedError
from qq_ai_bot.conversation.rollup.models import RollupKind
from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_helpers import keeper_event_clause
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


async def _seed(database):
    policy = _policy()
    scope = ConversationScope.private("bot-a", "snapshot-peer")
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    await _append(uow, scope, 4)
    repository = ConversationRollupRepository(database, policy)
    claim = await repository.claim_next_job(lease_owner="paid-owner", lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None
    return repository, scope, claim, candidate


async def _publish(repository, claim, candidate, summary, overlay):
    if overlay:
        return await repository.commit_emergency_overlay(
            claim, candidate, summary, source_emergency=False
        )
    return await repository.commit_candidate(
        claim, candidate, summary_text=summary, summary_kind=RollupKind.MODEL
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay", [False, True], ids=["semantic", "overlay"])
@pytest.mark.parametrize("mutation", ["append", "edit", "reset", "lease", "hold", "generation"])
async def test_source_to_first_write_race_reprepares_only_database(
    database, monkeypatch, overlay, mutation
):
    repository, scope, claim, candidate = await _seed(database)
    paid_model = AsyncMock(return_value=candidate_summary(candidate, "paid once"))
    summary = await paid_model()
    ready, proceed = asyncio.Event(), asyncio.Event()
    original = repository._confirm_commit_lease
    attempts = 0

    async def before_write(session, bound_claim, *, now):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            ready.set()
            await proceed.wait()
        await original(session, bound_claim, now=now)

    monkeypatch.setattr(repository, "_confirm_commit_lease", before_write)
    if mutation == "hold":

        class Holds:
            async def earliest_source_event_id(self, session, *, canonical_conversation_id):
                # An indexed persisted dependency, using the production hold protocol.
                failures = await session.scalar(
                    select(CanonicalConversationRollupJobModel.failure_count).where(
                        CanonicalConversationRollupJobModel.conversation_id
                        == canonical_conversation_id
                    )
                )
                return candidate.events[0].id if failures == 99 else None

        repository._coverage_holds = Holds()
    task = asyncio.create_task(_publish(repository, claim, candidate, summary, overlay))
    await asyncio.wait_for(ready.wait(), 5)
    second = Database(database.url)
    try:
        uow = ScopedEventLedgerUnitOfWork(second, config=repository.config)
        if mutation == "append":
            await _append(uow, scope, 1, start=5)
        elif mutation == "edit":
            assert await uow.set_visual_summary(candidate.events[0].id, "new visual source")
        elif mutation == "reset":
            await uow.append_new_generation_command(
                scope=scope,
                inbound=InboundMessage(
                    message_id="reset-snapshot",
                    event_type="message:test",
                    scope_type=ScopeType.PRIVATE,
                    sender=SenderIdentity(user_id="snapshot-peer"),
                    text="reset",
                    bot_user_id="bot-a",
                ),
            )
        else:
            values = (
                {"lease_owner": "new-owner", "lease_token": "new-token"}
                if mutation == "lease"
                else {"generation": claim.generation + 1}
                if mutation == "generation"
                else {"failure_count": 99}
            )
            async with second.immediate_session() as writer:
                await writer.execute(
                    update(CanonicalConversationRollupJobModel)
                    .where(
                        CanonicalConversationRollupJobModel.conversation_id == claim.conversation_id
                    )
                    .values(**values)
                )
        proceed.set()
        if mutation == "append":
            result = await asyncio.wait_for(task, 5)
            assert result.rollup.source_fingerprint == candidate.fingerprint
            assert result.rollup.summary_text == summary
            assert attempts == 2  # Real SQLITE_BUSY_SNAPSHOT; fresh DB plan only.
        else:
            with pytest.raises((RollupSourceChangedError, RollupLeaseLostError)):
                await asyncio.wait_for(task, 5)
            assert attempts == 1  # Second preparation rejects before first DML.
        snapshot = await repository.load_prompt_snapshot(scope)
        if mutation != "append":
            assert snapshot.rollup is None and snapshot.overlay is None
        async with database.sessions() as reader:
            conversation = await reader.get(CanonicalConversationModel, claim.conversation_id)
            assert conversation is not None
            remaining = (
                await reader.scalars(
                    select(ChatEventModel.id).where(
                        ChatEventModel.canonical_conversation_id == claim.conversation_id,
                        ChatEventModel.id > conversation.covered_through_event_id,
                        keeper_event_clause(),
                    )
                )
            ).all()
            assert conversation.uncovered_event_count == len(remaining)
            if mutation == "append":
                assert conversation.last_event_id == 5
                assert conversation.uncovered_event_count == (
                    5 if overlay else 5 - len(candidate.events)
                )
            if mutation == "lease":
                job = await reader.get(CanonicalConversationRollupJobModel, claim.conversation_id)
                assert job is not None and job.lease_token == "new-token"
        paid_model.assert_awaited_once()
    finally:
        proceed.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay", [False, True])
async def test_sql_execution_clock_rejects_expired_lease_despite_stale_python_time(
    database, monkeypatch, overlay
):
    repository, _scope, claim, candidate = await _seed(database)
    # A deterministic barrier advances real lease time by changing the original
    # lease before commit; a stale Python now must not authenticate it.
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalConversationRollupJobModel)
            .where(CanonicalConversationRollupJobModel.conversation_id == claim.conversation_id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    monkeypatch.setattr(
        "qq_ai_bot.conversation.rollup.repository._utcnow",
        lambda: datetime.now(UTC) - timedelta(minutes=1),
    )
    with pytest.raises(RollupLeaseLostError, match="publication"):
        await _publish(
            repository, claim, candidate, candidate_summary(candidate, "old clock"), overlay
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [(517, 3), (5, 1), (6, 1)])
async def test_snapshot_retry_is_finite_and_specific(database, monkeypatch, code, expected):
    repository, _scope, claim, candidate = await _seed(database)
    error = sqlite3.OperationalError("synthetic database contention")
    error.sqlite_errorcode = code
    operation = AsyncMock(side_effect=OperationalError("SQL", {}, error))
    monkeypatch.setattr(repository, "_commit_canonical_candidate", operation)
    with pytest.raises(OperationalError):
        await _publish(repository, claim, candidate, candidate_summary(candidate, "paid"), False)
    assert operation.await_count == expected
    snapshot = await repository.load_prompt_snapshot(
        ConversationScope.private("bot-a", "snapshot-peer")
    )
    assert snapshot.rollup is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["background", "foreground", "heartbeat", "retry"])
async def test_lease_duration_begins_after_writer_acquisition(database, monkeypatch, operation):
    repository, scope, claim, _candidate = await _seed(database)
    before = datetime.now(UTC) - timedelta(minutes=5)
    after = datetime.now(UTC)
    acquired = False
    original = database.immediate_session

    @asynccontextmanager
    async def delayed_writer():
        nonlocal acquired
        async with original() as session:
            acquired = True
            yield session

    monkeypatch.setattr(database, "immediate_session", delayed_writer)
    monkeypatch.setattr(
        "qq_ai_bot.conversation.rollup.repository._utcnow",
        lambda: after if acquired else before,
    )
    if operation == "background":
        await repository.release_owner(claim.lease_owner)
        result = await repository.claim_next_job(lease_owner="new", lease_seconds=30)
    elif operation == "foreground":
        result = await repository.claim_scope_for_foreground(
            scope, lease_owner="new", lease_seconds=30
        )
    elif operation == "heartbeat":
        result = await repository.heartbeat(claim, lease_seconds=30)
    else:
        await repository.retry_infrastructure(
            claim, error_category="db failure", retry_max_seconds=960
        )
        async with database.sessions() as reader:
            job = await reader.get(CanonicalConversationRollupJobModel, claim.conversation_id)
            assert job is not None and job.status == "pending"
            assert job.next_attempt_at.replace(tzinfo=UTC) == after + timedelta(seconds=15)
            assert job.failure_count == 1
        return
    assert result is not None and result.lease_until == after + timedelta(seconds=30)


@pytest.mark.asyncio
async def test_empty_job_queue_does_not_acquire_writer(database):
    repository = ConversationRollupRepository(database, _policy())
    writes = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert await repository.claim_next_job(lease_owner="idle", lease_seconds=30) is None
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay", [False, True])
async def test_lost_commit_confirmation_does_not_authorize_republishing(
    database, monkeypatch, overlay
):
    repository, scope, claim, candidate = await _seed(database)
    summary = candidate_summary(candidate, "accepted paid result")
    original_factory = database.sessions

    @asynccontextmanager
    async def confirmation_lost():
        async with original_factory() as session:
            yield session
        # The transaction has really committed; only the caller confirmation fails.
        raise OSError("synthetic commit confirmation lost")

    monkeypatch.setattr(database, "sessions", confirmation_lost)
    with pytest.raises(OSError, match="confirmation lost"):
        await _publish(repository, claim, candidate, summary, overlay)
    monkeypatch.setattr(database, "sessions", original_factory)
    accepted = await repository.load_prompt_snapshot(scope)
    assert accepted.rollup.source_fingerprint == candidate.fingerprint
    assert accepted.rollup.summary_text == summary
    with pytest.raises((RollupLeaseLostError, RollupSourceChangedError)):
        await _publish(repository, claim, candidate, summary, overlay)
    assert await repository.load_prompt_snapshot(scope) == accepted
