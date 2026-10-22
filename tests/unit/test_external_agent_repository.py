"""Internal resource identity, original operations, and durable important facts."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from tests.support.client_identity import client_identity_service
from tests.unit.test_external_agent_work import case as work_case

from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.external_agents.db_models import AgentEventModel, AgentOperationModel
from yuki.external_agents.identity import ConnectorIdentityService
from yuki.external_agents.repository import (
    AgentRepository,
    execution_key,
    instance_key,
    session_key,
)
from yuki.external_agents.service import ExternalAgentService
from yuki.external_agents.transport import DeviceConnection
from yuki.identity.canonical_repository import find_identity_binding
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.sandbox.db_models import SandboxTaskContinuationModel, SandboxTaskRunModel
from yuki.sandbox.task_repository import SandboxTaskRepository

case = work_case


async def register_device(database, person_id, *, label="computer"):
    identity = client_identity_service(database)
    code = await identity.issue_for_operator(
        person_id, str(uuid4()), str(uuid4()), purpose="connector"
    )
    connector = ConnectorIdentityService(database)
    device = await connector.pair(code["code"], label)
    repository = AgentRepository(database)
    await repository.register(
        device["device_id"],
        [
            {
                "id": "profile",
                "label": "same name",
                "driver": "codex",
                "capabilities": ["get", "steer"],
            }
        ],
    )
    return SimpleNamespace(
        device=device,
        actor=person_id,
        repository=repository,
        instance=instance_key(device["device_id"], "profile"),
    )


@pytest_asyncio.fixture
async def native(database):
    async with database.sessions() as session:
        owner = await find_identity_binding(session, "1001")
        other = await find_identity_binding(session, "1002")
    env = await register_device(database, owner.person_id)
    env.other_actor = other.person_id
    mapping = await env.repository.remember_sessions(env.instance, env.actor, ["native-thread"])
    env.session = mapping["native-thread"]
    env.source = {"actor_person_id": env.actor}
    env.service = ExternalAgentService(database, SandboxTaskRepository(database))
    return env


async def prepare(env, identity="original-operation", **changes):
    parameters = {
        "identity": identity,
        "instance_id": env.instance,
        "actor": env.actor,
        "action": "steer",
        "arguments": {"action": "steer", "text": "keep working"},
        "command": {"id": identity, "action": "steer"},
        "source": env.source,
        "session_id": env.session,
        "native_execution_id": "native-turn",
        "lease": None,
    }
    parameters.update(changes)
    return await env.repository.prepare(**parameters)


def event(identity="event-one", *, operation="original-operation", **changes):
    result = {
        "event_id": identity,
        "instance_id": "profile",
        "operation_id": operation,
        "session_id": "native-thread",
        "execution_id": "native-turn",
        "kind": "progress",
        "payload": {"message": "tests started"},
    }
    result.update(changes)
    return result


async def test_device_profile_and_native_ids_never_merge_across_resources(database, native):
    same_person = await register_device(database, native.actor, label="second computer")
    foreign = await register_device(database, native.other_actor)
    second = await same_person.repository.remember_sessions(
        same_person.instance, native.actor, ["native-thread"]
    )
    third = await foreign.repository.remember_sessions(
        foreign.instance, foreign.actor, ["native-thread"]
    )
    ids = {native.session, second["native-thread"], third["native-thread"]}
    assert len(ids) == 3
    assert native.instance != same_person.instance != foreign.instance
    own = await native.service.execute({"action": "list"}, "read-directory", native.source)
    assert {row["id"] for row in own["instances"]} == {native.instance, same_person.instance}
    with pytest.raises(ValueError, match="external_agent_not_found"):
        await native.repository.native_session(third["native-thread"], native.actor)
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(work)) == 0
        assert await session.scalar(select(func.count()).select_from(SandboxTaskRunModel)) == 0


async def test_original_operation_key_preserves_unknown_and_rejects_changed_intent(native):
    original, created = await prepare(native)
    assert created and original.result_json is None and original.run_id is None
    same, created = await prepare(native)
    assert same.id == original.id and not created
    with pytest.raises(ValueError, match="external_agent_request_conflict"):
        await prepare(native, arguments={"action": "steer", "text": "different instruction"})
    view = await native.service.execute(
        {"action": "get", "operation_id": original.id}, "read-original", native.source
    )
    assert view["status"] == "unknown" and view["uncertain"] and view["pending"]
    assert not view["device_online"]


async def test_revoke_blocks_native_access_but_preserves_original_receipts(database, native):
    operation, _ = await prepare(native)
    await native.repository.response(
        operation.id,
        {
            "status": "completed",
            "pending": False,
            "session_id": "native-thread",
            "execution_id": "native-turn",
        },
    )
    async with database.immediate_session() as session:
        (
            await session.get(ClientDeviceModel, native.device["device_id"])
        ).revoked_at = datetime.now(UTC)
    assert (await native.repository.operation(operation.id, native.actor)).id == operation.id
    view = await native.service.execute(
        {"action": "get", "operation_id": operation.id}, "read-old-receipt", native.source
    )
    assert view["status"] == "completed" and not view["pending"]
    with pytest.raises(ClientError, match="unauthenticated"):
        await native.service.execute(
            {"action": "get", "session_id": native.session}, "new-native-read", native.source
        )
    assert (await native.service.catalog(native.actor))["devices"] == []


async def test_event_replay_deduplicates_and_changed_payload_or_wrong_owner_is_rejected(
    database, native
):
    await prepare(native)
    progress = event()
    await native.repository.receive_events(native.device["device_id"], [progress, progress])
    assert len(await native.repository.event_page("original-operation", 0)) == 1
    with pytest.raises(ValueError, match="external_agent_event_conflict"):
        await native.repository.receive_events(
            native.device["device_id"], [{**progress, "payload": {"message": "different fact"}}]
        )
    foreign = await register_device(database, native.other_actor)
    with pytest.raises(ValueError, match="external_agent_event_mismatch"):
        await native.repository.receive_events(foreign.device["device_id"], [event("cross-owner")])
    await native.repository.receive_events(
        native.device["device_id"], [event("token-delta", kind="delta")]
    )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentEventModel)) == 1


async def test_explicit_unknown_operation_is_not_acknowledged_or_orphaned(database, native):
    with pytest.raises(ValueError, match=r"external_agent.*"):
        await native.repository.receive_events(
            native.device["device_id"], [event(operation="missing-original-operation")]
        )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentEventModel)) == 0


@pytest.mark.parametrize(
    "result",
    [
        {"session_id": "another-thread", "execution_id": "native-turn"},
        {"session_id": "native-thread", "execution_id": "another-turn"},
    ],
)
async def test_response_cannot_rebind_an_original_operation_to_another_native_resource(
    native, result
):
    operation, _ = await prepare(native)
    with pytest.raises(ValueError, match=r"external_agent.*"):
        await native.repository.response(
            operation.id, {**result, "pending": True, "status": "running"}
        )
    original = await native.repository.operation(operation.id, native.actor)
    assert original.session_id == native.session and original.native_execution_id == "native-turn"


async def test_permission_uses_internal_event_id_and_resolves_exact_native_question(native):
    operation, _ = await prepare(native)
    await native.repository.receive_events(
        native.device["device_id"],
        [
            event(
                kind="permission",
                payload={"native_request_id": "native-question", "options": ["allow", "deny"]},
            )
        ],
    )
    page = await native.repository.event_page(operation.id, 0)
    permission_id = page[0]["permission_id"]
    assert permission_id != "native-question" and permission_id == str(page[0]["cursor"])
    commands = []

    class Socket:
        async def send_json(self, command):
            commands.append(command)
            connection.respond(
                {"id": command["id"], "result": {"pending": False, "status": "responded"}}
            )

        async def close(self, code=1000, reason=None):
            pass

    connection = DeviceConnection(native.device["device_id"], Socket())
    await native.service.connections.attach(connection)
    with pytest.raises(ValueError, match="permission_id_required"):
        await native.service.execute(
            {
                "action": "respond",
                "operation_id": operation.id,
                "permission_id": "native-question",
                "decision": "allow",
            },
            "bad-native-id",
            native.source,
        )
    assert commands == []
    result = await native.service.execute(
        {
            "action": "respond",
            "operation_id": operation.id,
            "permission_id": permission_id,
            "decision": "allow",
        },
        "answer-original-question",
        native.source,
    )
    assert commands[0]["arguments"]["permission_id"] == "native-question"
    assert commands[0]["arguments"]["native_request_id"] == "native-question"
    assert result["run_id"] is None


async def test_work_feedback_and_native_execution_keep_distinct_original_ids(database, case):
    env = await register_device(database, case.source["actor_person_id"])
    env.source = {
        **case.source,
        "work_id": case.control.current["id"],
        "conversation_id": case.control.lease.conversation_id,
    }
    env.session = (
        await env.repository.remember_sessions(env.instance, env.actor, ["native-thread"])
    )["native-thread"]
    env.service = ExternalAgentService(database, case.tasks)
    operation, _ = await prepare(
        env,
        action="prompt",
        arguments={"action": "prompt", "prompt": "run tests"},
        command={"id": "original-operation", "action": "prompt"},
        lease=case.control.lease,
    )
    progress = event()
    permission = event(
        "permission-event", kind="permission", payload={"native_request_id": "native-question"}
    )
    await env.repository.receive_events(env.device["device_id"], [progress, permission, progress])
    async with database.sessions() as session:
        messages = list(
            await session.scalars(
                select(inputs.c.payload_json).where(inputs.c.work_id == operation.work_id)
            )
        )
    assert len(messages) == 2
    payloads = [json.loads(json.loads(item)["text"]) for item in messages]
    question = next(item for item in payloads if item["event"] == "permission")
    internal_question = next(
        row
        for row in await env.repository.event_page(operation.id, 0)
        if row["kind"] == "permission"
    )
    assert question["permission_id"] == internal_question["permission_id"]
    terminal = event(
        "terminal-event", kind="completed", payload={"status": "completed", "text": "tests passed"}
    )
    await env.repository.receive_events(env.device["device_id"], [terminal, terminal])
    view = await env.service.execute(
        {"action": "get", "operation_id": operation.id}, "read-final", env.source
    )
    assert view["run_id"] == operation.run_id
    assert view["execution_id"] == execution_key(env.session, "native-turn")
    assert view["execution_id"] != view["run_id"]
    assert view["session_id"] == session_key(env.instance, "native-thread")
    assert view["native_reference"]["execution_id"] == "native-turn"
    async with database.sessions() as session:
        task = await session.get(SandboxTaskRunModel, operation.id)
        continuation = await session.get(SandboxTaskContinuationModel, operation.id)
        assert task.status == "completed" and continuation.state == "ready"
        assert json.loads(task.completion_json)["run_id"] == operation.run_id


async def test_dispatch_disconnect_keeps_original_operation_unknown_without_new_effect(
    database, native
):
    commands = []

    class Socket:
        async def send_json(self, command):
            commands.append(command)
            connection.disconnected()

        async def close(self, code=1000, reason=None):
            pass

    connection = DeviceConnection(native.device["device_id"], Socket())
    await native.service.connections.attach(connection)
    arguments = {
        "action": "steer",
        "instance_id": native.instance,
        "execution_id": await native.repository.remember_execution(native.session, "native-turn"),
        "prompt": "keep going",
    }
    result = await native.service.execute(arguments, "uncertain-original", native.source)
    assert result["pending"] and result["uncertain"] and result["status"] == "unknown"
    replay = await native.service.execute(arguments, "uncertain-original", native.source)
    assert replay["operation_id"] == result["operation_id"]
    assert len(commands) == 1
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentOperationModel)) == 1
        assert await session.scalar(select(func.count()).select_from(work)) == 0
