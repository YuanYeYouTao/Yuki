"""Select ready scope heads without a database round trip per blocked scope."""

import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert
from tests.support.social_identity_cases import social_env
from tests.support.sqlite_cost import capture_reads, read_vm_steps

from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.identity.canonical_repository import find_space_binding
from yuki.runtime.work_recovery_schema import recovery
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_scheduler import WorkScheduler
from yuki.runtime.work_schema_v1 import scope, work


def queued(identity, conversation):
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
    )


def scheduler_for(database, monkeypatch):
    repository = WorkRepository(database)
    monkeypatch.setattr(repository, "repair_abandoned_inputs", AsyncMock())
    monkeypatch.setattr(
        "yuki.automation.repository.AutomationRepository.settle_work_occurrences", AsyncMock()
    )
    scheduler = WorkScheduler(repository, AsyncMock(), chat_admission_enabled=True)
    scheduler._last_reclaim = time.monotonic()
    return scheduler


@pytest.mark.parametrize("blocked", ["lease", "recovery"])
async def test_blocked_scopes_do_not_require_one_query_each(
    database, tmp_path, monkeypatch, blocked
):
    env = await social_env(database, tmp_path)
    scheduler = scheduler_for(database, monkeypatch)
    previous, counts = 0, []
    for size in (16, 160):
        async with database.immediate_session() as writer:
            binding = await find_space_binding(writer, "20001")
            conversations = [
                await ensure_canonical_conversation(
                    writer,
                    kind="space",
                    space_id=env.space,
                    space_binding_id=binding.id,
                    primary_scope_key=f"blocked:{i}",
                    thread_id=f"blocked-{i}",
                )
                for i in range(previous, size)
            ]
            await writer.execute(
                insert(work),
                [
                    queued(f"blocked-{i}", entry.conversation_id)
                    for i, entry in zip(range(previous, size), conversations, strict=True)
                ],
            )
            if blocked == "lease":
                await writer.execute(
                    insert(scope),
                    [
                        dict(
                            conversation_id=entry.conversation_id,
                            generation=1,
                            owner="other-process",
                            lease_until=time.time() + 3600,
                        )
                        for entry in conversations
                    ],
                )
            else:
                await writer.execute(
                    insert(recovery),
                    [
                        dict(
                            work_id=f"blocked-{i}",
                            activation_id=f"accepted-{i}",
                            not_before=time.time() + 3600,
                            updated=1.0,
                        )
                        for i in range(previous, size)
                    ],
                )
        with capture_reads(database) as statements:
            assert await scheduler.dispatch_once() == []
        counts.append(len(statements))
        previous = size
    assert scheduler._resume.await_count == 0
    assert counts[1] <= counts[0] + 1, counts


@pytest.mark.parametrize("deferred", [False, True])
async def test_busy_scope_selection_skips_its_backlog_and_terminal_history(
    database, tmp_path, monkeypatch, deferred
):
    env = await social_env(database, tmp_path)
    scheduler = scheduler_for(database, monkeypatch)
    conversation = env.context.conversation_id
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(scope).values(
                conversation_id=conversation,
                generation=1,
                owner="other-process",
                lease_until=time.time() + 3600,
            )
        )
    previous, costs = 0, []
    for size in (16, 640):
        async with database.immediate_session() as writer:
            await writer.execute(
                insert(work),
                [queued(f"busy-{i}", conversation) for i in range(previous, size)]
                + [
                    {**queued(f"old-{i}", conversation), "state": "completed"}
                    for i in range(previous, size)
                ],
            )
            if deferred:
                await writer.execute(
                    insert(recovery),
                    [
                        dict(
                            work_id=f"busy-{i}",
                            activation_id=f"accepted-busy-{i}",
                            not_before=time.time() + 3600,
                            updated=1.0,
                        )
                        for i in range(previous, size)
                    ],
                )
        with capture_reads(database) as statements:
            assert await scheduler.dispatch_once() == []
        costs.append(read_vm_steps(database, statements))
        previous = size
    assert scheduler._resume.await_count == 0
    assert costs[1] <= costs[0] * 4, costs
