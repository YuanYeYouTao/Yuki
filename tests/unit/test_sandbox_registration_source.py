"""Sandbox registration derives receipt ownership from the active work lease."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.support.social_identity_cases import social_env

from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import inputs
from qq_ai_bot.sandbox.db_models import SandboxTaskContinuationModel
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", ["missing", None, 1, 2, True])
async def test_automation_registration_and_completion(database, tmp_path, generation):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    assert lease is not None

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "automation-source", {}, validate)
    control.current = await repo.accept(
        lease, source_key="automation-source", source={}, goal="background execution"
    )
    source = {
        "work_id": control.current["id"],
        "conversation_id": lease.conversation_id,
        "origin": "scheduled_automation",
        "allow_admin_actions": False,
    }
    if generation != "missing":
        source["generation"] = generation
    original = dict(source)
    tasks = SandboxTaskRepository(database)
    token = current_work_control.set(control)
    try:
        if generation == 2 or generation is True:
            with pytest.raises(ValueError, match="invalid_task_work_generation"):
                await tasks.prepare("command", {"command": "true"}, source)
            assert await tasks.get("command") is None
            return
        row = await tasks.prepare("command", {"command": "true"}, source)
        assert json.loads(row.source_json) == {**source, "generation": 1}
        assert source == original
        repeated = await tasks.prepare("command", {"command": "true"}, source)
        assert repeated.source_json == row.source_json
    finally:
        current_work_control.reset(token)
        await repo.release(lease)

    # Completion runs outside the originating Agent activation, as in production.
    lease = await repo.acquire(env.context.conversation_id, 1)
    assert lease is not None
    await repo.transition(
        lease, control.current["id"], control.current["revision"], "waiting_external"
    )
    await repo.release(lease)
    run_id = str(uuid4())
    await tasks.receive(
        {
            "request_id": "command",
            "run_id": run_id,
            "result": {"run_id": run_id, "status": "succeeded", "pending": False, "exit_code": 0},
        }
    )
    await repo.route_child_completion("command")
    await repo.route_child_completion("command")
    parent = await repo.get(control.current["id"])
    assert parent["state"] == "queued"
    async with database.sessions() as session:
        notification = (await session.execute(select(inputs))).mappings().one()
        assert notification["work_id"] == parent["id"]
        assert notification["generation"] == 1
        receipt = await session.get(SandboxTaskContinuationModel, "command")
        assert receipt.state == "observed"
        assert receipt.reason == "forwarded_to_parent_work"
