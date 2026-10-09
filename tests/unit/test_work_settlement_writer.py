"""One accepted control and one settlement writer for normal and failure exits."""

import json

import pytest
from tests.support.social_identity_cases import social_env

from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository


async def _running_work(database, tmp_path, output_kind="answer"):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(
        repository,
        lease,
        "settlement-writer",
        {"actor_person_id": env.person, "delivery_contract": "return_to_caller"},
        validate,
    )
    accepted = json.loads(
        await control.execute(
            "task_control",
            {"action": "accept", "goal": "answer", "output_kind": output_kind},
            "accept",
        )
    )
    assert accepted["ok"]
    return repository, lease, control


async def _late_input(repository, lease, identity, *, ready):
    input_id = await repository.enqueue(
        lease.conversation_id,
        lease.generation,
        f"late-{ready}",
        kind="message",
        work_id=identity,
        ready=False,
    )
    if ready:
        assert await repository.prepare_input(input_id, {"text": "change of plan"})


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [False, True])
async def test_recovery_after_accepted_complete_retains_late_input(database, tmp_path, ready):
    """T03 counterexample: recover_failure used to UPDATE completed past a new input."""
    repository, lease, control = await _running_work(database, tmp_path)
    result = json.loads(
        await control.execute(
            "task_control", {"action": "complete", "result": "internal answer"}, "complete"
        )
    )
    assert result["ok"] and control.ending == "completed"
    await _late_input(repository, lease, control.current["id"], ready=ready)
    await control.recover_failure(RuntimeError("crash before settlement"))
    row = await repository.get(control.current["id"])
    assert row["state"] == ("queued" if ready else "waiting_external")
    # The candidate never became a replayable success.
    assert "sync_result" not in json.loads(row["checkpoint_json"])


@pytest.mark.asyncio
async def test_recovery_failure_does_not_strand_ready_input_on_failed_work(database, tmp_path):
    repository, lease, control = await _running_work(database, tmp_path)
    assert json.loads(
        await control.execute("task_control", {"action": "fail", "reason": "blocked"}, "fail")
    )["ok"]
    await _late_input(repository, lease, control.current["id"], ready=True)
    await control.recover_failure(RuntimeError("crash before settlement"))
    assert (await repository.get(control.current["id"]))["state"] == "queued"


@pytest.mark.asyncio
async def test_recovery_commits_accepted_complete_and_publishes_result_atomically(
    database, tmp_path
):
    repository, _lease, control = await _running_work(database, tmp_path)
    assert json.loads(
        await control.execute(
            "task_control", {"action": "complete", "result": "internal answer"}, "complete"
        )
    )["ok"]
    await control.recover_failure(RuntimeError("crash after pairing"))
    row = await repository.get(control.current["id"])
    checkpoint = json.loads(row["checkpoint_json"])
    assert row["state"] == "completed"
    assert checkpoint["sync_result"] == "internal answer"
    assert "accepted_control" not in checkpoint


@pytest.mark.asyncio
async def test_exception_without_accepted_control_never_mints_completion(database, tmp_path):
    repository, _lease, control = await _running_work(database, tmp_path)
    await control.recover_failure(RuntimeError("provider failure"))
    assert (await repository.get(control.current["id"]))["state"] in {"queued", "suspended"}


@pytest.mark.asyncio
async def test_new_input_withdraws_accepted_control(database, tmp_path):
    repository, lease, control = await _running_work(database, tmp_path)
    assert json.loads(
        await control.execute("task_control", {"action": "complete", "result": "stale"}, "complete")
    )["ok"]
    await _late_input(repository, lease, control.current["id"], ready=True)
    assert await control.take_inputs("attempt")
    assert control.ending is None and control.accepted is None


@pytest.mark.asyncio
async def test_host_checkpoint_patch_keeps_accepted_control(database, tmp_path):
    repository, lease, control = await _running_work(database, tmp_path)
    assert json.loads(
        await control.execute("task_control", {"action": "fail", "reason": "blocked"}, "fail")
    )["ok"]
    await repository.checkpoint(lease, control.current["id"], {"transcript_ref": "x"})
    row = await repository.get(control.current["id"])
    assert json.loads(row["checkpoint_json"])["accepted_control"]["action"] == "fail"
