"""Durable work race and recovery contracts against a real SQLite database."""

import asyncio
import json

import pytest
from sqlalchemy import select, update
from tests.support.social_identity_cases import social_env

from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, scope


@pytest.mark.asyncio
async def test_work_fencing_input_consumption_and_late_receipts(database, tmp_path):
    env = await social_env(database, tmp_path)
    conversation_id = env.context.conversation_id
    a, b = WorkRepository(database), WorkRepository(database)
    claims = await asyncio.gather(a.acquire(conversation_id, 1), b.acquire(conversation_id, 1))
    assert sum(item is not None for item in claims) == 1
    lease = next(item for item in claims if item is not None)
    item = await a.accept(lease, source_key="drawing", source={"actor": "original"}, goal="draw")
    repeated = await b.accept(
        lease, source_key="drawing", source={"actor": "original"}, goal="draw"
    )
    assert item["id"] == repeated["id"]
    with pytest.raises(WorkConflict):
        await a.accept(lease, source_key="drawing", source={"actor": "other"}, goal="draw")
    ids = await asyncio.gather(
        *[repo.enqueue(conversation_id, 1, "new-input", kind="message") for repo in (a, b)]
    )
    assert ids[0] == ids[1]
    assert await a.valid(lease)  # New input never revokes a running activation.
    await a.stage(lease, [ids[0]], "attempt")
    with pytest.raises(WorkConflict):
        await b.stage(lease, [ids[0]], "second-attempt")
    await a.consume(lease, "attempt")
    await b.consume(lease, "attempt")
    await a.checkpoint(lease, item["id"], {"pending_run_id": "child"}, models=2, tools=1)
    assert await a.prepare_effect(lease, item["id"], "send-once", "message")
    assert not await b.prepare_effect(lease, item["id"], "send-once", "message")
    await a.cancel(conversation_id)
    assert not await a.valid(lease)
    with pytest.raises(WorkConflict):
        await b.checkpoint(lease, item["id"], {}, models=1)
    # An already accepted send survives cancellation and cannot become retryable.
    await a.record_effect("send-once", "accepted", {"message_id": "real-platform-id"})
    await b.record_effect("send-once", "accepted", {"message_id": "real-platform-id"})
    with pytest.raises(WorkConflict):
        await b.record_effect("send-once", "failed", {})
    persisted = await a.get(item["id"])
    assert persisted["state"] == "cancelled"
    assert persisted["model_requests"] == 2
    assert json.loads(persisted["checkpoint_json"])["pending_run_id"] == "child"
    async with database.sessions() as session:
        assert (await session.execute(select(inputs.c.state))).scalar_one() == "consumed"
        assert (await session.execute(select(effects.c.state))).scalar_one() == "accepted"


@pytest.mark.asyncio
async def test_expired_owner_cannot_release_or_mutate_replacement(database, tmp_path):
    env = await social_env(database, tmp_path)
    key = env.context.conversation_id
    repository = WorkRepository(database)
    old = await repository.acquire(key, 1)
    assert old
    item = await repository.accept(old, source_key="goal", source={}, goal="render")
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(scope).where(scope.c.conversation_id == key).values(lease_until=0)
        )
    new = await WorkRepository(database).acquire(key, 1)
    assert new and new.fence > old.fence
    await repository.release(old)
    assert await repository.valid(new)
    with pytest.raises(WorkConflict):
        await repository.transition(old, item["id"], 1, "completed")
    waiting = await repository.transition(new, item["id"], 1, "waiting_external")
    with pytest.raises(WorkConflict):
        await repository.transition(new, item["id"], 1, "running")
    done = await repository.transition(new, item["id"], waiting["revision"], "completed")
    with pytest.raises(WorkConflict):
        await repository.transition(new, item["id"], done["revision"], "running")
    await repository.cancel(key, generation=2)
    assert await repository.acquire(key, 1) is None
    assert await repository.acquire(key, 2)
