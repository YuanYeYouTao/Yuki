"""Independent cloud bridge review: local completion resumes the original Work."""

import json

from sqlalchemy import select
from tests.unit.test_local_device_bridge import setup_device

from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import inputs
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.sandbox.continuation_worker import SandboxContinuationWorker


async def test_local_terminal_completion_reaches_original_owned_wait(database, tmp_path):
    c = await setup_device(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(c.env.context.conversation_id, 1)
    assert lease

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(
        repository,
        lease,
        "review-original-work",
        {"actor_person_id": c.env.person, "principal_kind": "person", "origin": "user_message"},
        validate,
    )
    control.current = await repository.accept(
        lease,
        source_key="review-original-work",
        source=control.source,
        goal="follow original local execution",
    )
    original_work = control.current["id"]
    started = await c.local.execute(
        "terminal_exec",
        {"device_id": c.device_id, "command": "original job"},
        request_id="original-local-job",
        source={
            **c.source,
            **control.source,
            "work_id": original_work,
            "conversation_id": lease.conversation_id,
            "generation": lease.generation,
        },
    )
    wait = json.loads(
        await control.execute(
            "task_control",
            {"action": "wait", "conditions": [{"kind": "owned_run", "run_id": started["run_id"]}]},
            "wait-local-job",
        )
    )
    assert wait["ok"]
    await control.settle(pending_inputs=False)
    completion = {
        "request_id": "original-local-job",
        "run_id": started["run_id"],
        "result": {
            "run_id": started["run_id"],
            "status": "succeeded",
            "pending": False,
            "exit_code": 0,
        },
    }
    await c.local.receive(c.device_id, completion)
    await SandboxContinuationWorker(repository).drain_once()
    assert await WorkWaitRepository(repository).deliver_due() == 1
    current = await repository.get(original_work)
    assert current["state"] == "queued"
    assert (await WorkWaitRepository(repository).describe(original_work))["wait_id"] == wait[
        "wait_id"
    ]
    await c.local.receive(c.device_id, completion)
    await SandboxContinuationWorker(repository).drain_once()
    assert await WorkWaitRepository(repository).deliver_due() == 0
    async with database.read_session() as reader:
        facts = list(
            await reader.scalars(
                select(inputs.c.source_key).where(inputs.c.work_id == original_work)
            )
        )
    assert facts.count("completion:original-local-job") == 1
    assert facts.count(f"wait:{wait['wait_id']}") == 1
    assert c.connection.request.call_count == 1
    await c.service.close()
