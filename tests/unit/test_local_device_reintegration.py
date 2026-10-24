"""Actual personal HTTP, static Agent tools, grants and original executor receipts."""

import asyncio
import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_api import bearer
from tests.unit.test_client_works import owner

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.client_api.errors import ClientError
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.domain.tool_actor import ToolActor
from yuki.executor.db_models import ExecutorOperationModel
from yuki.executor.service import LocalExecutors
from yuki.runtime.origin import TurnOrigin
from yuki.services.agent_tools import ToolRuntime

admin = admin_fixture
app_env = app_fixture


@pytest.fixture
async def local(app_env, database):
    device, person = await owner(app_env, database)
    service = LocalExecutors(app_env.identity, app_env.store)
    app = FastAPI()
    attach_client_api(app, lambda: ClientApi(app_env.identity, executors=service))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/client/v1/executors",
            headers=bearer(device["token"]),
            json={"request_id": str(uuid4()), "label": "real local device", "platform": "test"},
        )
        assert created.status_code == 200, created.text
        executor = created.json()["data"]
        principal = await app_env.identity.authenticate(device["token"])
        channels = []
        try:
            yield SimpleNamespace(
                service=service,
                client=client,
                executor=executor,
                device=device,
                person=person,
                principal=principal,
                env=app_env,
                database=database,
                channels=channels,
            )
        finally:
            for channel in reversed(channels):
                await channel.aclose()


async def grant(local, grants):
    current = await local.service.config(local.executor["executor_id"])
    response = await local.client.put(
        f"/client/v1/executors/{local.executor['executor_id']}/grants",
        headers=bearer(local.device["token"]),
        json={
            "request_id": str(uuid4()),
            "expected_revision": current["revision"],
            "grants": grants,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_fixed_manifest_calls_real_agent_tool_and_returns_original_pending_without_wait(
    local,
):
    chat = local.env.harness.processor._chat
    chat._tools.local_executors = local.service
    contract = chat.runtime.runner.main_contract
    declaration = await contract.definitions()
    descriptor = next(tool for tool in declaration if tool.name == "local_device")
    assert descriptor.parameters["properties"]["action"]["enum"] == ["list", "get", "execute"]
    await grant(local, [{"capability": "notify"}])
    channel = local.service.stream(local.executor["executor_id"])
    local.channels.append(channel)
    await anext(channel)
    runtime = ToolRuntime(
        inbound=None,
        actor_context=ToolActor(
            user_id="1001",
            bot_user_id="yuki-app",
            group_id=None,
            origin=TurnOrigin.USER_MESSAGE,
            instruction="show the authorized notification",
            execution_id="original-device-tool-execution",
            person_id=local.person,
            conversation_id=local.principal.conversation_id,
        ),
        person_id=local.person,
        conversation_id=local.principal.conversation_id,
        execution_id="original-device-tool-execution",
    )
    arguments = {
        "action": "execute",
        "executor_id": local.executor["executor_id"],
        "capability": "notify",
        "params": {"title": "Yuki", "body": "actual authorized request"},
    }
    token = current_invocation.set(ToolInvocationContext(runtime, "original-host-call"))
    try:
        # A native reply is deliberately absent. Dispatch must return its receipt now.
        result = await chat._tools.execute("local_device", json.dumps(arguments), runtime)
        assert result.ok and result.data["pending"]
        operation = result.data["op_id"]
        assert result.data["operation_id"] == operation
        assert result.data["state"] == "dispatched"
        again = await chat._tools.execute("local_device", json.dumps(arguments), runtime)
        assert again.data["op_id"] == operation
        headers = bearer(local.executor["token"])
        ack = await local.client.post(f"/executor/v1/operations/{operation}/ack", headers=headers)
        assert ack.status_code == 200, ack.text
        completed = await local.client.post(
            f"/executor/v1/operations/{operation}/result",
            headers=headers,
            json={"state": "succeeded", "level": "submitted", "result": {"submitted": True}},
        )
        assert completed.status_code == 200, completed.text
        final = await chat._tools.execute("local_device", json.dumps(arguments), runtime)
        assert final.data["op_id"] == operation
        assert final.data["pending"] is False and final.data["level"] == "submitted"
        async with local.database.read_session() as reader:
            assert (
                await reader.scalar(select(func.count()).select_from(ExecutorOperationModel)) == 1
            )
        assert await contract.definitions() == declaration
    finally:
        current_invocation.reset(token)
        await channel.aclose()


async def test_reopening_executor_stream_preserves_legacy_pending_bytes_without_dispatch(local):
    original_id = str(uuid4())
    raw = '{ "title":"original", "body":"must not replay" }'
    async with local.database.immediate_session() as writer:
        writer.add(
            ExecutorOperationModel(
                op_id=original_id,
                executor_id=local.executor["executor_id"],
                person_id=local.person,
                work_id=None,
                tool_call_key="original-retired-host-key",
                capability="notify",
                params_json=raw,
                state="registered",
                receipt_hash="f" * 64,
                created_at=datetime.now(UTC),
            )
        )
    reopened = LocalExecutors(local.env.identity, local.env.store)
    channel = reopened.stream(local.executor["executor_id"])
    local.channels.append(channel)
    try:
        sync = await anext(channel)
        body = json.loads(sync.split("data: ", 1)[1])
        assert body["pending"] == []
        old = await reopened.tools(local.person, "get", {"op_id": original_id}, "read", None)
        assert old["state"] == "registered" and old["op_id"] == original_id
        async with local.database.read_session() as reader:
            row = await reader.get(ExecutorOperationModel, original_id)
            assert (row.params_json, row.state, row.tool_call_key, row.receipt_hash) == (
                raw,
                "registered",
                "original-retired-host-key",
                "f" * 64,
            )
        assert reopened.dispatching == set()
    finally:
        await channel.aclose()


async def test_grant_removed_during_artifact_preparation_prevents_new_effect(local, monkeypatch):
    source = local.env.store.root / "fixture-source.txt"
    source.write_bytes(b"actual immutable artifact")
    with source.open("rb") as stream:
        artifact = local.env.store.snapshot(stream.fileno(), "actual.txt", source_key="local-test")
    await grant(
        local,
        [{"capability": "folder.write", "scope": {"alias": "Inbox", "digest": "a" * 64}}],
    )
    channel = local.service.stream(local.executor["executor_id"])
    local.channels.append(channel)
    await anext(channel)
    entered, release = threading.Event(), threading.Event()

    read_bytes = local.env.store.read_bytes

    def prepare(identity):
        entered.set()
        assert release.wait(10)
        return read_bytes(identity)

    monkeypatch.setattr(local.env.store, "read_bytes", prepare)
    operation = asyncio.create_task(
        local.service.tools(
            local.person,
            "execute",
            {
                "executor_id": local.executor["executor_id"],
                "capability": "folder.write",
                "params": {
                    "path": "Inbox/output.txt",
                    "artifact_id": artifact["artifact_id"],
                    "version": artifact["sha256"],
                },
            },
            "original-before-grant-change",
            None,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        await grant(local, [])
        release.set()
        with pytest.raises(ClientError, match="permission_denied"):
            await operation
        async with local.database.read_session() as reader:
            assert (
                await reader.scalar(select(func.count()).select_from(ExecutorOperationModel)) == 0
            )
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)
        await channel.aclose()
