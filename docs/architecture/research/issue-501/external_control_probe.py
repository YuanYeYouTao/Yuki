"""Research probe: passing confirms the observed recovery gap, not its repair."""

# The imported case fixture is consumed by pytest's name-based injection.
# ruff: noqa: F401, F811

import json
from hashlib import sha256

import pytest
from sqlalchemy import select
from tests.unit.test_external_agent_repository import prepare, register_device
from tests.unit.test_external_agent_work import case

from yuki.capabilities.invocation import (
    Invocation,
    InvocationIdentity,
    TrustedInvocationContext,
    direct_operation_id,
)
from yuki.domain.messages import ToolCall, ToolFunction
from yuki.external_agents.service import ExternalAgentService
from yuki.runtime.work_journal import WorkJournal
from yuki.runtime.work_schema_v1 import effects
from yuki.sandbox.db_models import SandboxTaskRunModel
from yuki.sandbox.task_repository import SandboxTaskRepository


@pytest.mark.asyncio
async def test_control_receipt_exists_but_work_recovery_cannot_project_it(case):
    control = case.control
    native = await register_device(case.db, case.source["actor_person_id"])
    sessions = await native.repository.remember_sessions(
        native.instance, native.actor, ["probe-thread"]
    )
    native.session = sessions["probe-thread"]
    native.source = {
        **case.source,
        "conversation_id": control.lease.conversation_id,
        "work_id": control.current["id"],
    }
    key = direct_operation_id("issue501-chain", 1, "provider-steer-call")
    request = sha256(key.encode()).hexdigest()
    args = {"action": "steer", "text": "continue original work"}
    invocation = Invocation(
        InvocationIdentity(key, control.current["id"], "issue501-chain", 1, "provider-steer-call"),
        ToolCall("provider-steer-call", ToolFunction("external_agent", json.dumps(args))),
        TrustedInvocationContext(None, "issue501-probe"),
    )
    await control.repository.prepare_effect(
        control.lease, control.current["id"], key, "tool", invocation=invocation.durable_metadata()
    )
    assert await control.repository.admit_dispatch(control.lease, control.current["id"], key)
    _original, created = await prepare(
        native, identity=request, arguments=args, lease=control.lease
    )
    await native.repository.response(
        request, {"status": "succeeded", "pending": False, "admitted": True}
    )
    service = ExternalAgentService(case.db, SandboxTaskRepository(case.db))
    # Direct service recovery uses the original ID before asking for a device connection.
    direct = await service.execute(
        {"action": "get", "operation_id": request}, "read-original-control", native.source
    )
    recovered = json.loads(await WorkJournal(control.repository).effect_result(key))
    async with case.db.read_session() as reader:
        task = await reader.get(SandboxTaskRunModel, request)
        state = (
            await reader.execute(select(effects.c.state).where(effects.c.effect_key == key))
        ).scalar_one()
    assert created and task is None
    assert direct["operation_id"] == request and direct["status"] == "succeeded"
    assert recovered["error"] == "execution_outcome_unknown"
    assert recovered["replay_forbidden"] is True
    assert "operation_id" not in recovered and "request_id" not in recovered
    print(
        json.dumps(
            {"effect_state": state, "domain_receipt_read": direct, "work_recovery": recovered},
            ensure_ascii=False,
        )
    )
