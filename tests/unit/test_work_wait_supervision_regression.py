"""Controlled #514 sequence with the production Main loop and isolated SQLite."""

from dataclasses import replace

from sqlalchemy import update
from tests.support.work_runner_helpers import case, run, tool

from yuki.domain.messages import ChatMessage, ChatResponse
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_schema_v1 import work
from yuki.runtime.work_wait import WorkWaitRepository


async def test_auxiliary_activation_cannot_turn_original_wait_into_quiet_completion(
    database, tmp_path
):
    wait = {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 1800}]}
    probe = await case(
        database,
        tmp_path,
        [
            ChatResponse("", 0, tool_calls=(tool("task_control", wait, "original-wait"),)),
            ChatResponse(
                "", 0, tool_calls=(tool("task_control", wait, "new-wait-after-stray-wake"),)
            ),
            ChatResponse("", 0),
        ],
    )
    first = await run(probe)
    assert first.work_state == "waiting_external"
    await probe.control.settle(pending_inputs=False)
    identity = probe.control.current["id"]
    original_wait = await WorkWaitRepository(probe.repository).describe(identity)
    counters = {key: probe.control.current[key] for key in ("model_requests", "tool_calls")}
    # Reproduce the historical order without claiming to reproduce its original
    # trigger: a foreign/auxiliary activation before the time condition is due.
    async with database.immediate_session() as writer:
        await writer.execute(update(work).where(work.c.id == identity).values(state="running"))
    resumed = WorkControl(
        probe.repository,
        probe.control.lease,
        probe.control.source_key,
        probe.control.source,
        probe.control.validate,
        current=await probe.repository.get(identity),
    )
    await probe.runner.run(
        (ChatMessage("user", "auxiliary activation; no new business input"),),
        replace(probe.runtime, work_control=resumed),
        probe.backend,
    )
    await resumed.settle(pending_inputs=False)
    assert resumed.current["state"] == "waiting_external"
    assert resumed.accepted["wait_id"] == original_wait["wait_id"]
    assert len(probe.provider.requests) == 1
    assert {key: resumed.current[key] for key in counters} == counters
    current_wait = await WorkWaitRepository(probe.repository).describe(identity)
    assert current_wait["conditions"] == original_wait["conditions"]
    assert current_wait["wait_id"] == original_wait["wait_id"]
