"""Original observation receipts survive gaps, later turns and transport replacement."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from tests.unit.test_external_agent_repository import connect, event, prepare, receipt
from tests.unit.test_external_agent_repository import native as native
from tests.unit.test_external_agent_work import case as case
from tests.unit.test_external_agent_writer_contract import owned

from yuki.external_agents.db_models import AgentEventModel, AgentExecutionModel
from yuki.external_agents.repository import encode, execution_key
from yuki.external_agents.service import ExternalAgentService
from yuki.external_agents.transport import DeviceConnection
from yuki.runtime.work_schema_v1 import inputs
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.sandbox.db_models import SandboxTaskRunModel


class Socket:
    async def send_json(self, value):
        pass

    async def close(self, code=1000, reason=None):
        pass


def state(sequence, status, *, execution="native-turn", pending=True, **values):
    return {
        "session_id": "native-thread",
        "execution_id": execution,
        "status": status,
        "pending": pending,
        "admitted": True,
        "state_sequence": sequence,
        **values,
    }


async def push(env, operation, sequence, status, *, execution="native-turn", **values):
    fact = event(
        f"fact-{sequence}",
        operation=operation.id,
        sequence=sequence,
        execution_id=execution,
        operation_state=state(sequence, status, execution=execution, **values),
        payload={"status": status},
    )
    await env.repository.receive_events(env.device["device_id"], [fact])
    return fact


async def test_unknown_and_recovery_replace_running_without_new_admission(native):
    operation, _ = await prepare(native)
    await native.repository.response(operation.id, state(1, "running"))
    await push(native, operation, 2, "unknown", observation_error="stream_closed")
    unknown = await native.repository.operation(operation.id, native.actor)
    assert json.loads(unknown.result_json) == state(2, "unknown", observation_error="stream_closed")
    await native.repository.response(operation.id, state(1, "running"))
    assert (
        json.loads((await native.repository.operation(operation.id, native.actor)).result_json)[
            "status"
        ]
        == "unknown"
    )
    await push(native, operation, 3, "running", stream_gap=True)
    await push(native, operation, 4, "completed", pending=False)
    await push(native, operation, 5, "unknown")
    current = await native.repository.operation(operation.id, native.actor)
    terminal = json.loads(current.result_json)
    assert terminal["status"] == "completed" and terminal["state_sequence"] == 4


async def test_session_watch_keeps_multiple_turns_without_completing_observation(case):
    env, _ = await owned(case)
    operation, _ = await prepare(
        env,
        identity="session-watch",
        action="watch",
        command={"id": "session-watch", "action": "watch", "observation_scope": "session"},
        native_execution_id=None,
        lease=case.control.lease,
    )
    await push(env, operation, 1, "running", execution="turn-one")
    await push(env, operation, 2, "idle", execution="turn-one")
    await push(env, operation, 3, "running", execution="turn-two")
    current = await env.repository.operation(operation.id, env.actor)
    assert current.native_execution_id is None
    async with case.db.read_session() as reader:
        executions = list(await reader.scalars(select(AgentExecutionModel.native_id)))
        task = await reader.get(SandboxTaskRunModel, operation.id)
    assert {"turn-one", "turn-two"} <= set(executions)
    assert task.status == "waiting"
    events = await env.repository.event_page(operation.id, 0)
    assert events[-1]["execution_id"] == execution_key(env.session, "turn-two")
    assert events[-1]["native_reference"]["execution_id"] == "turn-two"
    assert events[-1]["session_id"] == env.session
    service = ExternalAgentService(case.db, case.tasks)
    view = await service._view(current)
    assert view["pending"] and view["execution_id"] == execution_key(env.session, "turn-two")
    assert view["last_confirmed"]["status"] == "running"
    await service._detached(current)
    await push(env, operation, 4, "running", execution="turn-three")
    current = await env.repository.operation(operation.id, env.actor)
    assert current.detached_at is not None
    assert json.loads(current.result_json)["status"] == "detached"


async def test_single_execution_still_rejects_another_turn(native):
    operation, _ = await prepare(native)
    with pytest.raises(ValueError, match="execution_mismatch"):
        await push(native, operation, 1, "running", execution="another-turn")


async def test_native_failure_without_transport_error_code_remains_readable(case):
    env, operation = await owned(case)
    error = {"message": "native turn failed", "codexErrorInfo": "serverOverloaded"}
    await push(env, operation, 1, "failed", pending=False, error=error)
    service = ExternalAgentService(case.db, case.tasks)
    original = await env.repository.operation(operation.id, env.actor)
    view = await service._view(original)
    assert view["status"] == "failed" and view["pending"] is False
    assert view["error"] == error


@pytest.mark.parametrize("cleanup", [False, True])
@pytest.mark.parametrize("device_online", [False, True])
async def test_native_offline_detach_keeps_real_device_receipt_boundary(
    case, cleanup, device_online
):
    env, _ = await owned(case)
    env.service = ExternalAgentService(case.db, case.tasks)
    observation, _ = await prepare(
        env,
        identity="offline-watch",
        action="watch",
        command={"id": "offline-watch", "action": "watch", "observation_scope": "session"},
        native_execution_id=None,
        lease=case.control.lease,
    )
    sent, receipts = [], []

    class DetachSocket(Socket):
        async def send_json(self, value):
            # Protocol 5: connecting reads nothing back; only the detach is written.
            sent.append(value)
            assert value["action"] == "detach"
            assert value["arguments"]["operation_id"] == observation.id
            receipts.append(
                receipt(
                    value,
                    {
                        "status": "detached",
                        "pending": False,
                        "detached": True,
                        "admitted": True,
                        "executed": True,
                    },
                )
            )

    connection = DeviceConnection(env.device["device_id"], DetachSocket())
    if device_online:
        await env.service.connect(
            env.device["device_id"],
            connection,
            {
                "protocol_version": 5,
                "instances": [
                    {
                        "id": "profile",
                        "driver": "codex",
                        "status": "unavailable",
                        "capabilities": [],
                    }
                ],
            },
        )
        assert not connection.instances
        result = await env.service.execute(
            {"action": "prompt", "session_id": observation.session_id, "prompt": "original"},
            "unavailable-prompt",
            env.source,
        )
        assert result["error"] == "device_offline" and result["executed"] is False
        assert not sent
    if cleanup:
        [control] = await env.service.cleanup_tasks([observation.id], "cleanup-original")
        if device_online:
            # Registered before it is written; its receipt event settles the target.
            # One registered attempt per connection.
            assert (
                control["operation_id"]
                == f"cleanup-original:{observation.id}:detach:{connection.id}"
            )
            assert control["pending"] is True
            assert (await env.repository.operation(observation.id, env.actor)).detached_at is None
    else:
        await env.service.execute(
            {"action": "detach", "operation_id": observation.id}, "detach-original", env.source
        )
        control = {"operation_id": "detach-original"}
    await env.service.drain()
    await env.service.receive_events(env.device["device_id"], receipts)
    result = await env.service.execute(
        {"action": "get", "operation_id": control["operation_id"]}, "read-detach", env.source
    )
    original = await env.repository.operation(observation.id, env.actor)
    # Source cleanup still needs the device; an explicit detach ends the Work
    # relationship without it and says so, never claiming a native detach.
    assert (original.detached_at is not None) is (device_online or not cleanup)
    assert len(sent) == int(device_online)
    if device_online:
        assert result["status"] == "detached"
    elif cleanup:
        assert result.get("status") != "detached"
    else:
        assert result["status"] == "released" and result["released_without_device"] is True
        assert result["executed"] is False
        released = json.loads(original.result_json)
        assert released["native_status"] == "unknown" and released["admitted"] is False


async def test_get_persists_unknown_snapshot_instead_of_showing_old_running(native):
    operation, _ = await prepare(native)
    await native.repository.response(operation.id, state(1, "running"))

    class QuerySocket(Socket):
        async def send_json(self, value):
            connection.respond({"id": value["id"], "result": state(2, "unknown")})

    connection = DeviceConnection(native.device["device_id"], QuerySocket())
    await connect(native, connection)
    first = await native.service.execute(
        {"action": "get", "operation_id": operation.id}, "query", native.source
    )
    # The query returns the persisted receipt at once; the device answer lands after it.
    assert first["status"] == "running"
    await native.service.drain()
    view = await native.service.execute(
        {"action": "get", "operation_id": operation.id}, "query-again", native.source
    )
    assert view["status"] == "unknown" and view["admitted"] is True
    original = await native.repository.operation(operation.id, native.actor)
    assert json.loads(original.result_json) == state(2, "unknown")


async def test_upgrade_replay_acknowledges_same_legacy_fact_without_rewriting_it(native):
    operation, _ = await prepare(native)
    replay = event("legacy-committed", operation=operation.id)
    legacy = {
        key: value for key, value in replay.items() if key not in {"sequence", "operation_state"}
    }
    async with native.repository.database.immediate_session() as writer:
        writer.add(
            AgentEventModel(
                device_id=native.device["device_id"],
                event_id=replay["event_id"],
                operation_id=operation.id,
                kind=replay["kind"],
                payload_json=encode(legacy),
                created_at=datetime.now(UTC),
            )
        )
    await native.repository.receive_events(native.device["device_id"], [replay])
    async with native.repository.database.read_session() as reader:
        original = await reader.scalar(select(AgentEventModel.payload_json))
    assert original == encode(legacy)
    replay["payload"] = {"text": "different actual fact"}
    with pytest.raises(ValueError, match="external_agent_event_conflict"):
        await native.repository.receive_events(native.device["device_id"], [replay])


async def test_device_offline_is_view_fact_and_old_socket_cannot_disconnect_replacement(case):
    env, operation = await owned(case)
    env.service = ExternalAgentService(case.db, case.tasks)
    await env.repository.response(operation.id, state(1, "running"))
    old = DeviceConnection(env.device["device_id"], Socket())
    await connect(env, old)
    replacement = DeviceConnection(env.device["device_id"], Socket())
    await connect(env, replacement)
    await env.service.disconnect(old)
    assert env.service.connections.get(env.device["device_id"]) is replacement
    await env.service.disconnect(replacement)
    await env.service.disconnect(replacement)
    original = await env.repository.operation(operation.id, env.actor)
    assert json.loads(original.result_json) == state(1, "running")
    view = await env.service._view(original)
    assert view["status"] == "unknown" and view["observation_error"] == "device_offline"
    assert view["last_confirmed"]["status"] == "running"
    async with case.db.read_session() as reader:
        messages = list(await reader.scalars(select(inputs.c.payload_json)))
    facts = [json.loads(json.loads(value)["text"])["event"] for value in messages]
    # Connecting is progress; going offline is the one wake fact.
    assert facts == ["device_offline"]
    main_inputs = await case.control.take_inputs("transport-facts")
    assert any("device_offline" in message.content for message in main_inputs)
    assert any(operation.id in message.content for message in main_inputs)
    await env.repository.response(operation.id, state(2, "completed", pending=False))
    view = await env.service._view(await env.repository.operation(operation.id, env.actor))
    assert view["pending"] is False and view["status"] == "succeeded"


@pytest.mark.parametrize("arrival", ["before", "during", "after"])
async def test_native_gap_and_recovery_reach_original_main_across_owned_wait(
    case, monkeypatch, arrival
):
    env, operation = await owned(case)
    control = case.control

    async def wait():
        result = json.loads(
            await control.execute(
                "wait",
                {"for": [{"run": operation.run_id}]},
                "wait-original-operation",
            )
        )
        assert result.get("ok") is not False
        await control.settle(pending_inputs=False)

    if arrival == "after":
        await wait()
    elif arrival == "during":
        prepare_wait = WorkWaitRepository.prepare_external_action
        registered = False

        async def register_while_event_prepared(repository, reader, work_id):
            nonlocal registered
            result = await prepare_wait(repository, reader, work_id)
            if not registered:
                registered = True
                await wait()
            return result

        monkeypatch.setattr(
            WorkWaitRepository, "prepare_external_action", register_while_event_prepared
        )
    gap = await push(env, operation, 1, "unknown", observation_error="native_stream_lost")
    if arrival == "before":
        await wait()
    await env.repository.receive_events(env.device["device_id"], [gap])
    await push(env, operation, 2, "running", stream_gap=True)
    current = await control.repository.get(operation.work_id)
    assert current["state"] == "queued"
    assert (await WorkWaitRepository(control.repository).describe(operation.work_id))[
        "status"
    ] == "delivered"
    control.current = current
    messages = await control.take_inputs("original-main-resume")
    native_messages = [message for message in messages if "external_agent_card" in message.content]
    # The lost stream is a wake fact; the recovery that follows is timeline progress.
    assert len(native_messages) == 1
    facts = [json.loads(message.content.split("\n", 1)[1]) for message in native_messages]
    assert [fact["status"] for fact in facts] == ["unknown"]
    assert all(operation.id in message.content for message in native_messages)
    assert all(
        execution_key(env.session, "native-turn") in message.content for message in native_messages
    )
