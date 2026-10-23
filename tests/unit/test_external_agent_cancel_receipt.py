"""An admitted cancel reads the original execution's terminal receipt without dispatch."""

import json
from hashlib import sha256

import pytest
from sqlalchemy import select
from tests.unit.test_external_agent_repository import event, prepare, register_device
from tests.unit.test_external_agent_work import case as work_case
from tests.unit.test_work_domain_receipts import admitted, read_result

from yuki.external_agents.db_models import AgentOperationModel
from yuki.external_agents.repository import AgentRepository, execution_key
from yuki.external_agents.service import ExternalAgentService
from yuki.runtime.effect_queries import RuntimeEffectQueries
from yuki.runtime.work_wait import WorkWaitRepository

case = work_case


async def environment(database, case):
    env = await register_device(database, case.source["actor_person_id"])
    env.service = ExternalAgentService(database, case.tasks)
    env.session = (
        await env.repository.remember_sessions(env.instance, env.actor, ["native-thread"])
    )["native-thread"]
    env.source = {
        **case.source,
        "work_id": case.control.current["id"],
        "conversation_id": case.control.lease.conversation_id,
    }
    return env


@pytest.mark.parametrize("terminal_status", ["cancelled", "completed", "failed"])
async def test_cancel_reads_terminal_rewatch_without_reviving_detached_prompt(
    database, case, terminal_status
):
    env = await environment(database, case)
    running = {
        "status": "running",
        "pending": True,
        "admitted": True,
        "session_id": "native-thread",
        "execution_id": "native-turn",
    }
    original, _ = await prepare(env, identity="original-prompt", action="prompt")
    original = await env.repository.response(original.id, running)
    await env.service._detached(original)
    key = await admitted(case.control, "external_agent", {"action": "cancel"})
    cancel, _ = await prepare(env, identity=sha256(key.encode()).hexdigest(), action="cancel")
    cancel = await env.repository.response(cancel.id, running)
    watcher, _ = await prepare(env, identity="later-watch", action="watch")
    await env.repository.response(watcher.id, running)
    before = await env.repository.operation_view(cancel)
    assert before["pending"] and before["status"] == "running"
    owner = case.control.current["id"]
    waits = WorkWaitRepository(case.control.repository)
    await waits.register(
        case.control.lease,
        work_id=owner,
        source=case.source,
        call_key="wait-original-execution",
        mode="any",
        conditions=[{"kind": "owned_run", "run_id": watcher.run_id}],
        deadline_at=None,
    )
    current = await case.control.repository.get(owner)
    await case.control.repository.transition(
        case.control.lease, owner, current["revision"], "waiting_external"
    )
    await env.repository.receive_events(
        env.device["device_id"],
        [
            event(
                "native-terminal",
                operation=watcher.id,
                kind="completed",
                payload={
                    "status": terminal_status,
                    **({"error": {"code": "native_failed"}} if terminal_status == "failed" else {}),
                },
            )
        ],
    )
    # A new repository/service reads durable facts while the connector is offline.
    service = ExternalAgentService(database, case.tasks)
    view = await service.execute(
        {"action": "get", "operation_id": cancel.id}, "read-only-cancel", env.source
    )
    assert view["operation_id"] == cancel.id
    assert view["status"] == terminal_status and view["pending"] is False
    assert view["execution_id"] == execution_key(env.session, "native-turn")
    assert view["uncertain"] is False
    if terminal_status == "failed":
        assert view["error"] == "native_failed"
    recovered = await RuntimeEffectQueries(database).inspect_native_operation(
        work_id=case.control.current["id"], operation_key=key, action="cancel"
    )
    assert recovered is not None and recovered.data["status"] == terminal_status
    assert recovered.data["pending"] is False
    _, evidence, counters = await read_result(database, case.control, key)
    assert evidence["pending"] is False and counters == (0, 1)
    assert await waits.deliver_due() == 1
    wait = await waits.describe(owner)
    assert wait["conditions"][0]["matched"]["status"] == (
        "succeeded" if terminal_status == "completed" else terminal_status
    )
    assert await waits.deliver_due() == 0
    async with database.read_session() as reader:
        original_row = await reader.get(AgentOperationModel, original.id)
        cancel_row = await reader.get(AgentOperationModel, cancel.id)
        assert json.loads(original_row.result_json)["status"] == "detached"
        assert json.loads(cancel_row.result_json) == running
        assert len(list(await reader.scalars(select(AgentOperationModel)))) == 3


@pytest.mark.parametrize("difference", ["execution", "instance", "unadmitted", "detached"])
async def test_cancel_cannot_borrow_an_unrelated_or_non_native_terminal(database, case, difference):
    env = await environment(database, case)
    running = {
        "status": "running",
        "pending": True,
        "admitted": difference != "unadmitted",
        "session_id": "native-thread",
        "execution_id": "native-turn",
    }
    cancel, _ = await prepare(env, identity="pending-cancel", action="cancel")
    cancel = await env.repository.response(cancel.id, running)
    observer = await environment(database, case) if difference == "instance" else env
    execution = "different-turn" if difference == "execution" else "native-turn"
    watch, _ = await prepare(
        observer, identity="other-watch", action="watch", native_execution_id=execution
    )
    await observer.repository.response(
        watch.id,
        {
            "status": "detached" if difference == "detached" else "cancelled",
            "pending": False,
            "admitted": True,
            "session_id": "native-thread",
            "execution_id": execution,
        },
    )
    view = await AgentRepository(database).operation_view(cancel)
    assert view["pending"] is True and view["status"] == "running"
