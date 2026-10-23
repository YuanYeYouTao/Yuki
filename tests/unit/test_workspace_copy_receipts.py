"""A direct cache copy keeps its original file receipt across lost observations."""

import asyncio
import hashlib
import json
import os
import threading
from types import SimpleNamespace

import pytest
from tests.conftest import make_settings
from tests.support.service_dependencies import tool_dependencies, workspace_service
from tests.support.workspace_snapshots import artifact_ids

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.capabilities.models import CapabilityEffect
from yuki.capabilities.results import resolve_mutation_commit
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.service import MemoryFactService
from yuki.persistence.repositories import EventLedgerRepository
from yuki.sandbox.client import SandboxClient
from yuki.sandbox.persistent import PersistentManager
from yuki.services.agent_tools import AgentToolService, ToolRuntime
from yuki.workspace.store import WorkspaceStore

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Manager safe workspace FDs require POSIX"
)


def manager_at(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    manager = PersistentManager(
        tmp_path / "manager",
        WorkspaceStore(tmp_path / "artifacts"),
        "test-image",
        "test-network",
        "",
        tmp_path / "home",
        testing=True,
        media_cache=cache,
    )
    manager.files.root.mkdir(parents=True, exist_ok=True)
    return manager, cache


def copy_input(cache, *, name="original.txt", destination="target.txt", key="attachment:101:0"):
    content = b"original selected bytes"
    (cache / name).write_bytes(content)
    args = {
        "path": "/inputs/media-cache/" + name,
        "destination": destination,
        "expected_version": hashlib.sha256(content).hexdigest(),
        "source_key": key,
    }
    return args, content


async def save_tool_result(database, workspace, request_id):
    tools = AgentToolService(
        settings=make_settings(database.url),
        ledger=EventLedgerRepository(database),
        memories=MemoryFactService(MemoryFactRepository(database)),
        **tool_dependencies(database, make_settings(database.url)),
    )
    tools.workspace_service = workspace
    runtime = ToolRuntime(
        inbound=InboundMessage(
            "trigger",
            "message:private:normal",
            ScopeType.PRIVATE,
            SenderIdentity("1001"),
            "save the file",
            bot_user_id="8000",
        ),
        execution_id="execution-test",
        conversation_id="conversation-test",
        target_presence_id="presence-test",
    )
    token = current_invocation.set(ToolInvocationContext(runtime, request_id))
    try:
        return await tools.execute(
            "save_conversation_attachment_to_workspace",
            json.dumps({"event_id": 101, "attachment_index": 0, "destination": "target.txt"}),
            runtime,
        )
    finally:
        current_invocation.reset(token)


async def test_completed_copy_replays_before_source_or_destination_access(tmp_path, monkeypatch):
    manager, cache = manager_at(tmp_path)
    args, content = copy_input(cache)
    query = {"source_key": args["source_key"], "destination": args["destination"]}
    try:
        assert await manager.file_request("workspace_copy_result", query, "copy-101") == {
            "found": False
        }
        result = await manager.file_request("workspace_copy", args, "copy-101")
        assert result == {
            "path": "/workspace/target.txt",
            "version": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "external_untrusted": True,
        }
        assert artifact_ids(manager.store) == []
        (cache / "original.txt").unlink()
        (manager.files.root / "target.txt").write_bytes(b"user edit after successful copy")

        def no_replay(*_args):
            raise AssertionError("confirmed file effect must not execute again")

        monkeypatch.setattr(manager, "file_operation", no_replay)
        assert await manager.file_request("workspace_copy", args, "copy-101") == result
        found = await manager.file_request("workspace_copy_result", query, "copy-101")
        assert found["found"] is True
        assert found["result"] == result
        assert (
            manager.files.root / "target.txt"
        ).read_bytes() == b"user edit after successful copy"
        (manager.files.root / "target.txt").unlink()
        assert await manager.file_request("workspace_copy", args, "copy-101") == result
        assert not (manager.files.root / "target.txt").exists()
        for changed in (
            query | {"source_key": "attachment:102:0"},
            query | {"destination": "different.txt"},
        ):
            assert await manager.file_request("workspace_copy_result", changed, "copy-101") == {
                "error": "idempotency_conflict"
            }
        assert await manager.file_request(
            "workspace_copy", args | {"path": "/inputs/media-cache/other.txt"}, "copy-101"
        ) == {"error": "idempotency_conflict"}
    finally:
        await manager.close()


async def test_copy_interrupted_after_write_keeps_unknown_without_overwriting_user_file(
    database, tmp_path, monkeypatch
):
    manager, cache = manager_at(tmp_path)
    args, content = copy_input(cache)
    original = manager.file_operation
    calls = []

    def interrupted(method, values):
        calls.append(method)
        original(method, values)
        raise asyncio.CancelledError("receipt interrupted after physical write")

    try:
        monkeypatch.setattr(manager, "file_operation", interrupted)
        with pytest.raises(asyncio.CancelledError):
            await manager.file_request("workspace_copy", args, "interrupted-copy")
        assert (manager.files.root / "target.txt").read_bytes() == content
        (cache / "original.txt").unlink()
        (manager.files.root / "target.txt").write_bytes(b"later user content")
        monkeypatch.setattr(manager, "file_operation", original)
        result = await manager.file_request("workspace_copy", args, "interrupted-copy")
        assert result["error"] == "file_mutation_unknown_read_current_version"
        assert result["retryable"] is False and result["uncertain"] is True
        assert calls == ["workspace_copy"]
        found = await manager.file_request(
            "workspace_copy_result",
            {"source_key": args["source_key"], "destination": args["destination"]},
            "interrupted-copy",
        )
        assert found["found"] is True and found["result"]["error"] == result["error"]
        workspace = workspace_service(manager.store)
        workspace.sandbox = SimpleNamespace(execute=manager.file_request)
        outcome = await save_tool_result(database, workspace, "interrupted-copy")
        assert outcome.ok is False and outcome.error_code == result["error"]
        assert outcome.data == result and outcome.uncertain is True
        assert outcome.mutation_committed is None
        assert (
            resolve_mutation_commit(outcome, SimpleNamespace(effect=CapabilityEffect.WRITE_STATE))
            is None
        )
        assert (manager.files.root / "target.txt").read_bytes() == b"later user content"
        assert calls == ["workspace_copy"]
    finally:
        await manager.close()


async def test_save_cannot_turn_unavailable_receipt_query_into_confirmed_no_write(
    database, tmp_path
):
    manager, cache = manager_at(tmp_path)
    args, content = copy_input(cache)
    try:
        await manager.file_request("workspace_copy", args, "original-copy")
        assert (manager.files.root / "target.txt").read_bytes() == content
        workspace = workspace_service(manager.store)
        workspace.sandbox = SandboxClient(tmp_path / "offline-manager.sock")
        outcome = await save_tool_result(database, workspace, "original-copy")
        assert outcome.ok is False and outcome.error_code == "sandbox_unavailable"
        assert outcome.uncertain is True and outcome.mutation_committed is None
        assert (manager.files.root / "target.txt").read_bytes() == content
    finally:
        await manager.close()


async def test_cancelled_observation_waits_for_physical_copy_and_persists_success(
    tmp_path, monkeypatch
):
    manager, cache = manager_at(tmp_path)
    args, content = copy_input(cache)
    entered, release = threading.Event(), threading.Event()
    original = manager.file_operation

    def blocked(method, values):
        entered.set()
        release.wait()
        return original(method, values)

    monkeypatch.setattr(manager, "file_operation", blocked)
    task = asyncio.create_task(
        manager.file_request("workspace_copy", args, "cancelled-observation")
    )
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (manager.files.root / "target.txt").read_bytes() == content
        result = await manager.file_request("workspace_copy", args, "cancelled-observation")
        assert result["version"] == hashlib.sha256(content).hexdigest()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()


async def test_copy_result_waits_for_the_same_physical_request(tmp_path, monkeypatch):
    manager, cache = manager_at(tmp_path)
    args, _content = copy_input(cache)
    entered, release = threading.Event(), threading.Event()
    original = manager.file_operation
    calls = []

    def blocked(method, values):
        calls.append(method)
        entered.set()
        release.wait()
        return original(method, values)

    monkeypatch.setattr(manager, "file_operation", blocked)
    tasks = [asyncio.create_task(manager.file_request("workspace_copy", args, "same-copy"))]
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        tasks.append(asyncio.create_task(manager.file_request("workspace_copy", args, "same-copy")))
        tasks.append(
            asyncio.create_task(
                manager.file_request(
                    "workspace_copy_result",
                    {"source_key": args["source_key"], "destination": args["destination"]},
                    "same-copy",
                )
            )
        )
        await asyncio.sleep(0)
        assert not tasks[1].done() and not tasks[2].done()
        release.set()
        initial, repeated, found = await asyncio.gather(*tasks)
        assert initial == repeated and found["result"]["version"] == initial["version"]
        assert calls == ["workspace_copy"]
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()


@pytest.mark.parametrize("method", ["workspace_copy", "workspace_publish"])
async def test_socket_lost_ack_is_uncertain_and_queries_original_file_effect(tmp_path, method):
    manager, cache = manager_at(tmp_path)
    args, content = copy_input(cache)
    if method == "workspace_publish":
        args = {"path": args["path"]}
    socket = tmp_path / "manager.sock"
    connections = 0
    handlers = set()

    async def accept(reader, writer):
        nonlocal connections
        current = asyncio.current_task()
        handlers.add(current)
        connections += 1
        try:
            if connections == 1:
                request = json.loads(await reader.readline())
                result = await manager.handle(request)
                assert result["version"] == hashlib.sha256(content).hexdigest()
                writer.close()
                await writer.wait_closed()
            else:
                await manager.serve(reader, writer)
        finally:
            handlers.discard(current)

    server = await asyncio.start_unix_server(accept, str(socket))
    client = SandboxClient(socket)
    try:
        lost = await client.execute(method, args, request_id="socket-copy")
        assert lost["error"] == "sandbox_unavailable" and lost["uncertain"] is True
        (cache / "original.txt").unlink()
        if method == "workspace_copy":
            found = await client.execute(
                "workspace_copy_result",
                {"source_key": args["source_key"], "destination": args["destination"]},
                request_id="socket-copy",
            )
            assert found["found"] is True
            assert found["result"]["version"] == hashlib.sha256(content).hexdigest()
            assert (manager.files.root / "target.txt").read_bytes() == content
        else:
            published = await client.execute(method, args, request_id="socket-copy")
            assert published["version"] == hashlib.sha256(content).hexdigest()
            assert manager.store.read_bytes(published["artifact_id"])[1] == content
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*tuple(handlers), return_exceptions=True)
        await manager.close()


async def test_restart_preserves_original_copy_and_legacy_receipts(tmp_path):
    manager, cache = manager_at(tmp_path)
    args, content = copy_input(cache)
    result = await manager.file_request("workspace_copy", args, "copy-before-restart")
    legacy = ("original-old-id", "original-old-digest", '{"path":"/workspace/preserved.txt"}')
    with manager.db:
        manager.db.execute("INSERT INTO environment_mutations VALUES (?,?,?)", legacy)
    await manager.close()
    (cache / "original.txt").unlink()
    restarted, _cache = manager_at(tmp_path)
    try:
        assert await restarted.file_request("workspace_copy", args, "copy-before-restart") == result
        assert (restarted.files.root / "target.txt").read_bytes() == content
        assert (
            tuple(
                restarted.db.execute(
                    "SELECT * FROM environment_mutations WHERE request_id=?", (legacy[0],)
                ).fetchone()
            )
            == legacy
        )
    finally:
        await restarted.close()
