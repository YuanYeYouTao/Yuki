"""Terminal native facts must reach the still-active original Work."""

import json

import pytest
from sqlalchemy import event as sql_events
from sqlalchemy import func, select
from tests.unit.test_external_agent_repository import connect, event, prepare, register_device
from tests.unit.test_external_agent_repository import native as native_fixture
from tests.unit.test_external_agent_work import case as work_case

from yuki.capabilities.results import ToolExecutionResult
from yuki.external_agents.repository import execution_key
from yuki.external_agents.service import ExternalAgentService
from yuki.external_agents.transport import DeviceConnection
from yuki.runtime.effect_outcomes import execution_evidence, execution_finished
from yuki.runtime.work_schema_v1 import effects, inputs, work
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.sandbox.continuation_worker import SandboxContinuationWorker
from yuki.sandbox.db_models import SandboxTaskContinuationModel, SandboxTaskRunModel

case = work_case
native = native_fixture


@pytest.mark.parametrize("native_status", ["cancelled", "interrupted"])
async def test_native_cancellation_is_reportable_to_its_active_work(database, case, native_status):
    env = await register_device(database, case.source["actor_person_id"])
    env.source = {
        **case.source,
        "work_id": case.control.current["id"],
        "conversation_id": case.control.lease.conversation_id,
    }
    env.session = (
        await env.repository.remember_sessions(env.instance, env.actor, ["native-thread"])
    )["native-thread"]
    operation, _ = await prepare(
        env,
        action="prompt",
        arguments={"action": "prompt", "prompt": "run tests"},
        lease=case.control.lease,
    )
    await env.repository.receive_events(
        env.device["device_id"],
        [event("local-user-cancelled", kind="completed", payload={"status": native_status})],
    )
    await SandboxContinuationWorker(case.control.repository).drain_once()
    async with database.read_session() as session:
        owner_state = await session.scalar(
            select(work.c.state).where(work.c.id == operation.work_id)
        )
        assert owner_state not in {"completed", "failed", "cancelled"}
        assert (
            await session.scalar(
                select(func.count())
                .select_from(inputs)
                .where(
                    inputs.c.work_id == operation.work_id,
                    inputs.c.kind == "completion",
                    inputs.c.state == "pending",
                )
            )
            == 1
        )
        receipt = await session.get(SandboxTaskContinuationModel, operation.id)
        assert receipt.state == "observed"
        payload = await session.scalar(
            select(inputs.c.payload_json).where(inputs.c.source_key == f"completion:{operation.id}")
        )
        received = json.loads(json.loads(payload)["text"])
        assert received["status"] == "cancelled"
        assert received["kind"] == "external_agent_completion"
        assert received["operation_id"] == operation.id
        assert received["instance_id"] == env.instance
        assert received["session_id"] == env.session
        assert received["execution_id"] == execution_key(env.session, "native-turn")


async def test_get_internal_execution_reads_its_original_turn_without_creating_work(
    database, native
):
    commands = []
    original_execution = await native.repository.remember_execution(
        native.session, "old-native-turn"
    )

    class Socket:
        async def send_json(self, command):
            commands.append(command)
            assert command["execution_id"] == "old-native-turn"
            assert command["session_id"] == "native-thread"
            connection.respond(
                {
                    "id": command["id"],
                    "result": {
                        "session_id": "native-thread",
                        "execution_id": "old-native-turn",
                        "status": "completed",
                        "pending": False,
                    },
                }
            )

        async def close(self, code=1000, reason=None):
            pass

    connection = DeviceConnection(native.device["device_id"], Socket())
    await connect(native, connection)
    result = await native.service.execute(
        {"action": "get", "execution_id": original_execution},
        "read-original-execution",
        native.source,
    )
    assert result["execution_id"] == original_execution
    assert result["session_id"] == native.session
    assert result["status"] == "completed"
    assert [item["action"] for item in commands] == ["get"]
    async with database.read_session() as session:
        assert await session.scalar(select(func.count()).select_from(work)) == 0


@pytest.mark.parametrize("action,native_status", [("prompt", "running"), ("queue", "queued")])
async def test_detach_ends_work_relationship_without_claiming_native_success(
    database, case, action, native_status
):
    env = await register_device(database, case.source["actor_person_id"])
    env.service = ExternalAgentService(database, case.tasks)
    env.session = (
        await env.repository.remember_sessions(env.instance, env.actor, ["native-thread"])
    )["native-thread"]
    commands = []

    class Socket:
        async def send_json(self, command):
            commands.append(command)
            if command["action"] == action:
                result = {
                    "status": native_status,
                    "pending": True,
                    "admitted": True,
                    "session_id": "native-thread",
                    **(
                        {"execution_id": "native-turn"}
                        if action == "prompt"
                        else {"queued_submission_id": "native-queue-item"}
                    ),
                }
            else:
                assert command["action"] == "detach"
                result = {
                    "status": "completed",
                    "pending": False,
                    "admitted": True,
                    "detached": True,
                }
            connection.respond({"id": command["id"], "result": result})

        async def close(self, code=1000, reason=None):
            pass

    connection = DeviceConnection(env.device["device_id"], Socket())
    await connect(env, connection)
    source = {"actor_person_id": env.actor}
    original = await env.service.execute(
        {"action": action, "session_id": env.session, "prompt": "continue local work"},
        f"original-{action}",
        source,
    )
    owner = case.control.current["id"]
    repository, lease = case.control.repository, case.control.lease
    effect_key = f"original-{action}-effect"
    await repository.prepare_effect(
        lease,
        owner,
        effect_key,
        "tool",
        invocation={"version": 1, "tool_id": "external_agent", "action": action, "revision": 0},
    )
    await repository.record_effect(
        effect_key,
        "accepted",
        {
            "outcome": execution_evidence(
                ToolExecutionResult(
                    ok=True, provider_id="core", tool_name="external_agent", data=original
                ),
                tool="external_agent",
                side_effecting=True,
                arguments=json.dumps({"action": action}),
            )
        },
    )
    waits = WorkWaitRepository(repository)
    await waits.register(
        lease,
        work_id=owner,
        source=case.source,
        call_key=f"wait-original-{action}",
        mode="any",
        conditions=[{"kind": "owned_run", "run_id": original["run_id"]}],
        deadline_at=None,
    )
    current = await repository.get(owner)
    await repository.transition(lease, owner, current["revision"], "waiting_external")
    assert await case.control.repository.has_owned_execution(case.control.lease, owner)
    await env.service.execute(
        {"action": "detach", "operation_id": original["operation_id"]}, f"detach-{action}", source
    )
    assert not await case.control.repository.has_owned_execution(case.control.lease, owner)
    result = await env.service.execute(
        {"action": "get", "operation_id": original["operation_id"]},
        f"read-detached-{action}",
        source,
    )
    assert result["status"] == "detached" and result["pending"] is False
    assert result["native_status"] == native_status and result["native_pending"] is True
    if action == "queue":
        assert result["queued_submission_id"] == "native-queue-item"
    assert [command["action"] for command in commands] == [action, "detach"]
    await case.control.reconcile_completed_children()
    async with database.read_session() as session:
        receipt = json.loads(
            await session.scalar(
                select(effects.c.receipt_json).where(effects.c.effect_key == effect_key)
            )
        )
    outcome = receipt["outcome"]
    assert outcome["status"] == outcome["operation_status"] == "detached"
    assert outcome["native_status"] == native_status and outcome["native_pending"] is True
    assert outcome["pending"] is False and execution_finished(outcome)
    assert outcome["tool"] == "external_agent" and outcome["action"] == action
    assert outcome["side_effecting"] is True
    assert await waits.deliver_due() == 1
    binding = await waits.describe(owner)
    matched = binding["conditions"][0]["matched"]
    assert binding["status"] == "delivered" and (await repository.get(owner))["state"] == "queued"
    assert matched["status"] == matched["operation_status"] == "detached"
    assert matched["native_status"] == native_status and matched["native_pending"] is True
    assert await waits.deliver_due() == 0
    await SandboxContinuationWorker(case.control.repository).drain_once()
    async with database.read_session() as session:
        task = await session.get(SandboxTaskRunModel, original["operation_id"])
        assert task.status == "completed"
        payload = await session.scalar(
            select(inputs.c.payload_json).where(
                inputs.c.source_key == f"completion:{original['operation_id']}"
            )
        )
        signal = json.loads(json.loads(payload)["text"])
        assert signal["status"] == "detached"
        assert signal["native_status"] == native_status and signal["native_pending"] is True
        wait_payload = await session.scalar(
            select(inputs.c.payload_json).where(inputs.c.source_key == f"wait:{binding['wait_id']}")
        )
        wait_signal = json.loads(json.loads(wait_payload)["text"])
        assert wait_signal["conditions"][0]["matched"] == matched


async def test_unavailable_hello_keeps_failure_fact_without_dispatching_to_missing_driver(native):
    original, _ = await prepare(native)
    commands = []

    class Socket:
        async def send_json(self, command):
            commands.append(command)

        async def close(self, code=1000, reason=None):
            pass

    connection = DeviceConnection(native.device["device_id"], Socket())
    await native.service.connect(
        native.device["device_id"],
        connection,
        {
            "protocol_version": 2,
            "instances": [
                {
                    "id": "profile",
                    "label": "local Codex",
                    "driver": "codex",
                    "capabilities": [],
                    "status": "unavailable",
                    "error_type": "FileNotFoundError",
                }
            ],
        },
    )
    catalog = await native.service.catalog(native.actor)
    assert catalog["devices"][0]["online"] is True
    profile = catalog["instances"][0]
    assert profile["online"] is False
    assert profile["status"] == "unavailable" and profile["error_type"] == "FileNotFoundError"
    assert profile["capabilities"] == []
    result = await native.service.execute(
        {"action": "steer", "operation_id": original.id, "text": "continue"},
        "not-dispatched-to-missing-driver",
        native.source,
    )
    assert result["error"] == "device_offline" and result["executed"] is False
    assert commands == []
    with pytest.raises(ValueError, match="external_agent_not_found"):
        await native.repository.operation("not-dispatched-to-missing-driver", native.actor)


@pytest.mark.parametrize(
    "action,cancel_status", [("queue", "cancelled"), ("prompt", "cancel_submitted")]
)
async def test_exact_queue_cancel_settles_but_submitted_interrupt_waits_for_native_terminal(
    database, case, action, cancel_status
):
    env = await register_device(database, case.source["actor_person_id"])
    env.service = ExternalAgentService(database, case.tasks)
    env.session = (
        await env.repository.remember_sessions(env.instance, env.actor, ["native-thread"])
    )["native-thread"]
    commands = []

    class Socket:
        async def send_json(self, command):
            commands.append(command)
            if command["action"] == action:
                result = {
                    "status": "queued" if action == "queue" else "running",
                    "pending": True,
                    "admitted": True,
                    "session_id": "native-thread",
                    **(
                        {"queued_submission_id": "native-queue-item"}
                        if action == "queue"
                        else {"execution_id": "native-turn"}
                    ),
                }
            else:
                assert command["action"] == "cancel"
                assert command["arguments"]["operation_id"] == f"original-{action}"
                if action == "queue":
                    assert command["arguments"]["queued_submission_id"] == "native-queue-item"
                else:
                    assert command["execution_id"] == "native-turn"
                result = {"status": cancel_status, "pending": False, "admitted": True}
            connection.respond({"id": command["id"], "result": result})

        async def close(self, code=1000, reason=None):
            pass

    connection = DeviceConnection(env.device["device_id"], Socket())
    await connect(env, connection)
    source = {"actor_person_id": env.actor}
    original = await env.service.execute(
        {"action": action, "session_id": env.session, "prompt": "run tests"},
        f"original-{action}",
        source,
    )
    await env.service.execute(
        {"action": "cancel", "operation_id": original["operation_id"]},
        f"cancel-{action}",
        source,
    )
    owner = case.control.current["id"]
    async with database.read_session() as session:
        task = await session.get(SandboxTaskRunModel, original["operation_id"])
        assert task.status == ("completed" if action == "queue" else "waiting")
        if action == "queue":
            completion = json.loads(task.completion_json)
            assert completion["status"] == "cancelled" and completion["pending"] is False
        else:
            assert await session.get(SandboxTaskContinuationModel, original["operation_id"]) is None
    assert await case.control.repository.has_owned_execution(case.control.lease, owner) is (
        action == "prompt"
    )
    if action == "prompt":
        await env.repository.receive_events(
            env.device["device_id"],
            [
                event(
                    "actual-native-terminal",
                    operation=original["operation_id"],
                    kind="completed",
                    payload={"status": "completed"},
                )
            ],
        )
    await SandboxContinuationWorker(case.control.repository).drain_once()
    async with database.read_session() as session:
        payloads = list(
            await session.scalars(
                select(inputs.c.payload_json).where(
                    inputs.c.source_key == f"completion:{original['operation_id']}"
                )
            )
        )
        assert len(payloads) == 1
        assert json.loads(json.loads(payloads[0])["text"])["status"] == (
            "cancelled" if action == "queue" else "succeeded"
        )
    assert [command["action"] for command in commands] == [action, "cancel"]


async def test_unchanged_pending_journal_reads_never_acquire_sqlite_writer(database, native):
    original, _ = await prepare(native)
    pending = {
        "status": "running",
        "pending": True,
        "admitted": True,
        "session_id": "native-thread",
        "execution_id": "native-turn",
    }
    await native.repository.response(original.id, pending)

    class Socket:
        async def send_json(self, command):
            assert command["action"] == "get"
            assert command["arguments"] == {"operation_id": original.id}
            connection.respond({"id": command["id"], "result": pending})

        async def close(self, code=1000, reason=None):
            pass

    connection = DeviceConnection(native.device["device_id"], Socket())
    await connect(native, connection)
    writes = []

    def record_write(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith(("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    sql_events.listen(database.engine.sync_engine, "before_cursor_execute", record_write)
    try:
        for number in range(3):
            result = await native.service.execute(
                {"action": "get", "operation_id": original.id},
                f"observe-same-pending-{number}",
                native.source,
            )
            assert result["status"] == "running" and result["pending"] is True
    finally:
        sql_events.remove(database.engine.sync_engine, "before_cursor_execute", record_write)
    assert writes == []
