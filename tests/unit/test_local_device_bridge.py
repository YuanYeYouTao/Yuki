"""Original device routing and receipts, without a Work or native Agent prerequisite."""

import asyncio
import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.support.client_identity import client_identity_service
from tests.support.social_identity_cases import social_env

from yuki.client_api.errors import ClientError
from yuki.external_agents.identity import ConnectorIdentityService
from yuki.external_agents.service import ExternalAgentService
from yuki.external_agents.transport import DeviceConnection, DeviceOffline
from yuki.sandbox.client import SandboxClient
from yuki.sandbox.task_repository import SandboxTaskRepository
from yuki.workspace.store import WorkspaceError


async def setup_device(database, tmp_path):
    env = await social_env(database, tmp_path)
    identity = client_identity_service(database)
    code = await identity.issue_for_operator(
        env.person, str(uuid4()), str(uuid4()), purpose="connector"
    )
    device = await ConnectorIdentityService(database).pair(code["code"], "this computer")
    service = ExternalAgentService(database, SandboxTaskRepository(database))
    connection = DeviceConnection(device["device_id"], AsyncMock())
    connection.request = AsyncMock(
        side_effect=lambda command: {
            "run_id": command.get("run_id"),
            "status": "running",
            "pending": True,
            "output": "original",
            "next_cursor": 8,
        }
    )
    await service.connect(
        device["device_id"],
        connection,
        {
            "protocol_version": 4,
            "instances": [],
            "device_capabilities": {"full_access": True},
        },
    )
    return SimpleNamespace(
        env=env,
        service=service,
        local=service.local_devices,
        connection=connection,
        device_id=device["device_id"],
        source={
            "actor_person_id": env.person,
            "conversation_id": env.context.conversation_id,
            "parent_execution_id": str(uuid4()),
        },
    )


async def test_device_terminal_has_no_agent_or_work_gate_and_read_keeps_original_target(
    database, tmp_path
):
    c = await setup_device(database, tmp_path)
    client = SandboxClient(tmp_path / "unused", tasks=c.service.tasks)
    client.local_devices = c.local
    args = {"device_id": c.device_id, "command": "printf original"}
    started = await client.execute("terminal_exec", args, request_id="original", source=c.source)
    assert started["pending"] and started["device_id"] == c.device_id
    catalog = await c.service.catalog(c.env.person)
    assert catalog["instances"] == []
    assert catalog["devices"][0]["device_capabilities"]["full_access"]
    await client.execute(
        "terminal_read",
        {"run_id": started["run_id"], "cursor": 8},
        request_id="read-original",
        source=c.source,
    )
    command = c.connection.request.call_args.args[0]
    assert command["resource"] == "terminal" and command["action"] == "read"
    assert command["run_id"] == started["run_id"]
    await client.execute("terminal_exec", args, request_id="original", source=c.source)
    assert c.connection.request.call_args.args[0]["action"] == "get"
    with pytest.raises(ValueError, match="idempotency_conflict"):
        await client.execute(
            "terminal_exec", {**args, "command": "other"}, request_id="original", source=c.source
        )
    runs = await c.local.list_runs(c.env.person, device_id=c.device_id)
    assert [r["run_id"] for r in runs["items"]] == [started["run_id"]]
    await c.service.close()


async def test_completion_is_stable_across_output_cursor_and_replayed_exec_offline(
    database, tmp_path
):
    c = await setup_device(database, tmp_path)
    args = {"device_id": c.device_id, "command": "exit 0"}
    started = await c.local.execute("terminal_exec", args, request_id="finish", source=c.source)
    completion = {
        "run_id": started["run_id"],
        "status": "succeeded",
        "pending": False,
        "exit_code": 0,
    }
    c.connection.request.return_value = {
        "run_id": started["run_id"],
        "output": "tail",
        "next_cursor": 20,
        "completion": completion,
    }
    c.connection.request.side_effect = None
    await c.local.read(c.env.person, started["run_id"], 16)
    await c.local.receive(
        c.device_id, {"request_id": "finish", "run_id": started["run_id"], "result": completion}
    )
    c.service.connections.detach(c.connection)
    assert (
        await c.local.execute("terminal_exec", args, request_id="finish", source=c.source)
        == completion
    )


async def test_unknown_control_queries_original_id_without_redispatch(database, tmp_path):
    c = await setup_device(database, tmp_path)
    started = await c.local.execute(
        "terminal_exec",
        {"device_id": c.device_id, "command": "sleep"},
        request_id="long",
        source=c.source,
    )
    c.connection.request.side_effect = DeviceOffline("reply_lost")
    result = await c.local.control(c.env.person, started["run_id"], "original-control", "cancel")
    assert result["uncertain"] and result["request_id"] == "original-control"
    c.connection.request.side_effect = None
    c.connection.request.return_value = {"status": "not_found", "pending": True}
    await c.local.control_receipt(c.env.person, started["run_id"], "original-control")
    command = c.connection.request.call_args.args[0]
    assert command["action"] == "get"
    assert command["arguments"]["operation_id"].endswith(":original-control")
    await c.service.close()


async def test_local_read_rejects_another_person_even_with_known_run(database, tmp_path):
    c = await setup_device(database, tmp_path)
    started = await c.local.execute(
        "terminal_exec",
        {"device_id": c.device_id, "command": "private"},
        request_id="private",
        source=c.source,
    )
    from yuki.identity.canonical_repository import find_identity_binding

    async with database.read_session() as reader:
        other = await find_identity_binding(reader, "1002")
    with pytest.raises(ClientError):
        await c.local.read(other.person_id, started["run_id"])
    await c.service.close()


async def test_selected_local_file_bytes_and_version_release(database, tmp_path):
    c = await setup_device(database, tmp_path)
    data = b"actual local selected bytes"
    metadata = {
        "transfer_id": "selected:stat",
        "path": "C:/selected",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "version": "original-fd",
    }
    commands = []

    async def file_command(command):
        commands.append(command)
        if command["action"] == "stat":
            return metadata
        if command["action"] == "read":
            return {"data_base64": base64.b64encode(data).decode(), "eof": True}
        return {"released": True}

    c.connection.request.side_effect = file_command
    async with c.local.download(
        c.device_id, c.env.person, "C:/selected", request_id="selected"
    ) as (path, result):
        assert path.read_bytes() == data and result["version"] == "original-fd"
    assert commands[-1]["action"] == "release" and not path.exists()
    with pytest.raises(WorkspaceError, match="version_conflict"):
        async with c.local.download(
            c.device_id, c.env.person, "C:/selected", request_id="selected", expected_version="new"
        ):
            pytest.fail("must not yield a changed selection")
    assert commands[-1]["action"] == "release"
    await c.service.close()


@pytest.mark.parametrize("change", ["truncate", "grow"])
async def test_upload_source_change_stops_before_repeating_an_eof_chunk(database, tmp_path, change):
    c = await setup_device(database, tmp_path)
    source = tmp_path / "selected-upload.bin"
    source.write_bytes(b"a" * 140000)
    writes = []

    async def write_then_change(command):
        assert command["resource"] == "file" and command["action"] == "write"
        writes.append(command)
        source.write_bytes(b"" if change == "truncate" else b"b" * 200000)
        return {"pending": True}

    c.connection.request.side_effect = write_then_change
    try:
        with pytest.raises(WorkspaceError, match="file_changed_during_transfer"):
            await c.local.upload(
                c.device_id, c.env.person, source, "saved.bin", request_id="original-upload"
            )
        assert len(writes) == 1
        assert writes[0]["arguments"]["offset"] == 0
        assert writes[0]["arguments"]["final"] is False
        assert len(base64.b64decode(writes[0]["arguments"]["data_base64"])) == 65536
    finally:
        await c.service.close()


async def test_inflight_control_id_cannot_share_a_receipt_with_another_original_run():
    socket = SimpleNamespace(send_json=AsyncMock())
    connection = DeviceConnection("original-device", socket)
    original = {
        "id": "original-control",
        "resource": "terminal",
        "action": "cancel",
        "run_id": "run-a",
        "arguments": {},
    }
    first = asyncio.create_task(connection.request(original))
    await asyncio.sleep(0)
    try:
        with pytest.raises(ValueError, match="idempotency_conflict"):
            await asyncio.wait_for(connection.request({**original, "run_id": "run-b"}), 1)
        assert socket.send_json.await_count == 1
    finally:
        connection.respond(
            {"id": "original-control", "result": {"run_id": "run-a", "status": "cancelled"}}
        )
        result = await first
        assert result["run_id"] == "run-a"


@pytest.mark.parametrize("journal_shape", [False, True])
async def test_local_explicit_refusal_is_failed_not_forever_unknown(
    database, tmp_path, journal_shape
):
    c = await setup_device(database, tmp_path)
    c.connection.request.side_effect = None
    c.connection.request.return_value = {
        "error": {"code": "full_access_disabled", "executed": False},
        "executed": False,
        "pending": False,
    }
    if journal_shape:
        c.connection.request.return_value = {
            "status": "failed",
            "admitted": False,
            "pending": False,
            "error": {"code": "full_access_disabled", "executed": False},
        }
    result = await c.local.execute(
        "terminal_exec",
        {"device_id": c.device_id, "command": "must not execute"},
        request_id="refused",
        source=c.source,
    )
    assert result["status"] == "failed" and not result["pending"]
    row = await c.service.tasks.get("refused")
    assert row.status == "completed" and result["completion"]["executed"] is False
    await c.service.close()


async def test_lost_file_write_keeps_original_transfer_unknown_without_next_chunk(
    database, tmp_path
):
    c = await setup_device(database, tmp_path)
    source = tmp_path / "selected-source.bin"
    source.write_bytes(b"a" * 140000)
    c.connection.request.side_effect = DeviceOffline("reply_lost")
    try:
        result = await c.local.upload(
            c.device_id, c.env.person, source, "saved.bin", request_id="original-file-transfer"
        )
        assert result["status"] == "unknown" and result["uncertain"]
        assert result["transfer_id"] == "original-file-transfer"
        assert c.connection.request.await_count == 1
        prior = await c.local.upload_result(
            c.device_id, c.env.person, "saved.bin", request_id="original-file-transfer"
        )
        assert prior["uncertain"] and prior["transfer_id"] == "original-file-transfer"
        assert c.connection.request.call_args.args[0]["action"] == "get"
    finally:
        await c.service.close()


async def test_explicit_empty_device_never_falls_back_to_the_server(database, tmp_path):
    c = await setup_device(database, tmp_path)
    client = SandboxClient(tmp_path / "unused-manager", tasks=c.service.tasks)
    client.local_devices = c.local
    try:
        with pytest.raises(ClientError):
            await client.execute(
                "terminal_exec",
                {"device_id": "", "command": "must not dispatch"},
                request_id="empty-device",
                source=c.source,
            )
        assert c.connection.request.await_count == 0
        assert await c.service.tasks.get("empty-device") is None
    finally:
        await c.service.close()
