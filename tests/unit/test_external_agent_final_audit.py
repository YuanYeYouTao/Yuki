"""Terminal native facts must reach the still-active original Work."""

import json

import pytest
from sqlalchemy import func, select
from tests.unit.test_external_agent_repository import connect, event, prepare, register_device
from tests.unit.test_external_agent_repository import native as native_fixture
from tests.unit.test_external_agent_work import case as work_case

from yuki.external_agents.service import ExternalAgentService
from yuki.external_agents.transport import DeviceConnection
from yuki.runtime.work_schema_v1 import inputs, work
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
    async with database.sessions() as session:
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
        assert json.loads(json.loads(payload)["text"])["status"] == "cancelled"


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
    async with database.sessions() as session:
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
    await SandboxContinuationWorker(case.control.repository).drain_once()
    async with database.sessions() as session:
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
