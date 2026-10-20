"""Bound query work by the requested scopes/runs, not unrelated task history.

Synthetic Work rows isolate scheduler/query cost, not platform admission. The
same-engine small/large comparison tolerates planner differences and measures no
wall-clock latency. Forty times the irrelevant rows may use at most four times
the VM work; this is a coarse complexity guard, not a production capacity limit.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from tests.support.social_identity_cases import social_env
from tests.support.sqlite_cost import capture_reads, read_vm_steps

from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.identity.canonical_repository import find_space_binding
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_scheduler import WorkScheduler
from yuki.runtime.work_schema_v1 import work
from yuki.runtime.work_wait import WorkWaitRepository


def task(identity, *, conversation="scope", parent=None, state="completed", at=1.0):
    return dict(
        id=identity,
        parent_work_id=parent,
        conversation_id=conversation,
        generation=1,
        source_key=f"source:{identity}",
        source_json='{"origin":"user_message"}',
        goal="original task",
        state=state,
        created=at,
        updated=at,
        checkpoint_json="{}",
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "P05: scheduler reads all ready scopes; docs/operations/backend-five-pass-audit-20261020.md"
    ),
)
async def test_scheduler_selection_cost_is_bounded_by_available_scopes(
    database, tmp_path, monkeypatch
):
    environment = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    monkeypatch.setattr(repository, "repair_abandoned_inputs", AsyncMock())
    monkeypatch.setattr(
        "yuki.automation.repository.AutomationRepository.settle_work_occurrences", AsyncMock()
    )
    scheduler = WorkScheduler(repository, AsyncMock(), chat_admission_enabled=True)
    scheduler._last_reclaim = time.monotonic()
    costs, selected = [], []
    previous = 0
    for size in (16, 640):
        async with database.immediate_session() as writer:
            binding = await find_space_binding(writer, "20001")
            conversations = [
                await ensure_canonical_conversation(
                    writer,
                    kind="space",
                    space_id=environment.space,
                    space_binding_id=binding.id,
                    primary_scope_key=f"cost-thread:{i}",
                    thread_id=f"thread-{i}",
                )
                for i in range(previous, size)
            ]
            await writer.execute(
                insert(work),
                [
                    task(
                        f"queued-{i}",
                        conversation=conversation.conversation_id,
                        state="queued",
                        at=i + 1.0,
                    )
                    for i, conversation in zip(range(previous, size), conversations, strict=True)
                ],
            )
        with capture_reads(database) as queries:
            tasks = await scheduler.dispatch_once()
            selected.append(len(tasks))
            await asyncio.gather(*tasks)
        costs.append(read_vm_steps(database, queries))
        previous = size
    assert selected[0] == selected[1] > 0
    assert costs[1] <= costs[0] * 4, costs


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "P06: one owned run expands all siblings; "
        "docs/operations/backend-five-pass-audit-20261020.md"
    ),
)
async def test_one_owned_run_does_not_scan_unrelated_siblings(database, tmp_path):
    environment = await social_env(database, tmp_path)
    conversation = environment.context.conversation_id
    repository = WorkRepository(database)
    waits = WorkWaitRepository(repository)
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(work), [task("owner", conversation=conversation, state="waiting_external")]
        )
    binding = dict(
        id="original-wait",
        work_id="owner",
        generation=1,
        current_generation=1,
        work_generation=1,
        work_state="waiting_external",
        mode="any",
        deadline=None,
        conditions_json=json.dumps([{"kind": "owned_run", "run_id": "child-0", "matched": None}]),
    )
    costs, results = [], []
    previous = 0
    for size in (16, 640):
        async with database.immediate_session() as writer:
            await writer.execute(
                insert(work),
                [
                    task(f"child-{i}", conversation=conversation, parent="owner")
                    for i in range(previous, size)
                ],
            )
        with capture_reads(database) as queries:
            async with database.immediate_session() as writer:
                results.append(await waits._observe_page(writer, [binding], 100.0))
        costs.append(read_vm_steps(database, queries))
        previous = size
    assert results[0] == results[1]
    assert results[0][0][1] is not None
    assert costs[1] <= costs[0] * 4, costs


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "P11: empty maintenance revisits archived trees; "
        "docs/operations/backend-five-pass-audit-20261020.md"
    ),
)
async def test_empty_child_maintenance_ignores_already_archived_history(database, tmp_path):
    environment = await social_env(database, tmp_path)
    conversation = environment.context.conversation_id
    repository = WorkRepository(database)
    workers = SubagentRepository(repository)
    old = time.time() - 9 * 86_400
    waiting_root, waiting_child = str(uuid4()), str(uuid4())
    async with database.immediate_session() as writer:
        await writer.execute(insert(work), [task(waiting_root, conversation=conversation, at=old)])
        await writer.execute(
            insert(work),
            [
                task(
                    waiting_child,
                    conversation=conversation,
                    parent=waiting_root,
                    state="waiting_user",
                    at=old,
                )
            ],
        )
        await writer.execute(
            insert(children),
            [
                dict(
                    work_id=waiting_child,
                    source_key="pending-question",
                    brief_json="{}",
                    result_json='{"text":"original question"}',
                    notified_revision=1,
                )
            ],
        )
    costs = []
    previous = 0
    for size in (16, 640):
        roots = [str(uuid4()) for _ in range(size - previous)]
        leaves = [str(uuid4()) for _ in roots]
        async with database.immediate_session() as writer:
            await writer.execute(
                insert(work), [task(root, conversation=conversation, at=old) for root in roots]
            )
            await writer.execute(
                insert(work),
                [
                    task(leaf, conversation=conversation, parent=root, at=old)
                    for root, leaf in zip(roots, leaves, strict=True)
                ],
            )
            await writer.execute(
                insert(children),
                [
                    dict(
                        work_id=leaf,
                        source_key=f"spawn:{leaf}",
                        brief_json="{}",
                        result_json="{}",
                        archived_at=old,
                        notified_revision=1,
                    )
                    for leaf in leaves
                ],
            )
        with capture_reads(database) as queries:
            await workers.maintain()
        costs.append(read_vm_steps(database, queries))
        previous = size
    async with database.sessions() as reader:
        state = await reader.scalar(select(work.c.state).where(work.c.id == waiting_child))
        archived = await reader.scalar(
            select(children.c.archived_at).where(children.c.work_id == waiting_child)
        )
    assert state == "waiting_user" and archived is None
    assert costs[1] <= costs[0] * 4, costs
