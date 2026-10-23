"""Real TLS HTTP/device WebSocket, original SQLite Work inputs and receipts."""

import asyncio
import json
import socket
import ssl
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import trustme
import uvicorn
from fastapi import FastAPI
from sqlalchemy import func, select, update
from tests.support.client_identity import client_identity_service
from tests.support.social_identity_cases import social_env
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.external_agents.db_models import AgentEventModel, AgentOperationModel
from yuki.external_agents.http import attach_external_agent_api
from yuki.external_agents.service import ExternalAgentService
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.sandbox.continuation_worker import SandboxContinuationWorker
from yuki.sandbox.db_models import SandboxTaskContinuationModel, SandboxTaskRunModel
from yuki.sandbox.task_repository import SandboxTaskRepository


class BridgeServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets)
        self.ready.set()


class NativeComputer:
    """An explicit native protocol peer; no model or QQ effects are involved."""

    local_id = "codex-profile-one"
    native_session = "thread-from-native-store"
    native_execution = "turn-from-native-owner"

    def __init__(self, bridge):
        self.bridge = bridge
        self.commands = []
        self.results = {}
        self.acknowledged = asyncio.Queue()
        self.drop_next = None
        self.reject_next = None
        self.socket = None
        self.reader = None
        self.ready = asyncio.Event()
        self.catalog_ready = asyncio.Event()
        self.sequence = 0
        self.sent_events = {}

    async def start(self):
        self.socket = await connect(
            self.bridge.websocket_url,
            ssl=self.bridge.ssl,
            proxy=None,
            additional_headers={"Authorization": f"Bearer {self.bridge.device['token']}"},
        )
        await self.socket.send(
            json.dumps(
                {
                    "type": "hello",
                    "protocol_version": 3,
                    "instances": [
                        {
                            "id": self.local_id,
                            "label": "Local Codex",
                            "driver": "codex",
                            "capabilities": [
                                "list",
                                "get",
                                "watch",
                                "prompt",
                                "steer",
                                "queue",
                                "respond",
                                "cancel",
                                "detach",
                            ],
                        }
                    ],
                }
            )
        )
        self.reader = asyncio.create_task(self.read())
        self.ready.clear()
        await self.socket.send(json.dumps({"type": "ping"}))
        await asyncio.wait_for(self.ready.wait(), 5)
        return self

    async def read(self):
        try:
            async for raw in self.socket:
                message = json.loads(raw)
                if message["type"] == "pong":
                    self.ready.set()
                    continue
                if message["type"] == "catalog_ack":
                    self.catalog_ready.set()
                    continue
                if message["type"] == "ack":
                    self.acknowledged.put_nowait(message["event_id"])
                    continue
                if message["type"] != "command":
                    continue
                self.commands.append(message)
                action = message["action"]
                if self.drop_next == action:
                    self.drop_next = None
                    await self.socket.close()
                    continue
                if self.reject_next == action:
                    self.reject_next = None
                    result = {
                        "error": "capability_unsupported",
                        "executed": False,
                        "pending": False,
                        "status": "failed",
                    }
                elif action == "list":
                    cursor = message.get("arguments", {}).get("cursor")
                    result = {
                        "sessions": [
                            {
                                "id": self.native_session + "-second"
                                if cursor == "opaque-next-page"
                                else self.native_session,
                                "title": "Existing work",
                            }
                        ],
                        "next_cursor": "opaque-next-page" if not cursor else None,
                    }
                elif action == "get":
                    result = self.results.get(
                        message.get("arguments", {}).get("operation_id"),
                        {
                            "session_id": self.native_session,
                            "execution_id": self.native_execution,
                            "status": "running",
                            "pending": True,
                        },
                    )
                    if message.get("arguments", {}).get("include_history"):
                        result = {**result, "history": [{"text": "Explicit original history"}]}
                elif action in {"prompt", "queue", "watch"}:
                    result = {
                        "session_id": self.native_session,
                        "execution_id": self.native_execution,
                        "status": "running",
                        "pending": True,
                        "admitted": True,
                    }
                    self.results[message["id"]] = result
                else:
                    result = {"status": "accepted", "pending": True, "admitted": True}
                await self.socket.send(
                    json.dumps(
                        {
                            "type": "response",
                            "id": message["id"],
                            "result": result,
                        }
                    )
                )
        except ConnectionClosed:
            pass

    async def event(self, operation_id, kind, payload, *, event_id=None):
        self.sequence += 1
        event = {
            "type": "event",
            "event_id": event_id or str(uuid4()),
            "instance_id": self.local_id,
            "operation_id": operation_id,
            "session_id": self.native_session,
            "execution_id": self.native_execution,
            "kind": kind,
            "payload": payload,
            "sequence": self.sequence,
            "operation_state": {
                **self.results.get(operation_id, {}),
                **payload,
                "session_id": self.native_session,
                "execution_id": self.native_execution,
                "pending": kind != "completed",
                "state_sequence": self.sequence,
            },
        }
        event = self.sent_events.setdefault(event["event_id"], event)
        await self.socket.send(json.dumps(event))
        async with asyncio.timeout(5):
            assert await self.acknowledged.get() == event["event_id"]
        return event

    async def close(self):
        if self.socket is not None:
            await self.socket.close()
        if self.reader is not None:
            await self.reader


@pytest.fixture
async def bridge(database, tmp_path):
    env = await social_env(database, tmp_path)
    chat = client_identity_service(database, env.router)
    code = await chat.issue_for_operator(env.person, str(uuid4()), str(uuid4()))
    chat_device = await chat.pair(code["code"], "Chat control")
    service = ExternalAgentService(database, SandboxTaskRepository(database))
    database.external_agents = service
    app = FastAPI()
    attach_external_agent_api(app, lambda: service)
    ca = trustme.CA()
    cert = ca.issue_cert("localhost", "127.0.0.1")
    cert.cert_chain_pems[0].write_to_path(tmp_path / "server.pem")
    cert.private_key_pem.write_to_path(tmp_path / "server.key")
    ca.cert_pem.write_to_path(tmp_path / "root-ca.pem")
    context = ssl.create_default_context()
    ca.configure_trust(context)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = BridgeServer(
        uvicorn.Config(
            app,
            log_level="critical",
            lifespan="off",
            timeout_graceful_shutdown=1,
            ssl_keyfile=str(tmp_path / "server.key"),
            ssl_certfile=str(tmp_path / "server.pem"),
        )
    )
    server.ready = asyncio.Event()
    running = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(server.ready.wait(), 5)
        async with httpx.AsyncClient(
            base_url=f"https://127.0.0.1:{port}",
            verify=context,
            trust_env=False,
        ) as http:
            grant = await chat.issue_for_operator(
                env.person,
                str(uuid4()),
                str(uuid4()),
                purpose="connector",
            )
            paired = await http.post(
                "/agents/v1/pair",
                json={
                    "code": grant["code"],
                    "device_label": "Windows native computer",
                },
            )
            assert paired.status_code == 200, paired.text
            device = paired.json()
            yield SimpleNamespace(
                service=service,
                database=database,
                env=env,
                chat=chat,
                http=http,
                device=device,
                chat_device=chat_device,
                ssl=context,
                ca_file=tmp_path / "root-ca.pem",
                websocket_url=f"wss://127.0.0.1:{port}/agents/v1/connect",
                headers={"Authorization": f"Bearer {chat_device['token']}"},
                source={"actor_person_id": env.person},
            )
    finally:
        await service.close()
        server.should_exit = True
        await asyncio.wait_for(running, 5)
        listener.close()


@asynccontextmanager
async def accepted_work(bridge):
    repository = WorkRepository(bridge.database)
    conversation = bridge.env.context.conversation_id
    lease = await repository.acquire(conversation, 1)
    async with bridge.database.read_session() as reader:
        event = await reader.scalar(select(ChatEventModel))
    source = {
        **bridge.source,
        "principal_kind": "person",
        "origin": "user_message",
        "generation": 1,
        "trigger_event_id": event.id,
        "bot_user_id": bridge.env.bot.self_id,
        "presence_id": bridge.env.presence,
    }

    async def valid():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "external-owner", source, valid)
    control.current = await repository.accept(
        lease,
        source_key=f"external-owner:{uuid4()}",
        source=source,
        goal="Delegate local work",
    )
    token = current_work_control.set(control)
    try:
        yield control
    finally:
        current_work_control.reset(token)
        await repository.release(lease)


async def directory(bridge):
    response = await bridge.http.get("/agents/v1/devices", headers=bridge.headers)
    assert response.status_code == 200, response.text
    instance = response.json()["instances"][0]["id"]
    result = await bridge.service.execute(
        {"action": "list", "instance_id": instance},
        str(uuid4()),
        bridge.source,
    )
    return instance, result["sessions"][0]["id"]


async def test_tls_directory_and_later_catalog_keep_internal_ids_and_original_connection(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        assert instance != computer.local_id and session_id != computer.native_session
        UUID(instance)
        UUID(session_id)
        result = await bridge.service.execute(
            {"action": "get", "session_id": session_id},
            str(uuid4()),
            bridge.source,
        )
        assert result["session_id"] == session_id
        assert result["execution_id"] != computer.native_execution
        UUID(result["execution_id"])
        async with bridge.database.read_session() as reader:
            assert await reader.scalar(select(func.count()).select_from(AgentOperationModel)) == 0
            assert await reader.scalar(select(func.count()).select_from(SandboxTaskRunModel)) == 0
        assert [item["action"] for item in computer.commands] == ["list", "get"]
        connection = bridge.service.connections.get(bridge.device["device_id"])
        rows = [
            {
                "id": computer.local_id,
                "label": "Original",
                "driver": "codex",
                "capabilities": ["list", "get"],
                "status": "available",
            },
            {
                "id": "later-instance",
                "label": "Starting",
                "driver": "codex",
                "capabilities": [],
                "status": "unavailable",
            },
        ]
        for available in (False, True):
            if available:
                rows[1] = {**rows[1], "capabilities": ["list", "get"], "status": "available"}
            computer.catalog_ready.clear()
            await computer.socket.send(json.dumps({"type": "catalog", "instances": rows}))
            await asyncio.wait_for(computer.catalog_ready.wait(), 5)
            catalog = await bridge.service.catalog(bridge.env.person)
            later = next(item for item in catalog["instances"] if item["label"] == "Starting")
            assert later["online"] is available
            assert connection is bridge.service.connections.get(bridge.device["device_id"])
            if not available:
                identity = later["id"]
            else:
                assert later["id"] == identity
                listed = await bridge.service.execute(
                    {"action": "list", "instance_id": identity}, str(uuid4()), bridge.source
                )
                assert listed["sessions"]
                assert computer.commands[-1]["instance_id"] == "later-instance"
    finally:
        await computer.close()


@pytest.mark.parametrize("action", ["prompt", "queue", "watch"])
async def test_mutating_work_relationship_requires_real_work_before_native_dispatch(bridge, action):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        with pytest.raises(ValueError, match="accept_work_before_execution"):
            await bridge.service.execute(
                {
                    "action": action,
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "Isolated task",
                },
                str(uuid4()),
                bridge.source,
            )
        assert [item["action"] for item in computer.commands] == ["list"]
    finally:
        await computer.close()


async def test_progress_question_completion_commit_before_ack_into_original_work(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            operation_id = str(uuid4())
            result = await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "Edit only the isolated fixture",
                },
                operation_id,
                bridge.source,
            )
            assert result["run_id"] and result["operation_id"] == operation_id
            progress = await computer.event(operation_id, "progress", {"text": "Testing started"})
            await computer.event(
                operation_id,
                "permission",
                {
                    "native_request_id": "native-question-one",
                    "options": [{"id": "allow", "label": "Allow"}],
                },
            )
            # Replaying the same original event is ACKed without another private input.
            await computer.event(
                operation_id, "progress", progress["payload"], event_id=progress["event_id"]
            )
            async with bridge.database.read_session() as reader:
                stored = list(
                    await reader.scalars(select(AgentEventModel).order_by(AgentEventModel.id))
                )
                private = list(
                    (
                        await reader.execute(
                            select(inputs).where(
                                inputs.c.work_id == control.current["id"],
                                inputs.c.kind == "control",
                            )
                        )
                    ).mappings()
                )
            assert len(stored) == len(private) == 2
            assert all(item["work_id"] == control.current["id"] for item in private)
            question = json.loads(private[1]["payload_json"])
            permission_id = json.loads(question["text"])["permission_id"]
            assert permission_id != "native-question-one"
            await bridge.service.execute(
                {
                    "action": "respond",
                    "operation_id": operation_id,
                    "permission_id": permission_id,
                    "option_id": "allow",
                },
                str(uuid4()),
                bridge.source,
            )
            answered = computer.commands[-1]
            assert answered["arguments"]["permission_id"] == "native-question-one"
            await computer.event(operation_id, "completed", {"status": "completed", "text": "Done"})
            async with bridge.database.read_session() as reader:
                task = await reader.get(SandboxTaskRunModel, operation_id)
                continuation = await reader.get(SandboxTaskContinuationModel, operation_id)
                assert task.status == "completed" and continuation.state == "ready"
                assert json.loads(task.completion_json)["run_id"] == result["run_id"]
            await SandboxContinuationWorker(control.repository).drain_once()
            async with bridge.database.read_session() as reader:
                completion = (
                    (
                        await reader.execute(
                            select(inputs).where(
                                inputs.c.work_id == control.current["id"],
                                inputs.c.kind == "completion",
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
            assert (
                json.loads(json.loads(completion["payload_json"])["text"])["run_id"]
                == result["run_id"]
            )
            response = await bridge.http.get(
                f"/agents/v1/operations/{operation_id}",
                headers=bridge.headers,
            )
            assert response.status_code == 200 and response.json()["status"] == "succeeded"
    finally:
        await computer.close()


async def test_watch_is_supervision_and_detach_does_not_cancel_native_session(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            operation_id = str(uuid4())
            result = await bridge.service.execute(
                {
                    "action": "watch",
                    "instance_id": instance,
                    "session_id": session_id,
                },
                operation_id,
                bridge.source,
            )
            assert result["ownership"] == "observe"
            assert not await control.repository.has_owned_execution(
                control.lease, control.current["id"]
            )
            await bridge.service.execute(
                {"action": "detach", "operation_id": operation_id}, str(uuid4()), bridge.source
            )
            assert [item["action"] for item in computer.commands] == ["list", "watch", "detach"]
            assert not any(item["action"] in {"cancel", "close"} for item in computer.commands)
    finally:
        await computer.close()


async def test_revoked_long_socket_has_no_new_native_effect(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with bridge.database.immediate_session() as writer:
            from datetime import UTC, datetime

            (
                await writer.get(ClientDeviceModel, bridge.device["device_id"])
            ).revoked_at = datetime.now(UTC)
        async with accepted_work(bridge):
            with pytest.raises(ClientError, match="unauthenticated"):
                await bridge.service.execute(
                    {
                        "action": "prompt",
                        "instance_id": instance,
                        "session_id": session_id,
                        "prompt": "Must not dispatch",
                    },
                    str(uuid4()),
                    bridge.source,
                )
        assert [item["action"] for item in computer.commands] == ["list"]
        async with bridge.database.read_session() as reader:
            assert await reader.scalar(select(func.count()).select_from(AgentOperationModel)) == 0
    finally:
        await computer.close()


async def test_lost_native_acceptance_is_unknown_and_same_operation_never_resubmits(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge):
            operation_id = str(uuid4())
            arguments = {
                "action": "prompt",
                "instance_id": instance,
                "session_id": session_id,
                "prompt": "One native execution only",
            }
            computer.drop_next = "prompt"
            result = await bridge.service.execute(arguments, operation_id, bridge.source)
            assert result["uncertain"] is True
            await computer.close()
            await computer.start()
            recovered = await bridge.service.execute(arguments, operation_id, bridge.source)
            assert recovered["operation_id"] == operation_id and recovered["uncertain"] is True
            assert len([item for item in computer.commands if item["action"] == "prompt"]) == 1
            async with bridge.database.read_session() as reader:
                assert (
                    await reader.scalar(select(func.count()).select_from(AgentOperationModel)) == 1
                )
                assert (
                    await reader.scalar(select(func.count()).select_from(SandboxTaskRunModel)) == 1
                )
    finally:
        await computer.close()


async def test_late_progress_never_revives_work_but_terminal_fact_is_preserved(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            operation_id = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "Original accepted task",
                },
                operation_id,
                bridge.source,
            )
            async with bridge.database.immediate_session() as writer:
                await writer.execute(
                    update(work).where(work.c.id == control.current["id"]).values(state="cancelled")
                )
            await computer.event(operation_id, "progress", {"text": "Already happened"})
            await computer.event(operation_id, "completed", {"status": "completed"})
            await SandboxContinuationWorker(control.repository).drain_once()
            async with bridge.database.read_session() as reader:
                assert (
                    await reader.scalar(
                        select(work.c.state).where(
                            work.c.id == control.current["id"],
                        )
                    )
                    == "cancelled"
                )
                task = await reader.get(SandboxTaskRunModel, operation_id)
                assert task.status == "completed"
                assert json.loads(task.completion_json)["status"] == "succeeded"
                assert (
                    await reader.scalar(
                        select(func.count())
                        .select_from(inputs)
                        .where(
                            inputs.c.work_id == control.current["id"],
                        )
                    )
                    == 0
                )
    finally:
        await computer.close()


async def test_same_native_session_on_two_computers_has_distinct_internal_resources(bridge):
    first = await NativeComputer(bridge).start()
    second = None
    try:
        first_instance, first_session = await directory(bridge)
        grant = await bridge.chat.issue_for_operator(
            bridge.env.person,
            str(uuid4()),
            str(uuid4()),
            purpose="connector",
        )
        paired = await bridge.http.post(
            "/agents/v1/pair",
            json={
                "code": grant["code"],
                "device_label": "Second computer",
            },
        )
        assert paired.status_code == 200
        other = SimpleNamespace(**{**vars(bridge), "device": paired.json()})
        second = await NativeComputer(other).start()
        catalog = await bridge.service.execute(
            {"action": "list", "device_id": other.device["device_id"]},
            str(uuid4()),
            bridge.source,
        )
        second_instance = catalog["instances"][0]["id"]
        sessions = await bridge.service.execute(
            {"action": "list", "instance_id": second_instance},
            str(uuid4()),
            bridge.source,
        )
        second_session = sessions["sessions"][0]["id"]
        assert first_instance != second_instance and first_session != second_session
        assert first.commands[-1]["instance_id"] == second.commands[-1]["instance_id"]
    finally:
        await first.close()
        if second is not None:
            await second.close()


async def test_conflicting_operation_and_execution_never_redirects_cancel_to_another_device(bridge):
    first = await NativeComputer(bridge).start()
    second = None
    try:
        first_instance, first_session = await directory(bridge)
        grant = await bridge.chat.issue_for_operator(
            bridge.env.person,
            str(uuid4()),
            str(uuid4()),
            purpose="connector",
        )
        paired = await bridge.http.post(
            "/agents/v1/pair",
            json={
                "code": grant["code"],
                "device_label": "Other native owner",
            },
        )
        assert paired.status_code == 200
        other = SimpleNamespace(**{**vars(bridge), "device": paired.json()})
        second = await NativeComputer(other).start()
        catalog = await bridge.service.execute(
            {"action": "list", "device_id": other.device["device_id"]},
            str(uuid4()),
            bridge.source,
        )
        second_instance = catalog["instances"][0]["id"]
        sessions = await bridge.service.execute(
            {"action": "list", "instance_id": second_instance},
            str(uuid4()),
            bridge.source,
        )
        second_execution = await bridge.service.execute(
            {"action": "get", "session_id": sessions["sessions"][0]["id"]},
            str(uuid4()),
            bridge.source,
        )
        async with accepted_work(bridge):
            original = await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": first_instance,
                    "session_id": first_session,
                    "prompt": "First native owner only",
                },
                str(uuid4()),
                bridge.source,
            )
            with pytest.raises(ValueError, match=r"mismatch|conflict"):
                await bridge.service.execute(
                    {
                        "action": "cancel",
                        "operation_id": original["operation_id"],
                        "execution_id": second_execution["execution_id"],
                    },
                    str(uuid4()),
                    bridge.source,
                )
        assert not any(command["action"] == "cancel" for command in second.commands)
    finally:
        await first.close()
        if second is not None:
            await second.close()


async def test_conflicting_duplicate_event_is_not_acknowledged_or_applied(bridge, monkeypatch):
    disconnected = asyncio.Event()
    disconnect = ExternalAgentService.disconnect

    async def finish_disconnect(service, connection):
        await disconnect(service, connection)
        disconnected.set()

    monkeypatch.setattr(ExternalAgentService, "disconnect", finish_disconnect)
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            operation_id = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "Original task",
                },
                operation_id,
                bridge.source,
            )
            original = await computer.event(operation_id, "progress", {"text": "Original fact"})
            await computer.socket.send(
                json.dumps({**original, "payload": {"text": "Changed fact"}})
            )
            await asyncio.wait_for(computer.reader, 5)
            await asyncio.wait_for(disconnected.wait(), 5)
            assert computer.acknowledged.empty()
            async with bridge.database.read_session() as reader:
                events = list(await reader.scalars(select(AgentEventModel)))
                assert len(events) == 1
                assert json.loads(events[0].payload_json)["payload"]["text"] == "Original fact"
                private = list(
                    (
                        await reader.execute(
                            select(inputs).where(
                                inputs.c.work_id == control.current["id"],
                                inputs.c.kind == "control",
                            )
                        )
                    ).mappings()
                )
            original_inputs = [
                item
                for item in private
                if item["source_key"] == f"external-agent:{original['event_id']}:{operation_id}"
            ]
            assert len(original_inputs) == 1
            facts = [json.loads(json.loads(item["payload_json"])["text"]) for item in private]
            event_facts = [fact for fact in facts if fact["kind"] == "external_agent_event"]
            assert len(event_facts) == 1
            assert event_facts[0]["payload"] == {"text": "Original fact"}
            assert sorted((fact["kind"], fact["event"]) for fact in facts) == [
                ("external_agent_event", "progress"),
                ("external_agent_transport", "device_offline"),
            ]
            assert all("Changed fact" not in item["payload_json"] for item in private)
    finally:
        await computer.close()


async def test_unknown_operation_http_query_is_finite_not_found(bridge):
    response = await bridge.http.get(
        f"/agents/v1/operations/{uuid4()}",
        headers=bridge.headers,
    )
    assert response.status_code == 404
    assert response.json()["problem"]["code"] == "not_found"


async def test_refused_detach_does_not_publish_success_or_end_original_supervision(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge):
            operation_id = str(uuid4())
            await bridge.service.execute(
                {"action": "watch", "instance_id": instance, "session_id": session_id},
                operation_id,
                bridge.source,
            )
            computer.reject_next = "detach"
            result = await bridge.service.execute(
                {"action": "detach", "operation_id": operation_id},
                str(uuid4()),
                bridge.source,
            )
            assert result["admitted"] is False and result["error"] == "capability_unsupported"
            async with bridge.database.read_session() as reader:
                original = await reader.get(AgentOperationModel, operation_id)
                task = await reader.get(SandboxTaskRunModel, operation_id)
                assert original.detached_at is None and task.status == "waiting"
            assert not any(command["action"] == "cancel" for command in computer.commands)
    finally:
        await computer.close()


async def test_native_pagination_and_explicit_history_do_not_share_server_event_cursor(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        catalog = await bridge.http.get("/agents/v1/devices", headers=bridge.headers)
        instance = catalog.json()["instances"][0]["id"]
        first = await bridge.service.execute(
            {"action": "list", "instance_id": instance},
            str(uuid4()),
            bridge.source,
        )
        second = await bridge.service.execute(
            {"action": "list", "instance_id": instance, "native_cursor": first["next_cursor"]},
            str(uuid4()),
            bridge.source,
        )
        assert first["sessions"][0]["id"] != second["sessions"][0]["id"]
        assert computer.commands[-1]["arguments"]["cursor"] == "opaque-next-page"
        history = await bridge.service.execute(
            {
                "action": "get",
                "session_id": first["sessions"][0]["id"],
                "include_history": True,
                "native_cursor": "opaque-history-page",
            },
            str(uuid4()),
            bridge.source,
        )
        assert history["history"] == [{"text": "Explicit original history"}]
        assert computer.commands[-1]["arguments"]["include_history"] is True
        assert computer.commands[-1]["arguments"]["cursor"] == "opaque-history-page"
    finally:
        await computer.close()


async def test_event_waiting_for_real_sqlite_commit_does_not_block_rpc_responses(
    bridge, monkeypatch
):
    computer = await NativeComputer(bridge).start()
    sending = None
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge):
            operation = str(uuid4())
            await bridge.service.execute(
                {"action": "watch", "instance_id": instance, "session_id": session_id},
                operation,
                bridge.source,
            )
            entered = asyncio.Event()
            receive = bridge.service.receive_events

            async def contended(device_id, events):
                entered.set()
                await receive(device_id, events)

            monkeypatch.setattr(bridge.service, "receive_events", contended)
            # Test-only writer barrier: the socket reader must remain independent
            # of the event worker queued behind this real SQLite transaction.
            async with bridge.database.immediate_session():
                sending = asyncio.create_task(
                    computer.event(operation, "progress", {"text": "new"})
                )
                await asyncio.wait_for(entered.wait(), 5)
                connection = bridge.service.connections.get(bridge.device["device_id"])
                response = await asyncio.wait_for(
                    connection.request(
                        {
                            "type": "command",
                            "id": str(uuid4()),
                            "action": "get",
                            "instance_id": computer.local_id,
                            "arguments": {},
                        }
                    ),
                    5,
                )
                assert response["session_id"] == computer.native_session
                assert not sending.done()
            original = await asyncio.wait_for(sending, 5)
            async with bridge.database.read_session() as reader:
                stored = await reader.scalar(
                    select(AgentEventModel).where(AgentEventModel.event_id == original["event_id"])
                )
                assert stored.operation_id == operation
    finally:
        if sending is not None:
            await asyncio.gather(sending, return_exceptions=True)
        await computer.close()
