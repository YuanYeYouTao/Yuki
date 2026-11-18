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
from yuki.external_agents.errors import ExternalAgentArgumentError
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
        # Commands the native owner accepts but never answers until the test does.
        self.hold = set()
        self.held = {}
        self.fail = {}
        self.not_found = set()
        self.capabilities = [
            "list",
            "get",
            "watch",
            "prompt",
            "steer",
            "queue",
            "respond",
            "cancel",
            "detach",
        ]
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
                    "protocol_version": 4,
                    "instances": [
                        {
                            "id": self.local_id,
                            "label": "Local Codex",
                            "driver": "codex",
                            "capabilities": self.capabilities,
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
                if action in self.hold:
                    self.held[message["id"]] = message
                    continue
                if action in self.fail:
                    # The connector's protocol-level refusal of this command.
                    await self.socket.send(
                        json.dumps(
                            {"type": "response", "id": message["id"], "error": self.fail[action]}
                        )
                    )
                    continue
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
                elif (
                    action == "get"
                    and message.get("arguments", {}).get("operation_id") in self.not_found
                ):
                    result = {"status": "not_found"}
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

    async def answer(self, command_id, result):
        await self.socket.send(json.dumps({"type": "response", "id": command_id, "result": result}))

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


async def test_tls_directory_and_later_catalog_keep_internal_ids_and_original_connection(
    bridge, monkeypatch, tmp_path
):
    computer = await NativeComputer(bridge).start()
    replacement = None
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
        await bridge.service.execute(
            {"action": "cancel", "execution_id": result["execution_id"]},
            str(uuid4()),
            bridge.source,
        )
        # Controls are written by the service after the tool returned.
        await settled(bridge)
        assert computer.commands[-1]["action"] == "cancel"
        assert "operation_id" not in computer.commands[-1]["arguments"]
        connection = bridge.service.connections.get(bridge.device["device_id"])
        async with accepted_work(bridge):
            operation_id = str(uuid4())
            operation = await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "Keep the original receipt",
                },
                operation_id,
                bridge.source,
            )
            event = await computer.event(
                operation_id, "completed", {"status": "completed", "text": "Original result"}
            )
            computer.native_execution = "second-original-native-turn"
            pending_id = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "Keep the pending original owner",
                },
                pending_id,
                bridge.source,
            )
            # The native turn is known once the device answer is recorded, not at return.
            await settled(bridge)
            pending_operation = await bridge.service.execute(
                {"action": "get", "operation_id": pending_id}, str(uuid4()), bridge.source
            )
            assert pending_operation["execution_id"]
            await bridge.service.execute(
                {
                    "action": "watch",
                    "instance_id": instance,
                    "session_id": session_id,
                    "execution_id": pending_operation["execution_id"],
                },
                str(uuid4()),
                bridge.source,
            )
            computer.reject_next = "prompt"
            rejected_id = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "execution_id": pending_operation["execution_id"],
                    "prompt": "Rejected before dispatch",
                },
                rejected_id,
                bridge.source,
            )
            await settled(bridge)
            rejected = await bridge.service.execute(
                {"action": "get", "operation_id": rejected_id}, str(uuid4()), bridge.source
            )
            assert rejected["executed"] is False and rejected["pending"] is False
        execution_id = pending_operation["execution_id"]
        execution_control_id = str(uuid4())
        await bridge.service.execute(
            {"action": "cancel", "execution_id": execution_id}, execution_control_id, bridge.source
        )
        await settled(bridge)
        assert computer.commands[-1]["arguments"]["operation_id"] == pending_id
        assert computer.commands[-1]["execution_id"] == computer.native_execution

        # A second actual prompt/queue claim is ambiguous; a watch is not.
        async with accepted_work(bridge):
            await bridge.service.execute(
                {
                    "action": "queue",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "Queued",
                },
                str(uuid4()),
                bridge.source,
            )
            await settled(bridge)
        before = len(computer.commands)
        with pytest.raises(ExternalAgentArgumentError, match="external_agent_execution_ambiguous"):
            await bridge.service.execute(
                {"action": "steer", "execution_id": execution_id, "prompt": "Do not dispatch"},
                str(uuid4()),
                bridge.source,
            )
        assert len(computer.commands) == before
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

        # A complete snapshot removes only the absent profile. Unavailable B
        # is still configured and the original operation remains readable.
        remaining = {**rows[1], "capabilities": [], "status": "unavailable"}
        computer.catalog_ready.clear()
        await computer.socket.send(json.dumps({"type": "catalog", "instances": [remaining]}))
        await asyncio.wait_for(computer.catalog_ready.wait(), 5)
        catalog = (await bridge.http.get("/agents/v1/devices", headers=bridge.headers)).json()
        assert [item["id"] for item in catalog["instances"]] == [identity]
        assert not catalog["instances"][0]["online"]
        assert catalog["devices"][0]["id"] == bridge.device["device_id"]
        receipt = await bridge.http.get(
            f"/agents/v1/operations/{operation_id}", headers=bridge.headers
        )
        assert receipt.status_code == 200
        assert receipt.json()["instance_id"] == instance
        assert receipt.json()["status"] == "succeeded"
        operations = (await bridge.http.get("/agents/v1/operations", headers=bridge.headers)).json()
        pending = next(item for item in operations["items"] if item["operation_id"] == pending_id)
        assert pending["status"] == "unknown" and pending["uncertain"]
        assert pending["last_confirmed"]["status"] == "running"
        pending_read = await bridge.http.get(
            f"/agents/v1/operations/{pending_id}", headers=bridge.headers
        )
        assert pending_read.status_code == 200
        assert pending_read.json()["status"] == "unknown"
        assert computer.commands[-1]["action"] == "get"
        assert computer.commands[-1]["arguments"]["operation_id"] == pending_id

        # Removing configuration does not revoke this computer. An explicit
        # control still targets the original operation, but a new task cannot
        # select the removed instance.
        control_id = str(uuid4())
        control = await bridge.service.execute(
            {"action": "cancel", "operation_id": pending_id}, control_id, bridge.source
        )
        await settled(bridge)
        assert control["operation_id"] == control_id
        assert computer.commands[-1]["action"] == "cancel"
        assert computer.commands[-1]["arguments"]["operation_id"] == pending_id
        before = len(computer.commands)
        async with accepted_work(bridge):
            blocked = await bridge.service.execute(
                {"action": "prompt", "instance_id": instance, "prompt": "Do not dispatch"},
                str(uuid4()),
                bridge.source,
            )
        assert blocked["executed"] is False and len(computer.commands) == before
        late = await computer.event(pending_id, "completed", {"status": "completed"})
        completed = await bridge.http.get(
            f"/agents/v1/operations/{pending_id}", headers=bridge.headers
        )
        assert completed.json()["status"] == "succeeded"
        assert any(item["event_id"] == late["event_id"] for item in completed.json()["events"])

        # The old connection has already received a catalog when a new one
        # takes ownership. Its delayed catalog must not restore the old entry.
        entered, release = asyncio.Event(), asyncio.Event()
        register = bridge.service.repository.register

        async def delayed_register(device_id, catalog, **kwargs):
            if catalog and catalog[0].get("label") == "Stale catalog":
                entered.set()
                await release.wait()
            return await register(device_id, catalog, **kwargs)

        monkeypatch.setattr(bridge.service.repository, "register", delayed_register)
        stale = asyncio.create_task(
            bridge.service.update_catalog(
                bridge.device["device_id"], connection, [{**rows[0], "label": "Stale catalog"}]
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            replacement = NativeComputer(bridge)
            replacement.local_id = "later-instance"
            await replacement.start()
        finally:
            release.set()
            await stale
        current = (await bridge.http.get("/agents/v1/devices", headers=bridge.headers)).json()
        assert [item["id"] for item in current["instances"]] == [identity]

        # An empty snapshot still exposes the computer; restoring the same
        # configured profile keeps its original server and session identity.
        replacement.catalog_ready.clear()
        await replacement.socket.send(json.dumps({"type": "catalog", "instances": []}))
        await asyncio.wait_for(replacement.catalog_ready.wait(), 5)
        empty = (await bridge.http.get("/agents/v1/devices", headers=bridge.headers)).json()
        assert empty["instances"] == [] and empty["devices"][0]["online"]
        replacement.catalog_ready.clear()
        await replacement.socket.send(json.dumps({"type": "catalog", "instances": [rows[0]]}))
        await asyncio.wait_for(replacement.catalog_ready.wait(), 5)
        restored = (await bridge.http.get("/agents/v1/devices", headers=bridge.headers)).json()
        assert restored["instances"][0]["id"] == instance

        principal = await bridge.chat.authenticate(bridge.chat_device["token"])
        revoke_id = str(uuid4())
        revoked = await bridge.chat.revoke(principal, bridge.device["device_id"], revoke_id)
        assert revoked["device_id"] == bridge.device["device_id"] and revoked["revoked"]
        assert (await bridge.http.get("/agents/v1/devices", headers=bridge.headers)).json()[
            "devices"
        ] == []
        with pytest.raises(ClientError, match="unauthenticated"):
            await bridge.service.execute(
                {"action": "list", "instance_id": instance}, str(uuid4()), bridge.source
            )
        with pytest.raises(ClientError, match="unauthenticated"):
            await bridge.service.execute(
                {"action": "cancel", "operation_id": pending_id}, str(uuid4()), bridge.source
            )
        receipt = await bridge.http.get(
            f"/agents/v1/operations/{operation_id}", headers=bridge.headers
        )
        assert receipt.status_code == 200 and receipt.json()["status"] == "succeeded"
        async with bridge.database.read_session() as reader:
            stored = await reader.get(AgentOperationModel, operation_id)
            assert stored.instance_id == instance and stored.run_id == operation["run_id"]
            assert (
                await reader.scalar(
                    select(func.count())
                    .select_from(AgentEventModel)
                    .where(AgentEventModel.event_id == event["event_id"])
                )
                == 1
            )
        (tmp_path / "catalog-history.json").write_text(
            json.dumps(
                {
                    "device_id": bridge.device["device_id"],
                    "instance_id": instance,
                    "session_id": session_id,
                    "operation_id": operation_id,
                    "pending_operation_id": pending_id,
                    "execution_id": execution_id,
                    "execution_control_request_id": execution_control_id,
                    "control_operation_id": control_id,
                    "run_id": operation["run_id"],
                    "event_id": event["event_id"],
                    "late_event_id": late["event_id"],
                    "revoke_request_id": revoke_id,
                    "receipt": receipt.json(),
                    "late_receipt": completed.json(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        await computer.close()
        if replacement is not None:
            await replacement.close()


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
            await settled(bridge)
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
            await settled(bridge)
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
            # The device received the command and dropped the socket before answering.
            await settled(bridge)
            await computer.close()
            # What a real connector journal holds after begin without a native answer.
            computer.results[operation_id] = {
                "status": "unknown",
                "pending": True,
                "reason": "original_dispatch_not_confirmed",
                "state_sequence": 0,
            }
            await computer.start()
            await settled(bridge)
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
            detach_id = str(uuid4())
            await bridge.service.execute(
                {"action": "detach", "operation_id": operation_id},
                detach_id,
                bridge.source,
            )
            await settled(bridge)
            result = await bridge.service.execute(
                {"action": "get", "operation_id": detach_id}, str(uuid4()), bridge.source
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


async def settled(bridge):
    """Wait for this process's background device work; tests only observe its facts."""
    await bridge.service.drain()


async def until(predicate):
    # Polls durable/transport facts written by other tasks; there is no event to await.
    async with asyncio.timeout(5):
        while not await predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


async def receipt(bridge, operation_id):
    async with bridge.database.read_session() as reader:
        row = await reader.get(AgentOperationModel, operation_id)
        return json.loads(row.result_json) if row.result_json else None


async def test_unanswered_prompt_never_holds_the_activation_and_reconnect_queries_original_id(
    bridge,
):
    """F1/F3/F4/F6/F7: a silent native owner cannot occupy the Work activation."""
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            computer.hold = {"prompt", "get"}
            operation_id = str(uuid4())
            arguments = {
                "action": "prompt",
                "instance_id": instance,
                "session_id": session_id,
                "prompt": "Keep working while the owner is silent",
            }
            # The device never answers; returning at all proves the activation is free.
            async with asyncio.timeout(5):
                result = await bridge.service.execute(arguments, operation_id, bridge.source)
            assert result["operation_id"] == operation_id and result["run_id"]
            assert result["pending"] is True and result["status"] == "unknown"
            assert await control.repository.has_owned_execution(
                control.lease, control.current["id"]
            )

            async def written():
                return any(item["id"] == operation_id for item in computer.commands)

            await until(written)
            async with asyncio.timeout(5):
                queried = await bridge.service.execute(
                    {"action": "get", "operation_id": operation_id}, str(uuid4()), bridge.source
                )
                again = await bridge.service.execute(arguments, operation_id, bridge.source)
            assert queried["operation_id"] == operation_id and queried["pending"] is True
            assert again["operation_id"] == operation_id
            # The original command is still in flight; no query races it for the receipt.
            assert [item["action"] for item in computer.commands].count("get") == 0

            await computer.close()
            await settled(bridge)
            assert await receipt(bridge, operation_id) is None
            computer.hold = set()
            computer.results[operation_id] = {
                "session_id": computer.native_session,
                "execution_id": computer.native_execution,
                "status": "running",
                "pending": True,
                "admitted": True,
                "state_sequence": 1,
            }
            await computer.start()

            async def confirmed():
                return (await receipt(bridge, operation_id) or {}).get("admitted") is True

            await until(confirmed)
            await settled(bridge)
            assert [item["action"] for item in computer.commands].count("prompt") == 1
            queries = [item for item in computer.commands if item["action"] == "get"]
            assert queries[-1]["arguments"] == {"operation_id": operation_id}
    finally:
        await computer.close()


async def test_device_without_the_original_record_is_never_given_a_second_prompt(bridge):
    """F5: not_found is a reconciliation fact, never success and never a resend licence."""
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge):
            computer.drop_next = "prompt"
            operation_id = str(uuid4())
            arguments = {
                "action": "prompt",
                "instance_id": instance,
                "session_id": session_id,
                "prompt": "Delivered at most once",
            }
            await bridge.service.execute(arguments, operation_id, bridge.source)
            await settled(bridge)
            computer.not_found = {operation_id}
            await computer.start()

            async def reconciled():
                return any(
                    item["action"] == "get"
                    and item["arguments"].get("operation_id") == operation_id
                    for item in computer.commands
                )

            await until(reconciled)
            await settled(bridge)
            assert await receipt(bridge, operation_id) is None
            view = await bridge.service.execute(
                {"action": "get", "operation_id": operation_id}, str(uuid4()), bridge.source
            )
            assert view["uncertain"] is True and view["reconciliation_status"] == "not_found"
            await bridge.service.execute(arguments, operation_id, bridge.source)
            await settled(bridge)
            assert [item["action"] for item in computer.commands].count("prompt") == 1
    finally:
        await computer.close()


async def test_reconciliation_answer_belongs_to_the_connection_that_gave_it(bridge):
    """F8: a not_found from one connection is not reported once that connection is gone."""
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge):
            computer.drop_next = "prompt"
            operation_id = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "Answered by a connection that later ends",
                },
                operation_id,
                bridge.source,
            )
            await settled(bridge)
            computer.not_found = {operation_id}
            await computer.start()

            async def reconciled():
                return any(
                    item["action"] == "get"
                    and item["arguments"].get("operation_id") == operation_id
                    for item in computer.commands
                )

            await until(reconciled)
            await settled(bridge)
            query = {"action": "get", "operation_id": operation_id}
            view = await bridge.service.execute(query, str(uuid4()), bridge.source)
            assert view["reconciliation_status"] == "not_found"
            await computer.close()

            async def offline():
                return not bridge.service.connections.online(bridge.device["device_id"])

            await until(offline)
            gone = await bridge.service.execute(query, str(uuid4()), bridge.source)
            assert "reconciliation_status" not in gone
            assert gone["device_online"] is False and gone["uncertain"] is True
            assert await receipt(bridge, operation_id) is None
    finally:
        await computer.close()


async def test_reconnect_reads_back_only_controls_whose_answer_still_matters(bridge):
    """F9: a control is read back while its target is unsettled, not forever."""
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        targets = []
        async with accepted_work(bridge):
            for prompt in ("Finishes before the cancel answer", "Still running"):
                operation_id = str(uuid4())
                await bridge.service.execute(
                    {
                        "action": "prompt",
                        "instance_id": instance,
                        "session_id": session_id,
                        "prompt": prompt,
                    },
                    operation_id,
                    bridge.source,
                )
                targets.append(operation_id)
            await settled(bridge)
        finished, running = targets
        computer.hold = {"cancel"}
        controls = {}
        for target in targets:
            # Outside any Work: no initiating Work waits for these answers.
            controls[target] = str(uuid4())
            await bridge.service.execute(
                {"action": "cancel", "operation_id": target}, controls[target], bridge.source
            )

        async def held():
            return set(controls.values()) <= set(computer.held)

        await until(held)
        await computer.event(finished, "completed", {"status": "completed"})
        assert (await receipt(bridge, finished))["pending"] is False
        await computer.close()
        await settled(bridge)
        computer.hold = set()
        computer.commands.clear()
        await computer.start()

        async def read_back():
            return any(
                item["action"] == "get"
                and item["arguments"].get("operation_id") == controls[running]
                for item in computer.commands
            )

        await until(read_back)
        await settled(bridge)
        queried = {
            item["arguments"].get("operation_id")
            for item in computer.commands
            if item["action"] == "get"
        }
        assert controls[finished] not in queried
        # Nothing was resent: no second cancel reached the device.
        assert [item["action"] for item in computer.commands].count("cancel") == 0
    finally:
        await computer.close()


async def test_late_native_refusal_settles_the_original_run_without_the_activation(bridge):
    """F2: the response arriving after the tool returned still settles the original run."""
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge):
            computer.hold = {"prompt"}
            operation_id = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "Native owner will refuse later",
                },
                operation_id,
                bridge.source,
            )

            async def held():
                return operation_id in computer.held

            await until(held)
            await computer.answer(
                operation_id,
                {"status": "failed", "pending": False, "admitted": False, "error": "conflict"},
            )
            await settled(bridge)
            async with bridge.database.read_session() as reader:
                task = await reader.get(SandboxTaskRunModel, operation_id)
                continuation = await reader.get(SandboxTaskContinuationModel, operation_id)
            assert task.status == "completed" and continuation.state == "ready"
            assert json.loads(task.completion_json)["status"] == "failed"
    finally:
        await computer.close()


async def test_declared_capabilities_reject_unsupported_new_input_before_registration(bridge):
    """F8/F9: an explicit catalog refusal is immediate; an unclear catalog defers to the owner."""
    computer = NativeComputer(bridge)
    computer.capabilities = ["list", "get", "watch", "prompt", "respond", "cancel", "detach"]
    await computer.start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge):
            with pytest.raises(ExternalAgentArgumentError, match="unsupported_action"):
                await bridge.service.execute(
                    {
                        "action": "queue",
                        "instance_id": instance,
                        "session_id": session_id,
                        "prompt": "Not a declared native capability",
                    },
                    str(uuid4()),
                    bridge.source,
                )
        assert [item["action"] for item in computer.commands] == ["list"]
        async with bridge.database.read_session() as reader:
            assert await reader.scalar(select(func.count()).select_from(AgentOperationModel)) == 0
    finally:
        await computer.close()
    unclear = NativeComputer(bridge)
    unclear.capabilities = []
    await unclear.start()
    try:
        async with accepted_work(bridge):
            operation_id = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "queue",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "The native owner decides",
                },
                operation_id,
                bridge.source,
            )
            await settled(bridge)
        assert any(item["id"] == operation_id for item in unclear.commands)
    finally:
        await unclear.close()


async def test_capability_refusal_reads_the_live_connection_not_a_stale_stored_catalog(bridge):
    """Q15 regression: stored catalogs never refuse; original-owner controls reach the device."""
    from yuki.external_agents.db_models import AgentInstanceModel

    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        # The stored row says queue/steer are absent, but this live connection declared them.
        async with bridge.database.immediate_session() as writer:
            row = await writer.get(AgentInstanceModel, instance)
            stale = json.loads(row.catalog_json)
            row.catalog_json = json.dumps({**stale, "capabilities": ["list", "get", "prompt"]})
        async with accepted_work(bridge):
            queued = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "queue",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "Live declaration allows this",
                },
                queued,
                bridge.source,
            )
            await settled(bridge)
        assert any(item["id"] == queued for item in computer.commands)
    finally:
        await computer.close()

    restricted = NativeComputer(bridge)
    restricted.capabilities = ["list", "get", "watch", "prompt", "respond", "cancel", "detach"]
    await restricted.start()
    try:
        async with accepted_work(bridge):
            original = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "Original owner",
                },
                original,
                bridge.source,
            )
            await settled(bridge)
            # Controls of an original operation are not refused from today's declaration.
            steer = str(uuid4())
            await bridge.service.execute(
                {"action": "steer", "operation_id": original, "prompt": "Adjust"},
                steer,
                bridge.source,
            )
        assert any(item["id"] == steer for item in restricted.commands)
    finally:
        await restricted.close()


async def test_new_input_on_an_existing_operation_never_claims_its_native_turn(bridge):
    """P0-3: a selected operation locates the session; its turn is not pre-bound."""
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge):
            original = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "Work A owns this turn",
                },
                original,
                bridge.source,
            )
            await settled(bridge)
        async with accepted_work(bridge):
            computer.native_execution = "turn-started-by-the-new-input"
            queued = str(uuid4())
            await bridge.service.execute(
                {"action": "queue", "operation_id": original, "prompt": "Work B input"},
                queued,
                bridge.source,
            )
            await settled(bridge)
        command = next(item for item in computer.commands if item["id"] == queued)
        assert "execution_id" not in command
        assert command["arguments"]["operation_id"] == original
        async with bridge.database.read_session() as reader:
            first = await reader.get(AgentOperationModel, original)
            second = await reader.get(AgentOperationModel, queued)
        # Each operation is bound only to the turn the device reported for it.
        assert first.native_execution_id == "turn-from-native-owner"
        assert second.native_execution_id == "turn-started-by-the-new-input"
    finally:
        await computer.close()


@pytest.mark.parametrize("device_state", ["offline", "no_record"])
async def test_explicit_detach_ends_the_work_relationship_without_the_device(bridge, device_state):
    """Q11: unknown delivery leaves an explicit exit that never claims native facts."""
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            computer.hold = {"prompt"}
            operation_id = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "Delivery stays unknown",
                },
                operation_id,
                bridge.source,
            )

            async def held():
                return operation_id in computer.held

            await until(held)
            if device_state == "offline":
                await computer.close()
                await settled(bridge)
            else:
                computer.fail = {
                    "detach": {
                        "code": "original_operation_not_found",
                        "message": "Original operation is unavailable",
                        "admitted": False,
                        "executed": False,
                    }
                }
            assert await control.repository.has_owned_execution(
                control.lease, control.current["id"]
            )
            detach_id = str(uuid4())
            await bridge.service.execute(
                {"action": "detach", "operation_id": operation_id}, detach_id, bridge.source
            )

            # The original prompt is deliberately never answered, so wait for this
            # control's own receipt rather than for all device work to finish.
            async def answered():
                return await receipt(bridge, detach_id) is not None

            await until(answered)
            released = await bridge.service.execute(
                {"action": "get", "operation_id": detach_id}, str(uuid4()), bridge.source
            )
            assert released["status"] == "released" and released["executed"] is False
            # The Work that issued it is told what the control settled.
            async with bridge.database.read_session() as reader:
                told = (
                    await reader.execute(
                        select(inputs.c.payload_json).where(
                            inputs.c.work_id == control.current["id"],
                            inputs.c.source_key.like(f"external-agent:control:{detach_id}%"),
                        )
                    )
                ).scalar_one()
            assert json.loads(json.loads(told)["text"])["status"] == "released"
            assert not await control.repository.has_owned_execution(
                control.lease, control.current["id"]
            )
            async with bridge.database.read_session() as reader:
                original = await reader.get(AgentOperationModel, operation_id)
                task = await reader.get(SandboxTaskRunModel, operation_id)
            stored = json.loads(original.result_json)
            assert original.detached_at is not None and stored["status"] == "detached"
            # The prompt was never confirmed; ending the relationship does not say otherwise.
            assert stored["admitted"] is False and stored["native_status"] == "unknown"
            assert json.loads(task.completion_json)["admitted"] is False
        assert [item["action"] for item in computer.commands].count("prompt") == 1
    finally:
        await computer.close()


async def test_unowned_native_wait_is_discoverable_metadata_answerable_only_with_deny(bridge):
    """The incident's unseen Bash permission: discoverable, privacy-minimal, deny-only."""
    from tests.support.canonical_identity import ensure_person

    computer = await NativeComputer(bridge).start()
    try:
        _instance, session_id = await directory(bridge)
        computer.sequence += 1
        request = {
            "type": "event",
            "event_id": str(uuid4()),
            "instance_id": computer.local_id,
            "operation_id": None,
            "session_id": computer.native_session,
            "execution_id": "self-started:turn",
            "kind": "permission",
            "payload": {
                "permission_id": "native-tool-use",
                "native_request_id": "native-tool-use",
                "connection_id": "sdk-owner-one",
                "tool": "Bash",
                "options": ["deny"],
                "unowned": True,
                "status": "requires_action",
                "pending": True,
            },
        }
        await computer.socket.send(json.dumps(request))
        async with asyncio.timeout(5):
            assert await computer.acknowledged.get() == request["event_id"]
        # The same computer's wait in another session belongs to that session only.
        computer.sequence += 1
        elsewhere = {
            **request,
            "event_id": str(uuid4()),
            "session_id": "another-native-thread",
            "payload": {**request["payload"], "native_request_id": "other-tool-use"},
            "sequence": computer.sequence,
        }
        await computer.socket.send(json.dumps(elsewhere))
        async with asyncio.timeout(5):
            assert await computer.acknowledged.get() == elsewhere["event_id"]
        view = await bridge.service.execute(
            {"action": "get", "session_id": session_id}, str(uuid4()), bridge.source
        )
        [waiting] = view["pending_unowned_requests"]
        assert waiting["tool"] == "Bash" and waiting["options"] == ["deny"]
        assert set(waiting) == {"permission_id", "kind", "tool", "received_at", "options"}
        # Another person cannot discover or answer this computer's waits.
        async with bridge.database.immediate_session() as writer:
            stranger = await ensure_person(writer, "20002")
        with pytest.raises(ValueError, match="external_agent_not_found"):
            await bridge.service.execute(
                {"action": "get", "session_id": session_id},
                str(uuid4()),
                {"actor_person_id": stranger},
            )
        answer = {
            "action": "respond",
            "session_id": session_id,
            "permission_id": waiting["permission_id"],
        }
        with pytest.raises(ExternalAgentArgumentError, match="deny_only"):
            await bridge.service.execute(
                {**answer, "option_id": "allow"}, str(uuid4()), bridge.source
            )
        with pytest.raises(ValueError, match="external_agent_not_found"):
            await bridge.service.execute(
                {**answer, "option_id": "deny"}, str(uuid4()), {"actor_person_id": stranger}
            )
        assert not any(item["action"] == "respond" for item in computer.commands)
        await bridge.service.execute({**answer, "option_id": "deny"}, str(uuid4()), bridge.source)
        await settled(bridge)
        native = next(item for item in computer.commands if item["action"] == "respond")
        assert native["arguments"]["option_id"] == "deny"
        assert native["arguments"]["native_request_id"] == "native-tool-use"
        assert native["arguments"]["connection_id"] == "sdk-owner-one"
        assert native["session_id"] == computer.native_session
        resolved = {
            **request,
            "event_id": str(uuid4()),
            "kind": "progress",
            "payload": {"permission_resolved": "native-tool-use", "outcome": "answered"},
        }
        await computer.socket.send(json.dumps(resolved))
        async with asyncio.timeout(5):
            assert await computer.acknowledged.get() == resolved["event_id"]
        after = await bridge.service.execute(
            {"action": "get", "session_id": session_id}, str(uuid4()), bridge.source
        )
        assert after["pending_unowned_requests"] == []
    finally:
        await computer.close()
