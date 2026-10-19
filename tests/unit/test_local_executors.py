"""Real identity, HTTP credentials, SQLite operations and original Work receipts."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text, update
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_api import bearer, pair
from tests.unit.test_client_works import owner, seeded

from yuki.client_api.errors import ClientError
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.executor.db_models import ExecutorModel, ExecutorOperationModel
from yuki.executor.service import LocalExecutors
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_repository import WorkConflict
from yuki.runtime.work_schema_v1 import effects, work

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
        principal = await app_env.identity.authenticate(device["token"])
        request = {"request_id": str(uuid4()), "label": "my computer", "platform": "darwin"}
        created = await client.post(
            "/client/v1/executors", json=request, headers=bearer(device["token"])
        )
        assert created.status_code == 200, created.text
        executor = created.json()["data"]
        yield SimpleNamespace(
            client=client,
            service=service,
            app=app,
            env=app_env,
            device=device,
            person=person,
            principal=principal,
            executor=executor,
            request=request,
        )


async def grant(local, capability="notify", scope=None):
    config = await local.service.config(local.executor["executor_id"])
    body = {
        "request_id": str(uuid4()),
        "expected_revision": config["revision"],
        "grants": [{"capability": capability, **({"scope": scope} if scope else {})}],
    }
    response = await local.client.put(
        f"/client/v1/executors/{local.executor['executor_id']}/grants",
        json=body,
        headers=bearer(local.device["token"]),
    )
    assert response.status_code == 200, response.text
    return response.json()["data"], body


async def dispatch(local, stream, *, key=None, work_id=None, capability="notify", params=None):
    key = key or f"host:{uuid4()}"
    task = asyncio.create_task(
        local.service.tools(
            local.person,
            "invoke",
            {
                "executor_id": local.executor["executor_id"],
                "capability": capability,
                "params": params or {"title": "hello", "body": "real request"},
            },
            key,
            work_id,
        )
    )
    while True:
        frame = await asyncio.wait_for(anext(stream), 3)
        if frame.startswith("event: operation"):
            op_id = json.loads(frame.split("data: ")[1])["op_id"]
            break
    response = await local.client.post(
        f"/executor/v1/operations/{op_id}/ack", headers=bearer(local.executor["token"])
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["state"] == "acknowledged"
    return op_id, task, key


async def finish(local, op_id, **changes):
    body = {"state": "succeeded", "level": "submitted", "result": {"submitted": True}, **changes}
    return await local.client.post(
        f"/executor/v1/operations/{op_id}/result",
        json=body,
        headers=bearer(local.executor["token"]),
    )


async def test_creation_secret_once_receipts_cross_device_replay_and_revocation(local, database):
    env = local
    replay = await env.client.post(
        "/client/v1/executors", json=env.request, headers=bearer(env.device["token"])
    )
    assert replay.status_code == 200 and "token" not in replay.json()["data"]
    found = await env.client.get(
        f"/client/v1/requests/{env.request['request_id']}", headers=bearer(env.device["token"])
    )
    assert found.status_code == 200 and found.json()["data"]["found"]
    assert env.executor["token"] not in found.text
    listing = await env.client.get("/client/v1/executors", headers=bearer(env.device["token"]))
    assert listing.json()["data"][0]["claimed"] is False
    assert env.executor["token"] not in listing.text
    assert (
        await env.client.get("/executor/v1/config", headers=bearer(env.device["token"]))
    ).status_code == 401
    assert (
        await env.client.get("/client/v1/me", headers=bearer(env.executor["token"]))
    ).status_code == 401
    async with database.sessions() as session:
        receipt = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.request_id == env.request["request_id"]
            )
        )
        assert env.executor["token"] not in receipt.effective_state_json
        row = await session.get(ExecutorModel, env.executor["executor_id"])
        assert row.token_hash != env.executor["token"] and len(row.token_hash) == 64
    issued = await env.env.identity.issue_for_device(env.principal, str(uuid4()))
    second = await pair(env.env.client, issued["code"])
    body = {"request_id": str(uuid4())}
    response = await env.client.request(
        "DELETE",
        f"/client/v1/executors/{env.executor['executor_id']}",
        json=body,
        headers=bearer(second["token"]),
    )
    assert response.status_code == 200
    assert (
        await env.client.get("/executor/v1/config", headers=bearer(env.executor["token"]))
    ).status_code == 401


async def test_live_stream_grants_pause_offline_no_queue_and_original_completion(local, database):
    config, original = await grant(local)
    with pytest.raises(ClientError, match="executor_offline"):
        await local.service.tools(
            local.person,
            "invoke",
            {
                "executor_id": config["executor_id"],
                "capability": "notify",
                "params": {"title": "test", "body": "test"},
            },
            "offline-host-key",
            None,
        )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutorOperationModel)) == 0
    stream = local.service.stream(config["executor_id"])
    assert "event: sync" in await anext(stream)
    assert (await local.service.list(local.principal))[0]["online"]
    op_id, task, key = await dispatch(local, stream)
    pause = await local.client.post(
        f"/client/v1/executors/{config['executor_id']}/pause",
        json={"request_id": str(uuid4())},
        headers=bearer(local.device["token"]),
    )
    assert pause.status_code == 200
    with pytest.raises(ClientError, match="executor_paused"):
        await local.service.tools(
            local.person,
            "invoke",
            {
                "executor_id": config["executor_id"],
                "capability": "notify",
                "params": {"title": "test", "body": "test"},
            },
            "paused-host-key",
            None,
        )
    assert (await finish(local, op_id)).status_code == 200
    result = await task
    assert result["state"] == "succeeded" and result["level"] == "submitted"
    assert (await finish(local, op_id)).json()["data"]["op_id"] == op_id
    assert (await finish(local, op_id, result={})).status_code == 409
    assert (
        await finish(local, op_id, state="failed", level=None, result={"reason": "different"})
    ).status_code == 409
    stale = await local.client.put(
        f"/client/v1/executors/{config['executor_id']}/grants",
        json={**original, "request_id": str(uuid4())},
        headers=bearer(local.device["token"]),
    )
    assert stale.status_code == 409
    await stream.aclose()
    assert (await local.service.list(local.principal))[0]["online"] is False
    history = await local.service.history(local.principal, config["executor_id"], None, 1)
    assert [row["op_id"] for row in history["items"]] == [op_id]
    async with database.sessions() as session:
        row = await session.get(ExecutorOperationModel, op_id)
        assert row.tool_call_key == key and row.work_id is None
        assert await session.scalar(select(func.count()).select_from(work)) == 0


@pytest.mark.parametrize("first", ["result", "host"])
@pytest.mark.parametrize("terminal_work", [False, True])
async def test_terminal_and_host_first_acceptance_converge_original_effect(
    local, database, first, terminal_work
):
    original = await seeded(local.env, database, retain_lease=True)
    repo, key = original.repository, f"host:{uuid4()}"
    metadata = {"version": 1, "tool_id": "local_device", "revision": 0, "operation_id": key}
    composition = {"parent": "original composition"}
    await repo.prepare_effect(
        original.lease,
        original.row["id"],
        key,
        "tool",
        invocation=metadata,
        composition=composition,
    )
    await grant(local)
    stream = local.service.stream(local.executor["executor_id"])
    await anext(stream)
    op_id, task, _ = await dispatch(local, stream, key=key, work_id=original.row["id"])
    pending = {
        "outcome": {
            "tool": "local_device",
            "pending": True,
            "op_id": op_id,
            "ok": True,
            "side_effecting": True,
        }
    }
    if first == "host":
        await repo.record_effect(key, "accepted", pending)
    if terminal_work:
        async with database.immediate_session() as session:
            await session.execute(
                update(work).where(work.c.id == original.row["id"]).values(state="cancelled")
            )
    assert (await finish(local, op_id)).status_code == 200
    await task
    if first == "result":
        await repo.record_effect(key, "accepted", pending)
    async with database.sessions() as session:
        receipt = json.loads(
            await session.scalar(select(effects.c.receipt_json).where(effects.c.effect_key == key))
        )
        actual = (
            (await session.execute(select(work).where(work.c.id == original.row["id"])))
            .mappings()
            .one()
        )
        budget = (
            (await session.execute(select(budgets).where(budgets.c.root_id == original.row["id"])))
            .mappings()
            .one()
        )
    assert receipt["outcome"]["state"] == "succeeded" and receipt["outcome"]["pending"] is False
    assert receipt["outcome"]["op_id"] == op_id and receipt["outcome"]["level"] == "submitted"
    assert receipt["invocation"]["operation_id"] == key and receipt["composition"] == composition
    assert actual["state"] == ("cancelled" if terminal_work else "running")
    assert (actual["model_requests"], actual["tool_calls"], budget["models"], budget["tools"]) == (
        3,
        2,
        3,
        2,
    )
    with pytest.raises(WorkConflict, match="work_effect_receipt_conflict"):
        await repo.record_effect(key, "accepted", pending)
    await stream.aclose()
    await repo.release(original.lease)


async def test_effect_update_failure_rolls_back_operation_and_same_result_retries(local, database):
    original = await seeded(local.env, database, retain_lease=True)
    repo, key = original.repository, f"host:{uuid4()}"
    await repo.prepare_effect(
        original.lease,
        original.row["id"],
        key,
        "tool",
        invocation={"version": 1, "tool_id": "local_device", "revision": 0},
    )
    await grant(local)
    stream = local.service.stream(local.executor["executor_id"])
    await anext(stream)
    op_id, task, _ = await dispatch(local, stream, key=key, work_id=original.row["id"])
    await repo.record_effect(
        key, "accepted", {"outcome": {"tool": "local_device", "pending": True, "op_id": op_id}}
    )
    async with database.immediate_session() as session:
        await session.execute(
            text(
                "CREATE TRIGGER injected_effect_abort BEFORE UPDATE ON runtime_work_effects "
                "BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        )
    response = await finish(local, op_id)
    assert response.status_code == 500
    assert (await local.service.get(local.executor["executor_id"], op_id))[
        "state"
    ] == "acknowledged"
    async with database.immediate_session() as session:
        await session.execute(text("DROP TRIGGER injected_effect_abort"))
    assert (await finish(local, op_id)).status_code == 200
    assert (await task)["state"] == "succeeded"
    await stream.aclose()
    await repo.release(original.lease)


@pytest.mark.parametrize("first", ["host", "result"])
async def test_two_database_writers_serialize_first_acceptance_and_terminal(
    local, database, monkeypatch, first
):
    from contextlib import asynccontextmanager

    from yuki.persistence.database import Database
    from yuki.runtime.work_repository import WorkRepository

    original = await seeded(local.env, database, retain_lease=True)
    second = Database(database.url)
    repo, key = WorkRepository(second), f"host:{uuid4()}"
    await repo.prepare_effect(
        original.lease,
        original.row["id"],
        key,
        "tool",
        invocation={"version": 1, "tool_id": "local_device", "revision": 0},
    )
    await grant(local)
    stream = local.service.stream(local.executor["executor_id"])
    await anext(stream)
    op_id, task, _ = await dispatch(local, stream, key=key, work_id=original.row["id"])
    pending = {"outcome": {"tool": "local_device", "pending": True, "op_id": op_id}}
    reached, release, contender = asyncio.Event(), asyncio.Event(), asyncio.Event()
    held_database = second if first == "host" else database
    other_database = database if first == "host" else second
    held_original, other_original = (
        held_database.immediate_session,
        other_database.immediate_session,
    )

    @asynccontextmanager
    async def held_writer():
        async with held_original() as session:
            yield session
            reached.set()
            await release.wait()  # Fault injection at the real pre-COMMIT writer boundary.

    @asynccontextmanager
    async def contending_writer():
        contender.set()
        async with other_original() as session:
            yield session

    monkeypatch.setattr(held_database, "immediate_session", held_writer)
    monkeypatch.setattr(other_database, "immediate_session", contending_writer)

    async def host():
        await repo.record_effect(key, "accepted", pending)

    async def result():
        return await finish(local, op_id)

    first_task = asyncio.create_task(host() if first == "host" else result())
    await asyncio.wait_for(reached.wait(), 3)
    second_task = asyncio.create_task(result() if first == "host" else host())
    await asyncio.wait_for(contender.wait(), 3)
    assert not second_task.done()
    release.set()
    responses = await asyncio.gather(first_task, second_task)
    response = responses[1 if first == "host" else 0]
    assert response.status_code == 200
    assert (await task)["state"] == "succeeded"
    async with database.sessions() as session:
        receipt = json.loads(
            await session.scalar(select(effects.c.receipt_json).where(effects.c.effect_key == key))
        )
    assert receipt["outcome"]["pending"] is False and receipt["outcome"]["state"] == "succeeded"
    assert receipt["outcome"]["op_id"] == op_id
    monkeypatch.setattr(held_database, "immediate_session", held_original)
    monkeypatch.setattr(other_database, "immediate_session", other_original)
    await stream.aclose()
    await repo.release(original.lease)
    await second.engine.dispose()


async def test_executor_only_content_exact_immutable_version_and_terminal_acl(local):
    from tests.support.workspace_snapshots import snapshot_bytes

    artifact = snapshot_bytes(local.env.store, "report.txt", b"immutable content")
    params = {
        "path": "Inbox/report.txt",
        "artifact_id": artifact["artifact_id"],
        "version": artifact["sha256"],
    }
    await grant(local, "folder.write", {"alias": "Inbox", "digest": "a" * 64})
    stream = local.service.stream(local.executor["executor_id"])
    await anext(stream)
    op_id, task, _ = await dispatch(local, stream, capability="folder.write", params=params)
    response = await local.client.get(
        f"/executor/v1/operations/{op_id}/content", headers=bearer(local.executor["token"])
    )
    assert response.status_code == 200 and response.content == b"immutable content"
    assert response.headers["x-yuki-sha256"] == artifact["sha256"]
    assert (
        await local.client.get(
            f"/executor/v1/operations/{uuid4()}/content", headers=bearer(local.executor["token"])
        )
    ).status_code == 404
    changed = {**params, "version": "0" * 64}
    with pytest.raises(ClientError, match="not_found"):
        await local.service.tools(
            local.person,
            "invoke",
            {
                "executor_id": local.executor["executor_id"],
                "capability": "folder.write",
                "params": changed,
            },
            f"host:{uuid4()}",
            None,
        )
    wrong_level = await finish(local, op_id)
    assert wrong_level.status_code == 422
    assert (
        await finish(
            local,
            op_id,
            level="confirmed",
            result={"sha256": artifact["sha256"], "size": len(response.content), "created": True},
        )
    ).status_code == 200
    await task
    assert (
        await local.client.get(
            f"/executor/v1/operations/{op_id}/content", headers=bearer(local.executor["token"])
        )
    ).status_code == 404
    await stream.aclose()


async def test_no_requester_or_other_person_cannot_borrow_and_forget_erases_bodies(local, database):
    from tests.unit.test_client_api import person_and_code

    from yuki.persistence.people_repository import PeopleRepository

    other, _ = await person_and_code(local.env.client)
    assert (await local.service.tools(None, "list", {}, "self:list", None))["items"] == []
    assert (await local.service.tools(other, "list", {}, "other:list", None))["items"] == []
    with pytest.raises(ClientError, match="not_found"):
        await local.service.tools(
            other,
            "invoke",
            {
                "executor_id": local.executor["executor_id"],
                "capability": "notify",
                "params": {"title": "test", "body": "test"},
            },
            "other:invoke",
            None,
        )
    await grant(local)
    stream = local.service.stream(local.executor["executor_id"])
    await anext(stream)
    op_id, task, _ = await dispatch(local, stream)
    assert (
        await finish(
            local, op_id, state="unknown", level=None, result={}, observation={"exists": True}
        )
    ).status_code == 200
    assert (await task)["uncertain"]
    with pytest.raises(ClientError, match="not_found"):
        await local.service.tools(other, "get", {"op_id": op_id}, "other:get", None)
    await stream.aclose()
    await PeopleRepository(database).delete_person_id(local.person)
    async with database.sessions() as session:
        row = await session.get(ExecutorOperationModel, op_id)
        assert row.state == "unknown" and len(row.receipt_hash) == 64
        assert (row.created_at, row.dispatched_at, row.acknowledged_at, row.settled_at) == (
            None,
        ) * 4
        assert (
            row.person_id,
            row.executor_id,
            row.work_id,
            row.params_json,
            row.result_json,
            row.observation_json,
            row.capability,
        ) == (None,) * 7
        assert await session.scalar(select(func.count()).select_from(ExecutorModel)) == 0
    assert (
        await local.client.get("/executor/v1/config", headers=bearer(local.executor["token"]))
    ).status_code == 401


async def test_real_app_main_invocation_preserves_host_key_and_never_creates_work(local, database):
    from tests.unit.test_app_messages import drain

    from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction

    local.env.harness.processor._chat._tools.local_executors = local.service
    invoked = False

    def respond(request):
        nonlocal invoked
        if request.messages[-1].role == "tool":
            return "已提交系統通知"
        invoked = True
        assert "local_device" in {tool.name for tool in request.tools}
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(
                ToolCall(
                    id="provider-original-call",
                    function=ToolFunction(
                        name="local_device",
                        arguments=json.dumps(
                            {
                                "action": "invoke",
                                "executor_id": local.executor["executor_id"],
                                "capability": "notify",
                                "params": {"title": "hello", "body": "from App"},
                            }
                        ),
                    ),
                ),
            ),
        )

    local.env.provider._responder = respond
    await grant(local)
    stream = local.service.stream(local.executor["executor_id"])
    await anext(stream)
    response = await local.env.client.post(
        "/client/v1/messages",
        json={
            "request_id": str(uuid4()),
            "text": "請在我的電腦顯示通知",
            "attachments": [],
        },
        headers=bearer(local.device["token"]),
    )
    assert response.status_code == 200, response.text
    while True:
        frame = await asyncio.wait_for(anext(stream), 5)
        if frame.startswith("event: operation"):
            op_id = json.loads(frame.split("data: ")[1])["op_id"]
            break
    assert (
        await local.client.post(
            f"/executor/v1/operations/{op_id}/ack", headers=bearer(local.executor["token"])
        )
    ).status_code == 200
    assert (await finish(local, op_id)).status_code == 200
    await drain(local.env)
    assert invoked
    async with database.sessions() as session:
        op = await session.get(ExecutorOperationModel, op_id)
        assert op.person_id == local.person and op.work_id is None and op.state == "succeeded"
        assert op.tool_call_key.endswith(":provider-original-call")
        assert await session.scalar(select(func.count()).select_from(work)) == 0
    await stream.aclose()


async def test_server_restart_dispatches_original_registered_intent_once(
    local, database, monkeypatch
):
    from contextlib import asynccontextmanager

    await grant(local)
    first = local.service.stream(local.executor["executor_id"])
    await anext(first)
    original_writer, calls = database.immediate_session, 0

    @asynccontextmanager
    async def crash_before_dispatch():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConnectionError("injected crash after registration commit")
        async with original_writer() as session:
            yield session

    monkeypatch.setattr(database, "immediate_session", crash_before_dispatch)
    key = f"host:{uuid4()}"
    with pytest.raises(ConnectionError):
        await local.service.tools(
            local.person,
            "invoke",
            {
                "executor_id": local.executor["executor_id"],
                "capability": "notify",
                "params": {"title": "restart", "body": "original"},
            },
            key,
            None,
        )
    monkeypatch.setattr(database, "immediate_session", original_writer)
    await first.aclose()
    restarted = LocalExecutors(local.env.identity, local.env.store)
    stream = restarted.stream(local.executor["executor_id"])
    sync = json.loads((await anext(stream)).split("data: ")[1])
    assert len(sync["pending"]) == 1
    op_id = sync["pending"][0]
    assert (await restarted.get(local.executor["executor_id"], op_id))["state"] == "dispatched"
    await restarted.ack(local.executor["executor_id"], op_id)
    await restarted.result(
        local.executor["executor_id"],
        op_id,
        {"state": "succeeded", "level": "submitted", "result": {"submitted": True}},
    )
    replay = await restarted.tools(
        local.person,
        "invoke",
        {
            "executor_id": local.executor["executor_id"],
            "capability": "notify",
            "params": {"title": "restart", "body": "original"},
        },
        key,
        None,
    )
    assert replay["op_id"] == op_id and replay["state"] == "succeeded"
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutorOperationModel)) == 1
    await stream.aclose()


async def test_fixed_platform_app_and_code_contract_have_one_local_device(local, tmp_path):
    from yuki.services.main_agent_contract import MainAgentContract
    from yuki.workspace.short_state import ShortState
    from yuki.workspace.store import WorkspaceStore

    chat = local.env.harness.processor._chat
    for code in (False, True):
        contract = MainAgentContract(
            chat, ShortState(WorkspaceStore(tmp_path / str(code))), code_enabled=code
        )
        platform, app = await contract.definitions(), await contract.definitions("app")
        assert sum(tool.name == "local_device" for tool in platform) == 1
        assert sum(tool.name == "local_device" for tool in app) == 1
        if code:
            assert "local_device" in contract.script_api.schemas
            assert "local_device" in contract.app_script_api.schemas
