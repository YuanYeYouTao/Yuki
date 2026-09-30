"""Pre-history rollup preparation parks and wakes the original durable Work."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, event, select, update
from tests.conftest import build_harness, make_settings
from tests.unit.test_work_protocol_continuity import _control

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationRollupJobModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.runtime.activation_outcome import WorkActivationHandled
from qq_ai_bot.runtime.work_activation import activate_work
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import work


async def version(control):
    source, _ = await EventLedgerRepository(control.repository.database).read_scope_context(
        ConversationScope.group("80001", "20001"), limit=10
    )
    return source


async def finish_rollup(database, conversation_id):
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        writer.add(
            CanonicalConversationRollupModel(
                conversation_id=conversation_id,
                generation=1,
                covered_through_event_id=1,
                summary_text="prepared original source",
                summary_kind="model",
                source_fingerprint="0" * 64,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        await writer.execute(
            delete(CanonicalConversationRollupJobModel).where(
                CanonicalConversationRollupJobModel.conversation_id == conversation_id
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_before_settle", [False, True])
async def test_rollup_wait_restarts_and_completion_before_settle_is_not_lost(
    database, tmp_path, finish_before_settle
):
    control = await _control(database, tmp_path)
    identity = control.current["id"]
    await control.repository.checkpoint(control.lease, identity, {"retain": "receipt-cursor"})
    assert await control.repository.defer_context_rollup(
        control.lease, identity, await version(control), 0, 90
    )
    if finish_before_settle:
        await finish_rollup(database, control.lease.conversation_id)
        await control.repository.wake_context_rollups()
        assert (await control.repository.get(identity))["state"] == "running"
    control.ending = "waiting_external"
    await control.settle(delivered=False, pending_inputs=False)
    await control.repository.release(control.lease)
    if not finish_before_settle:
        await finish_rollup(database, control.lease.conversation_id)
    # A new repository represents restart; completion is derived from actual
    # canonical state, rather than a lost in-process callback or fake input.
    repository = WorkRepository(database)
    await repository.wake_context_rollups()
    current = await repository.get(identity)
    assert current["state"] == "queued"
    assert current["source_key"] == control.source_key and current["model_requests"] == 0
    assert json.loads(current["checkpoint_json"])["retain"] == "receipt-cursor"
    await repository.wake_context_rollups()
    assert (await repository.get(identity))["revision"] == current["revision"]
    lease = await repository.acquire(control.lease.conversation_id, 1)
    await repository.finish_context_rollup(lease, identity)
    assert json.loads((await repository.get(identity))["checkpoint_json"]) == {
        "retain": "receipt-cursor"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("wake_reason", ["timeout", "error", "cancel"])
async def test_rollup_wait_wakes_on_original_deadline_or_error_and_never_after_cancel(
    database, tmp_path, wake_reason
):
    control = await _control(database, tmp_path)
    identity = control.current["id"]
    assert await control.repository.defer_context_rollup(
        control.lease, identity, await version(control), 0, 90
    )
    control.ending = "waiting_external"
    await control.settle(delivered=False, pending_inputs=False)
    if wake_reason == "cancel":
        await control.repository.cancel(control.lease.conversation_id)
    else:
        async with database.immediate_session() as writer:
            if wake_reason == "timeout":
                await writer.execute(
                    update(work)
                    .where(work.c.id == identity)
                    .values(
                        checkpoint_json=json.dumps(
                            {"context_rollup": {"coverage": 0, "starts_after": 0, "deadline": 1}}
                        )
                    )
                )
            else:
                await writer.execute(
                    update(CanonicalConversationRollupJobModel).values(
                        last_error_category="timeout"
                    )
                )
    await control.repository.wake_context_rollups()
    assert (await control.repository.get(identity))["state"] == (
        "cancelled" if wake_reason == "cancel" else "queued"
    )
    if wake_reason != "cancel":
        assert not await control.repository.defer_context_rollup(
            control.lease, identity, await version(control), 0, 90
        )


@pytest.mark.asyncio
async def test_no_completion_does_not_write_or_overwrite_processing_claim(database, tmp_path):
    control = await _control(database, tmp_path)
    claim = await build_harness(
        database, make_settings(database.url)
    ).conversation_rollups.claim_scope_for_foreground(
        ConversationScope.group("80001", "20001"), lease_owner="actual-owner", lease_seconds=30
    )
    assert claim
    assert await control.repository.defer_context_rollup(
        control.lease, control.current["id"], await version(control), 0, 90
    )
    control.ending = "waiting_external"
    await control.settle(delivered=False, pending_inputs=False)
    statements = []

    def sql(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(database.engine.sync_engine, "before_cursor_execute", sql)
    try:
        await control.repository.wake_context_rollups()
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", sql)
    assert not any(
        s.startswith(("begin immediate", "update", "insert", "delete")) for s in statements
    )
    async with database.sessions() as reader:
        job = await reader.get(CanonicalConversationRollupJobModel, control.lease.conversation_id)
        assert job.lease_token == claim.lease_token and job.lease_owner == "actual-owner"


@pytest.mark.asyncio
async def test_over_budget_assembler_parks_before_rollup_model_and_releases_activation(
    database, tmp_path
):
    original = await _control(database, tmp_path)
    await original.repository.release(original.lease)
    harness = build_harness(database, make_settings(database.url))
    assembler = harness.processor._chat._context_assembler
    identity = ConversationScope.group("80001", "20001")
    state = await harness.conversation_scopes.get(identity)
    turn = ConversationTurnSnapshot(state.id, identity.key, 1, 1, 1)

    async def validate():
        pass

    with pytest.raises(WorkActivationHandled):
        async with activate_work(
            original.repository,
            original.lease.conversation_id,
            1,
            original.source_key,
            original.source,
            validate,
        ) as control:
            snapshot = await assembler._load_history_snapshot(
                identity, turn=turn, before_event_id=None
            )
            await asyncio.wait_for(
                assembler._ensure_uncovered_fits_budget(
                    snapshot=snapshot,
                    recent=snapshot.recent,
                    current_event_id=None,
                    content="continue",
                    yuki_account_ids=frozenset({"80001"}),
                    current_message_override=ChatMessage("user", "continue"),
                    remainder=1,
                    event_limit=1,
                    identity=identity,
                    turn=turn,
                ),
                timeout=0.5,
            )
    assert not await original.repository.valid(control.lease)
    assert (await original.repository.get(original.current["id"]))["state"] == "waiting_external"
    assert harness.provider.requests == []


@pytest.mark.asyncio
async def test_existing_model_history_is_not_recompressed_for_preparation(database, tmp_path):
    control = await _control(database, tmp_path)
    await control.repository.checkpoint(
        control.lease, control.current["id"], {"retained": "old"}, models=2
    )
    with pytest.raises(WorkConflict, match="work_journal_source_changed"):
        await control.repository.defer_context_rollup(
            control.lease, control.current["id"], await version(control), 0, 90
        )
    current = await control.repository.get(control.current["id"])
    assert current["model_requests"] == 2 and json.loads(current["checkpoint_json"]) == {
        "retained": "old"
    }
    async with database.sessions() as reader:
        assert (
            await reader.scalar(select(CanonicalConversationRollupJobModel.conversation_id)) is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("old_generation", [False, True])
async def test_original_job_is_claimable_and_required_work_keeps_priority(
    database, tmp_path, old_generation
):
    control = await _control(database, tmp_path)
    repository = build_harness(database, make_settings(database.url)).conversation_rollups
    if old_generation:
        now = datetime.now(UTC)
        async with database.immediate_session() as writer:
            writer.add(
                CanonicalConversationRollupJobModel(
                    conversation_id=control.lease.conversation_id,
                    generation=2,
                    signal_revision=3,
                    status="pending",
                    failure_count=1,
                    last_error_category="old-generation-error",
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
    assert await control.repository.defer_context_rollup(
        control.lease, control.current["id"], await version(control), 0, 90
    )
    claim = await repository.claim_next_job(lease_owner="worker", lease_seconds=30)
    assert claim and claim.generation == 1
    assert claim.claimed_signal_revision == (4 if old_generation else 1)
    assert await repository.has_required_work(claim)
    await control.repository.finish_context_rollup(control.lease, control.current["id"])
    assert not await repository.has_required_work(claim)


@pytest.mark.asyncio
async def test_completion_discovery_is_rechecked_after_privacy_cancel(
    database, tmp_path, monkeypatch
):
    control = await _control(database, tmp_path)
    identity = control.current["id"]
    assert await control.repository.defer_context_rollup(
        control.lease, identity, await version(control), 0, 90
    )
    control.ending = "waiting_external"
    await control.settle(delivered=False, pending_inputs=False)
    await finish_rollup(database, control.lease.conversation_id)
    original = database.immediate_session

    @asynccontextmanager
    async def cancel_before_writer():
        await control.repository.cancel(control.lease.conversation_id)
        async with original() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", cancel_before_writer)
    await control.repository.wake_context_rollups()
    assert (await control.repository.get(identity))["state"] == "cancelled"
