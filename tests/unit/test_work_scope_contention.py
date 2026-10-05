"""Scope no-op paths stay read-only; actual lease mutations keep their CAS."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy import select, update
from tests.support.projection_sql_counts import capture_sql
from tests.support.social_identity_cases import social_env

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import scope


def assert_read_only(statements):
    assert statements
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in statements)


async def test_busy_and_obsolete_acquire_do_not_queue_behind_an_unrelated_writer(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease
    async with database.immediate_session():
        with capture_sql(database) as statements:
            assert await asyncio.wait_for(repository.acquire(lease.conversation_id, 1), 0.5) is None
            assert await asyncio.wait_for(repository.acquire(lease.conversation_id, 2), 0.5) is None
            assert (
                await asyncio.wait_for(repository.acquire("missing-conversation", 1), 0.5) is None
            )
        assert_read_only(statements)
    assert await repository.valid(lease)
    await repository.release(lease)


@pytest.mark.parametrize("obsolete", ["expired", "owner", "fence", "generation", "cancel_epoch"])
async def test_obsolete_cleanup_and_renew_are_read_only_under_writer(database, tmp_path, obsolete):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease
    original = await repository.accept(
        lease, source_key="scope-original", source={}, goal="original"
    )
    await repository.checkpoint(lease, original["id"], {"original": True}, models=2, tools=3)
    saved = await repository.get(original["id"])
    if obsolete == "expired":
        async with database.immediate_session() as writer:
            await writer.execute(update(scope).values(lease_until=0))
    else:
        lease = replace(lease, **{obsolete: "another-owner" if obsolete == "owner" else 999})
    async with database.immediate_session():
        with capture_sql(database) as statements:
            await asyncio.wait_for(repository.release(lease), 0.5)
            assert await asyncio.wait_for(repository.renew(lease), 0.5) is False
        assert_read_only(statements)
    assert await repository.get(original["id"]) == saved


async def test_acquire_rechecks_generation_after_read_preparation(database, tmp_path, monkeypatch):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    prepared, proceed = asyncio.Event(), asyncio.Event()
    original_writer = database.immediate_session

    @asynccontextmanager
    async def paused_writer():
        prepared.set()
        await proceed.wait()
        async with original_writer() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", paused_writer)
    candidate = asyncio.create_task(repository.acquire(env.context.conversation_id, 1))
    try:
        await asyncio.wait_for(prepared.wait(), 1)
        async with original_writer() as writer:
            await writer.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == env.context.conversation_id)
                .values(generation=2)
            )
        proceed.set()
        assert await candidate is None
        async with database.sessions() as reader:
            assert await reader.scalar(select(scope.c.conversation_id)) is None
    finally:
        proceed.set()
        await asyncio.gather(candidate, return_exceptions=True)


async def test_concurrent_read_candidates_still_have_one_lease_owner(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    count = 0
    prepared = asyncio.Event()
    original_writer = database.immediate_session

    @asynccontextmanager
    async def together():
        nonlocal count
        count += 1
        if count == 2:
            prepared.set()
        await prepared.wait()
        async with original_writer() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", together)
    contenders = await asyncio.wait_for(
        asyncio.gather(*(repository.acquire(env.context.conversation_id, 1) for _ in range(2))), 2
    )
    winners = [lease for lease in contenders if lease is not None]
    assert len(winners) == 1
    async with database.sessions() as reader:
        stored = (await reader.execute(select(scope))).mappings().one()
    assert stored["fence"] == winners[0].fence == 1
    assert stored["owner"] == winners[0].owner


@pytest.mark.parametrize("operation", ["release", "renew"])
async def test_cleanup_rechecks_fence_when_owner_changes_after_read(
    database, tmp_path, monkeypatch, operation
):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease
    original_valid = repository.valid
    replacement = None

    async def replace_after_read(candidate):
        nonlocal replacement
        result = await original_valid(candidate)
        async with database.immediate_session() as writer:
            await writer.execute(update(scope).values(lease_until=0))
        replacement = await repository.acquire(lease.conversation_id, lease.generation)
        return result

    monkeypatch.setattr(repository, "valid", replace_after_read)
    if operation == "release":
        await repository.release(lease)
    else:
        assert await repository.renew(lease) is False
    assert replacement and await original_valid(replacement)
    assert replacement.fence == lease.fence + 1


async def test_renew_does_not_extend_lease_that_expired_after_read(database, tmp_path, monkeypatch):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease
    original_valid = repository.valid

    async def expire_after_read(candidate):
        result = await original_valid(candidate)
        async with database.immediate_session() as writer:
            await writer.execute(update(scope).values(lease_until=0))
        return result

    monkeypatch.setattr(repository, "valid", expire_after_read)
    assert await repository.renew(lease) is False
    async with database.sessions() as reader:
        assert await reader.scalar(select(scope.c.lease_until)) == 0
