"""Host manager contracts without executing untrusted code on the test host."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from qq_ai_bot.sandbox.client import sandbox_tools
from qq_ai_bot.sandbox.manager import Manager
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


@pytest.mark.asyncio
async def test_sandbox_bounded_request_lifecycle_and_publication(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "workspace", capacity=8)
    manager = Manager(
        tmp_path / "jobs", store, "fixed-python:test", "internal", "http://proxy:3128"
    )
    request = {"method": "run_python", "request_id": "request-1", "args": {"code": "print(1)"}}
    assert (await manager.handle(request))["error"] == "sandbox_unavailable"
    manager.ready = True

    async def complete():
        identity = await manager.queue.get()
        manager.finish(identity, "succeeded", {"output": "1"})
        manager.queue.task_done()

    task = asyncio.create_task(complete())
    result = await manager.handle(request)
    await task
    assert result["status"] == "succeeded" and result["output"] == "1"
    assert await manager.handle(request) == result
    assert (await manager.handle({**request, "args": {"code": "print(2)"}}))[
        "error"
    ] == "idempotency_conflict"
    assert (await manager.handle({**request, "args": {"code": "", "timeout_seconds": 121}}))[
        "error"
    ] == "invalid_arguments"
    for _ in range(4):
        manager.queue.put_nowait(str(uuid4()))
    assert (await manager.handle({**request, "request_id": "full"}))[
        "error"
    ] == "sandbox_queue_full"
    command = manager.docker_args(str(uuid4()))
    assert command[command.index("--runtime") + 1] == "runsc"
    assert command[command.index("--memory-swap") + 1] == "256m"
    assert "--privileged" not in command and "docker.sock" not in " ".join(command)
    assert command[-3:] == ("fixed-python:test", "python", "/inputs/code.py")
    with pytest.raises(ValueError):
        manager.docker_args("../../escape")
    assert [tool.name for tool in sandbox_tools()] == [
        "run_python",
        "get_code_run",
        "cancel_code_run",
    ]
    with pytest.raises(WorkspaceError):
        store.publish_batch([("one", b"123"), ("../bad", b"4")])
    assert store.list()["items"] == []
    assert len(store.publish_batch([("one", b"123"), ("two", b"456")])) == 2
    with pytest.raises(WorkspaceError, match="workspace_full"):
        store.publish_batch([("three", b"123")])
    assert len(store.list()["items"]) == 2
    manager.db.close()
