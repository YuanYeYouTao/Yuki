"""Scheduler fills available capacity past busy and deferred scopes."""

import asyncio
import time
from unittest.mock import AsyncMock

from sqlalchemy import insert, select, update
from tests.support.social_identity_cases import social_env

from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.identity.canonical_repository import find_space_binding
from yuki.runtime.work_recovery_schema import recovery
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_scheduler import WorkScheduler
from yuki.runtime.work_schema_v1 import scope, work


def task(identity, *, conversation):
    return dict(
        id=identity,
        conversation_id=conversation,
        generation=1,
        source_key=f"source:{identity}",
        source_json='{"origin":"user_message"}',
        goal="original task",
        state="queued",
        created=1.0,
        updated=1.0,
        checkpoint_json="{}",
    )


async def test_scheduler_cursor_wraps_past_leased_and_deferred_scopes(
    database, tmp_path, monkeypatch
):
    environment = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    monkeypatch.setattr(repository, "repair_abandoned_inputs", AsyncMock())
    monkeypatch.setattr(
        "yuki.automation.repository.AutomationRepository.settle_work_occurrences", AsyncMock()
    )
    resumed = []

    async def resume(item):
        resumed.append(item["id"])
        async with database.immediate_session() as writer:
            await writer.execute(
                update(work).where(work.c.id == item["id"]).values(state="completed")
            )

    scheduler = WorkScheduler(repository, resume, chat_admission_enabled=True)
    scheduler._last_reclaim = time.monotonic()
    assert await scheduler.dispatch_once() == []
    async with database.immediate_session() as writer:
        binding = await find_space_binding(writer, "20001")
        conversations = sorted(
            [
                (
                    await ensure_canonical_conversation(
                        writer,
                        kind="space",
                        space_id=environment.space,
                        space_binding_id=binding.id,
                        primary_scope_key=f"cursor-thread:{i}",
                        thread_id=f"cursor-{i}",
                    )
                ).conversation_id
                for i in range(10)
            ]
        )
        await writer.execute(
            insert(work),
            [
                task(f"cursor-{i}", conversation=identity)
                for i, identity in enumerate(conversations)
            ],
        )
        await writer.execute(
            insert(scope).values(
                conversation_id=conversations[0],
                generation=1,
                owner="real-owner",
                lease_until=time.time() + 3600,
            )
        )
        await writer.execute(
            insert(recovery).values(
                work_id="cursor-1",
                activation_id="original-activation",
                not_before=time.time() + 3600,
                updated=1,
            )
        )
    for _ in range(2):
        await asyncio.gather(*(await scheduler.dispatch_once()))
    assert sorted(resumed) == [f"cursor-{i}" for i in range(2, 10)]
    assert await scheduler.dispatch_once() == []
    async with database.immediate_session() as writer:
        await writer.execute(
            update(scope)
            .where(scope.c.conversation_id == conversations[0])
            .values(owner=None, lease_until=0)
        )
        await writer.execute(
            update(recovery).where(recovery.c.work_id == "cursor-1").values(not_before=0)
        )
    await asyncio.gather(*(await scheduler.dispatch_once()))
    assert sorted(resumed) == [f"cursor-{i}" for i in range(10)]
    assert await scheduler.dispatch_once() == []


async def test_scheduler_fills_last_slot_past_busy_and_deferred_scopes(
    database, tmp_path, monkeypatch
):
    environment = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    monkeypatch.setattr(repository, "repair_abandoned_inputs", AsyncMock())
    monkeypatch.setattr(
        "yuki.automation.repository.AutomationRepository.settle_work_occurrences", AsyncMock()
    )
    release = asyncio.Event()
    entered = {f"busy-{i}": asyncio.Event() for i in range(7)}
    resumed = []

    async def resume(item):
        resumed.append(item["id"])
        if item["id"] in entered:
            entered[item["id"]].set()
            await release.wait()
        else:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(work).where(work.c.id == item["id"]).values(state="completed")
                )

    scheduler = WorkScheduler(repository, resume, chat_admission_enabled=True)
    scheduler._last_reclaim = time.monotonic()
    active = []
    started = []
    try:
        async with database.immediate_session() as writer:
            binding = await find_space_binding(writer, "20001")
            identities = sorted(
                [
                    (
                        await ensure_canonical_conversation(
                            writer,
                            kind="space",
                            space_id=environment.space,
                            space_binding_id=binding.id,
                            primary_scope_key=f"last-slot:{i}",
                            thread_id=f"last-slot-{i}",
                        )
                    ).conversation_id
                    for i in range(9)
                ]
            )
            await writer.execute(
                insert(work),
                [
                    task(f"busy-{i}", conversation=identity)
                    for i, identity in enumerate(identities[:7])
                ],
            )
        active = await scheduler.dispatch_once()
        async with asyncio.timeout(5):
            await asyncio.gather(*(event.wait() for event in entered.values()))
        assert len(active) == 7
        async with database.immediate_session() as writer:
            await writer.execute(
                insert(work),
                [
                    task("deferred-last-slot", conversation=identities[7]),
                    task("ready-last-slot", conversation=identities[8]),
                ],
            )
            await writer.execute(
                insert(recovery).values(
                    work_id="deferred-last-slot",
                    activation_id="original-activation",
                    not_before=time.time() + 3600,
                    updated=1,
                )
            )
        started = await scheduler.dispatch_once()
        assert len(started) == 1
        await asyncio.gather(*started)
        assert resumed == [*(f"busy-{i}" for i in range(7)), "ready-last-slot"]
        async with database.read_session() as reader:
            state = await reader.scalar(
                select(work.c.state).where(work.c.id == "deferred-last-slot")
            )
            assert state == "queued"
    finally:
        release.set()
        await asyncio.gather(*active, *started, return_exceptions=True)
