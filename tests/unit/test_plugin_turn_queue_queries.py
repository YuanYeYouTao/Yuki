"""Runnable discovery is independent of paused history; settlement keeps original Work."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, update
from tests.support.background_authority import approve_background_plugin
from tests.support.social_identity_cases import social_env

from yuki.persistence.models import ChatEventModel
from yuki.plugin_host.background_turns import PluginBackgroundTurnWorker
from yuki.plugin_host.db_models import PluginBackgroundTurnJobModel as Job
from yuki.runtime.work_recovery_schema import recovery
from yuki.runtime.work_schema_v1 import scope, work
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest


@pytest.fixture
async def turn_queue(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = await approve_background_plugin(
        database,
        plugin_id="test.indexed-turn-queue",
        bot_user_id="80001",
        group_id="20001",
        creator_user_id="10001",
    )
    receipt = await repository.publish(
        plugin_id="test.indexed-turn-queue",
        request=PublishNotificationRequest(
            event_key="original-event",
            event_type="fixture",
            external_source="fixture",
            occurred_at=datetime.now(UTC),
            target=NotificationTarget(
                target_type="group",
                target_id=env.space,
                conversation_id=env.context.conversation_id,
            ),
            summary="original external source",
            ask_agent=True,
            agent_intent="original goal",
        ),
    )
    async with database.read_session() as reader:
        job = dict((await reader.execute(select(Job.__table__))).mappings().one())
        event = dict(
            (
                await reader.execute(
                    select(ChatEventModel.__table__).where(
                        ChatEventModel.id == receipt.source_event_id
                    )
                )
            )
            .mappings()
            .one()
        )
    return SimpleNamespace(db=database, repo=repository, job=job, event=event, env=env)


def _work_row(queue, identity, state):
    now = datetime.now(UTC).timestamp()
    return {
        "id": identity,
        "conversation_id": queue.env.context.conversation_id,
        "generation": 1,
        "source_key": f"original:{identity}",
        "source_json": '{"origin":"plugin_background"}',
        "goal": "original goal",
        "state": state,
        "created": now,
        "updated": now,
        "model_requests": 7,
        "tool_calls": 3,
    }


async def _bind(queue, state):
    identity = str(uuid4())
    async with queue.db.immediate_session() as writer:
        await writer.execute(insert(work).values(**_work_row(queue, identity, state)))
        await writer.execute(update(Job).where(Job.id == queue.job["id"]).values(work_id=identity))
    return identity


@pytest.mark.parametrize("state", ["waiting_external", "waiting_user", "suspended"])
async def test_paused_original_job_becomes_runnable_after_its_work_changes(turn_queue, state):
    queue = turn_queue
    identity = await _bind(queue, state)
    assert await queue.repo.claim_turn() is None
    async with queue.db.immediate_session() as writer:
        stored = await writer.get(Job, queue.job["id"])
        assert stored.attempts == 0 and stored.status == "pending"
        await writer.execute(update(work).where(work.c.id == identity).values(state="queued"))
    claimed = await queue.repo.claim_turn()
    assert claimed.id == queue.job["id"] and claimed.work_id == identity
    assert claimed.attempts == 1


@pytest.mark.parametrize("state", ["queued", "running"])
async def test_original_recovery_and_scope_lease_still_delay_claim(turn_queue, state):
    queue = turn_queue
    identity = await _bind(queue, state)
    future = (datetime.now(UTC) + timedelta(hours=1)).timestamp()
    async with queue.db.immediate_session() as writer:
        if state == "queued":
            await writer.execute(
                insert(recovery).values(
                    work_id=identity, activation_id=str(uuid4()), not_before=future, updated=future
                )
            )
        else:
            await writer.execute(
                insert(scope).values(
                    conversation_id=queue.env.context.conversation_id,
                    generation=1,
                    lease_until=future,
                )
            )
    assert await queue.repo.claim_turn() is None
    async with queue.db.immediate_session() as writer:
        if state == "queued":
            await writer.execute(
                update(recovery).where(recovery.c.work_id == identity).values(not_before=0)
            )
        else:
            await writer.execute(update(scope).values(lease_until=0))
    claimed = await queue.repo.claim_turn()
    assert claimed.work_id == identity and claimed.attempts == 1


async def test_unbound_and_expired_processing_use_original_attempt_semantics(turn_queue):
    queue = turn_queue
    unbound = await queue.repo.claim_turn()
    assert unbound.id == queue.job["id"] and unbound.work_id is None
    identity = await _bind(queue, "suspended")
    async with queue.db.immediate_session() as writer:
        await writer.execute(
            update(Job)
            .where(Job.id == unbound.id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await queue.repo.claim_turn() is None
    async with queue.db.read_session() as reader:
        row = await reader.get(Job, unbound.id)
        assert row.work_id == identity and row.status == "pending"
        assert row.attempts == unbound.attempts and row.lease_until is None
    assert await queue.repo.claim_turn() is None


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
async def test_terminal_reconciliation_keeps_original_result_usage_and_ids(turn_queue, state):
    queue = turn_queue
    identity = await _bind(queue, state)
    assert await queue.repo.claim_turn() is None
    await queue.repo.reconcile_terminal_turns()
    async with queue.db.read_session() as reader:
        row = await reader.get(Job, queue.job["id"])
        assert row.status == state and row.work_id == identity
        assert row.source_event_id == queue.event["id"]
        assert row.model_requests == 7 and row.tool_calls_used == 3 and row.attempts == 0


async def test_reconciliation_preserves_missing_work_result_for_legacy_broken_links(turn_queue):
    queue = turn_queue
    identity = await _bind(queue, "queued")
    # Only historical/external writes with FK disabled can leave this shape.
    # Current Work deletion retires/detaches its Job in the same transaction.
    async with queue.db.engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            await connection.exec_driver_sql("DELETE FROM runtime_work WHERE id = ?", (identity,))
            await connection.commit()
        finally:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    await queue.repo.reconcile_terminal_turns()
    async with queue.db.read_session() as reader:
        row = await reader.get(Job, queue.job["id"])
        assert row.work_id == identity and row.status == "failed"
        assert row.last_error_category == "runtime_work_missing" and row.attempts == 0


async def test_terminal_reconciliation_crosses_pages_without_losing_original_usage(turn_queue):
    queue = turn_queue
    for index in range(130):
        await queue.repo.publish(
            plugin_id="test.indexed-turn-queue",
            request=PublishNotificationRequest(
                event_key=f"terminal-page-{index}",
                event_type="fixture",
                external_source="fixture",
                occurred_at=datetime.now(UTC),
                target=NotificationTarget(
                    target_type="group",
                    target_id=queue.env.space,
                    conversation_id=queue.env.context.conversation_id,
                ),
                summary="original external source",
                ask_agent=True,
                agent_intent="original goal",
            ),
        )
    async with queue.db.read_session() as reader:
        identities = tuple(await reader.scalars(select(Job.id).order_by(Job.id)))
    assert len(identities) == 131
    original = {}
    async with queue.db.immediate_session() as writer:
        for index, identity in enumerate(identities):
            state = ("completed", "cancelled", "failed")[index % 3]
            work_id = str(uuid4())
            await writer.execute(insert(work).values(**_work_row(queue, work_id, state)))
            await writer.execute(
                update(Job)
                .where(Job.id == identity)
                .values(work_id=work_id, model_requests=11, tool_calls_used=1)
            )
            original[identity] = (work_id, state)
    await queue.repo.reconcile_terminal_turns()
    await queue.repo.reconcile_terminal_turns()
    async with queue.db.read_session() as reader:
        for row in await reader.scalars(select(Job)):
            assert (row.work_id, row.status) == original[row.id]
            assert row.model_requests == 11 and row.tool_calls_used == 3
            assert row.attempts == 0 and row.lease_until is None


async def test_worker_reconciles_at_each_start_without_waiting_for_the_poll(
    turn_queue, monkeypatch
):
    queue = turn_queue
    identity = await _bind(queue, "completed")
    worker = PluginBackgroundTurnWorker(
        repository=queue.repo,
        ledger=None,
        runtime_config=None,
        chat=None,
        turns=None,
        conversation_scopes=None,
    )
    claim = queue.repo.claim_turn

    async def stop_after_claim():
        result = await claim()
        worker._stop.set()
        worker.wake()
        return result

    monkeypatch.setattr(queue.repo, "claim_turn", stop_after_claim)
    for state in ("completed", "failed"):
        async with queue.db.immediate_session() as writer:
            await writer.execute(update(work).where(work.c.id == identity).values(state=state))
            await writer.execute(
                update(Job).where(Job.id == queue.job["id"]).values(status="pending")
            )
        await worker.start()
        async with asyncio.timeout(5):
            await worker._task
        await worker.close()
        async with queue.db.read_session() as reader:
            assert (await reader.get(Job, queue.job["id"])).status == state
