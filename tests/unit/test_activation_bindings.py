"""Cross-task activation hints and shared root/child lease ownership."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.support.social_identity_cases import social_env

from qq_ai_bot.runtime.activation_bindings import ActiveWorkBindings
from qq_ai_bot.runtime.activation_outcome import WorkActivationHandled
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.work_activation import (
    activate_work,
    bind_work_activation,
    current_work_control,
)
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects


async def child_stack(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    parent_lease = await repository.acquire(env.context.conversation_id, 1)
    parent = await repository.accept(parent_lease, source_key="parent", source={}, goal="parent")
    workers = SubagentRepository(repository)
    identity = await workers.start(
        parent_lease, parent["id"], "spawn", {"goal": "child", "output_kind": "answer"}
    )
    lease = await workers.acquire(identity)
    assert lease is not None

    async def validate():
        assert await repository.valid(lease)

    row = await repository.get(identity)
    control = WorkControl(repository, lease, row["source_key"], {}, validate)
    control.current = row
    return repository, workers, parent_lease, control


async def test_old_task_exit_does_not_remove_new_activation_binding():
    bindings = ActiveWorkBindings()
    old, new = SimpleNamespace(current={"id": "old"}), SimpleNamespace(current={"id": "new"})
    registered, leave_old = asyncio.Event(), asyncio.Event()

    async def old_activation():
        with bindings.bind("scope", old):
            registered.set()
            await leave_old.wait()

    task = asyncio.create_task(old_activation())
    await registered.wait()
    assert bindings.get("scope") is old
    with bindings.bind("scope", new):
        leave_old.set()
        await task
        assert bindings.get("scope") is new
        assert new.current is not None
    assert bindings.get("scope") is None


async def test_root_binding_follows_accept_and_releases_empty_scope(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    bindings = ActiveWorkBindings()

    async def validate():
        pass

    async with activate_work(
        repository,
        env.context.conversation_id,
        1,
        "source",
        {},
        validate,
        bindings=bindings,
        scope_key="scope",
    ) as control:
        assert current_work_control.get() is control
        assert bindings.get("scope") is control
        assert control.current is None
        control.current = await repository.accept(
            control.lease, source_key="source", source={}, goal="root"
        )
        assert bindings.get("scope") is control and control.current is not None
        control.ending = "waiting_external"
    assert current_work_control.get() is None
    assert bindings.get("scope") is None
    assert not await repository.valid(control.lease)
    assert (await repository.get(control.current["id"]))["state"] == "waiting_external"


async def test_child_finish_uses_own_lease_and_keeps_parent_lease(database, tmp_path):
    repository, workers, parent_lease, control = await child_stack(database, tmp_path)

    async def finish(active):
        # Settlement already committed; the callback only consumes the result.
        assert active.settled
        await workers.finish(active.lease)

    async with bind_work_activation(control, finish=finish):
        assert current_work_control.get() is control
        assert await repository.valid(parent_lease)
        control.current = await repository.accept_control(
            control.lease,
            control.current["id"],
            {"action": "complete", "call_key": "c", "result": "child result"},
        )
    assert not await repository.valid(control.lease)
    assert await repository.valid(parent_lease)
    async with database.sessions() as session:
        result = await session.scalar(
            select(children.c.result_json).where(children.c.work_id == control.lease.work_id)
        )
    assert json.loads(result)["text"] == "child result"
    assert json.loads(result)["state"] == "completed"
    await repository.release(parent_lease)


@pytest.mark.parametrize("mode", ["lease_lost", "recovery_deferred"])
async def test_child_exit_does_not_publish_results_without_confirmed_authority(
    database, tmp_path, mode
):
    repository, _workers, parent_lease, control = await child_stack(database, tmp_path)
    finish = AsyncMock()

    async def release_lease():
        await repository.release(control.lease)

    release = AsyncMock(side_effect=release_lease)
    async with bind_work_activation(control, finish=finish, release=release):
        if mode == "lease_lost":
            await repository.release(control.lease)
        else:
            control.recovery_deferred = True
    finish.assert_not_awaited()
    release.assert_awaited_once()
    assert current_work_control.get() is None
    assert not await repository.valid(control.lease)
    assert await repository.valid(parent_lease)
    await repository.release(parent_lease)


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_child_preserves_original_budget_unknown_effect_and_unbinds(
    database, tmp_path, cancel
):
    repository, _workers, parent_lease, control = await child_stack(database, tmp_path)
    identity = control.current["id"]
    await repository.checkpoint(control.lease, identity, {"keep": "checkpoint"}, models=2, tools=1)
    await repository.prepare_effect(control.lease, identity, "original-call", "tool")
    await repository.record_effect("original-call", "unknown", {"run_id": "original-run"})
    bindings = ActiveWorkBindings()
    failure = asyncio.CancelledError() if cancel else RuntimeError("execution fixture")
    with pytest.raises(asyncio.CancelledError if cancel else WorkActivationHandled):
        async with bind_work_activation(control, bindings=bindings, scope_key="child"):
            raise failure
    assert current_work_control.get() is None
    assert bindings.get("child") is None
    assert not await repository.valid(control.lease)
    row = await repository.get(identity)
    assert row["model_requests"] == 2
    assert row["tool_calls"] == 1
    assert json.loads(row["checkpoint_json"])["keep"] == "checkpoint"
    async with database.sessions() as session:
        effect = (
            (await session.execute(select(effects).where(effects.c.effect_key == "original-call")))
            .mappings()
            .one()
        )
    assert effect["state"] == "unknown"
    assert json.loads(effect["receipt_json"])["run_id"] == "original-run"
    assert await repository.valid(parent_lease)
    await repository.release(parent_lease)
