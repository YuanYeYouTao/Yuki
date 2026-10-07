"""Host manager contracts without executing untrusted code on the test host."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from tests.support.sandbox_completion_cases import completion_delivery_cases, pending_job

from qq_ai_bot.sandbox.client import SandboxClient, sandbox_tools
from qq_ai_bot.sandbox.persistent import PersistentManager
from qq_ai_bot.workspace.store import WorkspaceStore


@pytest.mark.asyncio
async def test_sandbox_bounded_request_lifecycle_and_publication(
    tmp_path: Path, database, monkeypatch
) -> None:
    store = WorkspaceStore(tmp_path / "workspace", capacity=8)
    manager = PersistentManager(
        tmp_path / "jobs", store, "fixed:test", "internal", "proxy", tmp_path / "home"
    )
    monkeypatch.setattr(manager, "output", lambda identity: {})
    assert "run_python" not in {tool.name for tool in sandbox_tools()}
    assert (await manager.handle({"method": "run_python", "args": {"code": "pass"}}))[
        "error"
    ] == "unknown_method"
    identity = pending_job(manager)
    manager.finish(identity, "running", {})
    observers = [
        asyncio.create_task(
            manager.handle(
                {
                    "method": "get_code_run",
                    "args": {"run_id": identity},
                }
            )
        )
        for _ in range(2)
    ]
    await asyncio.sleep(0)
    assert not any(observer.done() for observer in observers)
    manager.finish(identity, "succeeded", {"artifacts": ["image"]})
    observed = await asyncio.gather(*observers)
    assert all(r["status"] == "succeeded" and r["artifacts"] == ["image"] for r in observed)
    assert all(r["pending"] is False for r in observed)
    assert manager._waiters == {}
    identity = pending_job(manager)
    manager.finish(identity, "running", {})
    waiting = await manager.wait_result(identity, wait_seconds=0.01)
    assert waiting["pending"] is True and waiting["status"] == "running"
    assert manager._waiters == {}
    abandoned = asyncio.create_task(manager.wait_result(identity))
    await asyncio.sleep(0)
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    assert manager.get(identity)["status"] == "running"
    assert manager._waiters == {}
    with manager.db:
        manager.db.execute(
            "INSERT INTO environment_jobs (id,kind) VALUES (?,?)", (identity, "terminal_exec")
        )
    waiting_task = asyncio.create_task(manager.wait_result(identity))
    await asyncio.sleep(0)
    await manager.handle({"method": "cancel_code_run", "args": {"run_id": identity}})
    assert (await waiting_task)["status"] == "cancelled"
    assert not manager.finish(identity, "succeeded", {"artifacts": ["late"]})
    assert manager.get(identity)["status"] == "cancelled"
    manager.db.close()
    await completion_delivery_cases(tmp_path / "completion-delivery")
    from tests.support.sandbox_task_cases import task_receipt_cases

    await task_receipt_cases(database, tmp_path / "bot-receipts")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["terminal_exec", "environment_packages"])
async def test_new_execution_tools_persist_source_before_dispatch_and_stage_canonical_receipt(
    tmp_path, monkeypatch, method
):
    import asyncio

    order = []
    identity = str(uuid4())
    result = {"run_id": identity, "pending": False, "status": "succeeded", "output": "full"}
    received = []
    row = SimpleNamespace(request_id="request", progress_json="{}")

    class Tasks:
        async def prepare(self, request_id, arguments, source):
            assert arguments["tool"] == method
            assert source == {"trusted": True}
            order.append("persist")

        async def bind_run(self, request_id, run_id):
            assert run_id == identity

        async def get(self, request_id):
            return row

        async def by_run(self, run_id):
            return row

        async def receive(self, event):
            received.append(event)

    class Stream:
        def write(self, data):
            assert order == ["persist"]
            order.append("dispatch")

        async def drain(self):
            pass

        async def readline(self):
            return json.dumps(result).encode() + b"\n"

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(*args, **kwargs):
        return Stream(), Stream()

    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    client = SandboxClient(tmp_path / "socket", tasks=Tasks())
    await client.execute(method, {}, request_id="request", source={"trusted": True})
    await client._stage_result(
        "terminal_read",
        "observer",
        {
            **result,
            "output": "different cursor",
            "completion": result,
        },
    )
    assert received[0] == received[1]
