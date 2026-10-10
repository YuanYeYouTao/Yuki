"""Observed terminal state is separate from this Work's execution dependencies."""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from tests.support.work_effect_results_helpers import execute, owned_session
from tests.support.work_session import invoke_tool

from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.domain.messages import ToolCall, ToolFunction
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel
from qq_ai_bot.sandbox.persistent import PersistentManager
from qq_ai_bot.workspace.store import WorkspaceStore

OWNED = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
EXTERNAL = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


async def ready(database, tmp_path):
    control, session, store = await owned_session(database, tmp_path)
    await execute(
        session,
        store,
        "business-confirmed",
        ToolExecutionResult(
            ok=True,
            data={"changed": True},
            mutation_committed=True,
            provider_id="core",
            tool_name="business_write",
        ),
        side_effecting=True,
    )
    return control, session, store


async def complete(control):
    return json.loads(await control.execute("task_control", {"action": "complete"}, "complete"))


async def launch(session, store, *, tool_name="terminal_exec"):
    await execute(
        session,
        store,
        "launch-owned",
        ToolExecutionResult(
            ok=True,
            data={"run_id": OWNED, "pending": True, "status": "running"},
            mutation_committed=True,
            provider_id="core",
            tool_name=tool_name,
        ),
        side_effecting=True,
    )
    now = datetime.now(UTC)
    async with session.control.repository.database.immediate_session() as writer:
        writer.add(
            SandboxTaskRunModel(
                request_id=session.call_key("launch-owned"),
                source_conversation_id=session.control.lease.conversation_id,
                source_json=json.dumps(
                    {
                        "work_id": session.control.current["id"],
                        "conversation_id": session.control.lease.conversation_id,
                        "generation": session.control.lease.generation,
                    }
                ),
                payload_hash="0" * 64,
                run_id=OWNED,
                status="waiting",
                created_at=now,
                updated_at=now,
            )
        )


async def original_launch(database, session):
    async with database.sessions() as reader:
        receipt = await reader.scalar(
            select(effects.c.receipt_json)
            .where(effects.c.effect_key == session.call_key("launch-owned"))
            .limit(1)
        )
    return json.loads(receipt)["outcome"]


@pytest.mark.parametrize("tool", ["get_code_run", "terminal_read"])
@pytest.mark.parametrize("status", ["running", "unknown"])
async def test_external_read_preserves_observation_without_owning_execution(
    database, tmp_path, tool, status
):
    control, session, store = await ready(database, tmp_path)
    await execute(
        session,
        store,
        "read-external",
        ToolExecutionResult(
            ok=True,
            data={"run_id": EXTERNAL, "status": status},
            mutation_committed=False,
            provider_id="core",
            tool_name=tool,
        ),
        side_effecting=False,
    )
    observation = next(item for item in await control.effect_evidence() if item["tool"] == tool)
    assert observation["side_effecting"] is False
    assert observation["status"] == status
    assert observation["pending"] is (status == "running")
    assert observation["uncertain"] is (status == "unknown")
    await control.reconcile_completed_children()
    resumed = WorkControl(control.repository, control.lease, "result-test", {}, control.validate)
    resumed.current = await control.repository.get(control.current["id"])
    assert (await complete(resumed))["ok"] is True


async def test_reading_own_running_execution_does_not_remove_original_dependency(
    database, tmp_path
):
    control, session, store = await ready(database, tmp_path)
    await launch(session, store)
    await execute(
        session,
        store,
        "read-owned",
        ToolExecutionResult(
            ok=True,
            data={"run_id": OWNED, "status": "running", "pending": True},
            mutation_committed=False,
            provider_id="core",
            tool_name="get_code_run",
        ),
        side_effecting=False,
    )
    assert (await original_launch(database, session))["pending"] is True
    assert (await complete(control))["ok"] is True
    await control.settle(pending_inputs=False)
    assert control.current["state"] == "waiting_external"
    assert control.accepted["action"] == "complete"

    from qq_ai_bot.runtime.work_activation import activate_work
    from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository

    original = await control.repository.get(control.current["id"])
    request_id = session.call_key("launch-owned")
    await SandboxTaskRepository(database).receive(
        {
            "request_id": request_id,
            "run_id": OWNED,
            "result": {"run_id": OWNED, "status": "succeeded", "pending": False, "exit_code": 0},
        }
    )
    await control.repository.route_child_completion(request_id)
    await control.repository.release(control.lease)

    async def validate_resume():
        assert await control.repository.valid(resumed.lease)

    async with activate_work(
        control.repository,
        original["conversation_id"],
        original["generation"],
        original["source_key"],
        control.source,
        validate_resume,
        work_id=original["id"],
    ) as resumed:
        assert resumed.accepted["action"] == "complete"
        await resumed.reconcile_completed_children()
        await resumed.settle(pending_inputs=bool(await resumed.pending()))
        assert resumed.current["id"] == original["id"]
        assert resumed.current["state"] == "completed"
        assert resumed.current["source_json"] == original["source_json"]
        assert (resumed.current["model_requests"], resumed.current["tool_calls"]) == (
            original["model_requests"],
            original["tool_calls"],
        )
        receipt = await original_launch(database, session)
        assert receipt["run_id"] == OWNED and not receipt["pending"]


async def test_reconnecting_control_cannot_settle_a_still_running_execution(database, tmp_path):
    control, session, store = await ready(database, tmp_path)
    await launch(session, store)
    # Real Manager error branch; only the physical process/connection storage is fake.
    manager_state = SimpleNamespace(
        get=lambda _: {"run_id": OWNED, "status": "running", "pending": True}, connections={}
    )
    response = await PersistentManager.control(manager_state, OWNED, "interrupt")
    assert response["error"] == "terminal_reconnecting"
    assert "status" not in response and "pending" not in response
    await execute(
        session,
        store,
        "failed-control",
        ToolExecutionResult(
            ok=False,
            data=response,
            error_code=response["error"],
            mutation_committed=False,
            provider_id="core",
            tool_name="terminal_control",
        ),
        side_effecting=True,
    )
    after = await original_launch(database, session)
    assert after["status"] == "running" and after["pending"] is True
    assert after["mutation_committed"] is True
    assert (await complete(control))["ok"] is True
    await control.settle(pending_inputs=False)
    assert control.current["state"] == "waiting_external"
    assert control.accepted["action"] == "complete"


@pytest.mark.skipif(os.name != "posix", reason="Manager/Supervisor runtime requires POSIX")
@pytest.mark.parametrize("kind", ["terminal_exec", "environment_packages"])
@pytest.mark.parametrize("already_started", [False, True])
async def test_manager_restores_dispatched_run_without_replaying_request(
    database, tmp_path, monkeypatch, kind, already_started
):
    import signal

    from qq_ai_bot.runtime.subagent_repository import SubagentRepository
    from qq_ai_bot.sandbox import persistent
    from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
    from qq_ai_bot.services.subagent_execution import SubagentExecution

    control, session, store = await ready(database, tmp_path)
    request_id = session.call_key("launch-owned")
    args = (
        {"command": "printf once >> executions; sleep 300", "cwd": str(tmp_path)}
        if kind == "terminal_exec"
        else {"action": "repair"}
    )
    manager_args = (
        tmp_path / "manager",
        WorkspaceStore(tmp_path / "artifacts"),
        "test-image",
        "test-network",
        "",
        tmp_path / "home",
    )
    manager = PersistentManager(*manager_args, testing=True)
    manager.ready, manager.container_id = True, "original-container"
    transport = SimpleNamespace(
        request=AsyncMock(return_value={"session_id": EXTERNAL, "running": False}),
        connect=AsyncMock(side_effect=[TimeoutError("handshake lost"), AsyncMock()]),
    )
    command = AsyncMock(return_value=(0, b""))
    manager.execd = transport
    monkeypatch.setattr(manager, "command", command)
    monkeypatch.setattr(persistent, "uuid4", lambda: UUID(OWNED))

    async def observe(identity):
        return manager.get(identity)

    monkeypatch.setattr(manager, "wait_result", observe)
    process = None
    try:
        assert (await manager.submit(kind, args, request_id))["run_id"] == OWNED
        await launch(session, store, tool_name=kind)
        budget = await control.repository.get(control.current["id"])
        if kind == "terminal_exec":
            with pytest.raises(TimeoutError, match="handshake lost"):
                await manager.launch(OWNED)
        else:
            await manager.launch(OWNED)
        state = manager.state_path(OWNED)
        if already_started:
            (state / "started").touch()
            (state / "status.json").write_text(json.dumps({"status": "running", "heartbeat": 0}))
        with manager.db:
            manager.db.execute("UPDATE jobs SET created=created-3600 WHERE id=?", (OWNED,))
        original = dict(manager.active()[0])
        await manager.close()
        manager = PersistentManager(*manager_args, testing=True)
        manager.container_id, manager.execd = "original-container", transport
        monkeypatch.setattr(manager, "command", command)
        response = await manager.control(OWNED, "interrupt")
        assert response["error"] == "terminal_reconnecting"
        await manager.reconcile(manager.active()[0])
        assert manager.get(OWNED)["status"] == "running"
        assert dict(manager.active()[0]) == original
        assert (await manager.submit(kind, args, request_id))["run_id"] == OWNED
        if kind == "terminal_exec":
            assert transport.request.await_count == 1
            assert transport.request.await_args.args[:2] == ("POST", "/pty")
            assert [call.args for call in transport.connect.await_args_list] == [
                (EXTERNAL,),
                (EXTERNAL,),
            ]
        else:
            assert command.await_count == 1
            assert command.await_args.args[:3] == ("docker", "exec", "-d")
            transport.request.assert_not_awaited()
            transport.connect.assert_not_awaited()
        assert not manager.completions.pending()["events"]
        assert await control.repository.get(control.current["id"]) == budget
        assert (await original_launch(database, session))["pending"] is True

        if kind == "environment_packages":
            # The next native receipt, rather than age/heartbeat, ends this same run.
            (state / "status.json").write_text(json.dumps({"status": "failed", "exit_code": 7}))
            await manager.reconcile(manager.active()[0])
            receipt = manager.completions.pending()["events"][0]
            assert receipt["request_id"] == request_id and receipt["run_id"] == OWNED
            assert receipt["result"]["status"] == "failed"
            assert receipt["result"]["exit_code"] == 7 and receipt["result"]["pending"] is False
            assert command.await_count == 1
            return

        # Execute the real Supervisor in an isolated runtime directory. An attach
        # can invoke the wrapper again; its existing started file prevents a second command.
        wrapper = (
            "import sys; from pathlib import Path; "
            "from qq_ai_bot.sandbox import environment_supervisor as s; "
            "root, job = Path(sys.argv[1]), sys.argv[2]; "
            "s.Path = lambda value: root if value == '/var/lib/yuki-runtime' else Path(value); "
            "sys.argv = ['supervisor', job]; raise SystemExit(s.main())"
        )
        executions = tmp_path / "executions"
        for replay in (False, True):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                wrapper,
                str(manager.runtime_root),
                OWNED,
                stdin=asyncio.subprocess.DEVNULL,
            )
            if not already_started and not replay:
                async with asyncio.timeout(10):
                    while True:
                        started = manager.status_record(OWNED)
                        if started.get("status") == "running" and executions.exists():
                            break
                        await asyncio.sleep(0.01)
                assert started["supervisor_pid"] == process.pid
                os.kill(started["pid"], 0)
                assert executions.read_text() == "once"
                cancelled = json.loads(
                    await control.execute("task_control", {"action": "cancel"}, "cancel-owned")
                )
                assert cancelled["ok"]
                await control.settle(pending_inputs=False)
                assert control.current["state"] == "cancelled"

                async def cancel(method, arguments, *, request_id):
                    assert method == "cancel_code_run" and arguments["run_id"] == OWNED
                    assert request_id == f"worker-cancel:{OWNED}"
                    return await manager.control(OWNED, "cancel")

                execution = SubagentExecution(
                    control.repository,
                    SubagentRepository(control.repository),
                    SimpleNamespace(sandbox_client=SimpleNamespace(execute=cancel)),
                )
                await execution.cancel_commands()
                assert (state / "cancel").is_file()
                async with asyncio.timeout(10):
                    await process.wait()
                final = manager.status_record(OWNED)
                assert final["status"] == "cancelled" and final["exit_code"] == -signal.SIGTERM
                assert final["finished_at"] - final["started_at"] < 300
                await manager.reconcile(manager.active()[0])
                receipt = manager.completions.pending()["events"][0]
                assert (
                    receipt["run_id"] == OWNED and receipt["request_id"] == original["request_id"]
                )
                tasks = SandboxTaskRepository(database)
                await tasks.receive(receipt)
                await tasks.receive(receipt)
                await control.repository.route_child_completion(receipt["request_id"])
                await control.reconcile_completed_children()
                retained = await control.repository.get(control.current["id"])
                assert (
                    retained["state"] == "cancelled"
                    and retained["source_json"] == budget["source_json"]
                )
                assert (retained["model_requests"], retained["tool_calls"]) == (
                    budget["model_requests"],
                    budget["tool_calls"],
                )
                after = await original_launch(database, session)
                assert after["run_id"] == OWNED and after["status"] == "cancelled"
                assert not after["pending"] and after["mutation_committed"]
                continue
            async with asyncio.timeout(10):
                assert await process.wait() == 125
        assert not executions.exists() if already_started else executions.read_text() == "once"
    finally:
        if process is not None and process.returncode is None:
            (manager.state_path(OWNED) / "cancel").touch()
            async with asyncio.timeout(10):
                await process.wait()
        await manager.close()


async def test_interrupted_read_is_visible_but_not_an_unknown_mutation(database, tmp_path):
    control, session, store = await ready(database, tmp_path)
    call = ToolCall(id="read-error", type="function", function=ToolFunction("read_status", "{}"))

    async def fail():
        raise RuntimeError("readonly invocation interrupted before a typed result")

    with pytest.raises(RuntimeError):
        await invoke_tool(session, call, fail, side_effecting=False)
    visible = next(
        item for item in await control.effect_evidence() if item["tool"] == "read_status"
    )
    assert visible["side_effecting"] is False and visible["uncertain"] is True
    following = await execute(
        session,
        store,
        "following-mutation",
        ToolExecutionResult(ok=True, mutation_committed=True, tool_name="business_write"),
        side_effecting=True,
    )
    assert json.loads(following)["ok"] is True
    assert (await complete(control))["ok"] is True


@pytest.mark.parametrize("status,exit_code", [("succeeded", 0), ("failed", 1), ("cancelled", 1)])
async def test_terminal_receipt_preserves_original_committed_operation(
    database, tmp_path, status, exit_code
):
    _control, session, store = await ready(database, tmp_path)
    await launch(session, store)
    await execute(
        session,
        store,
        "terminal-read-final",
        ToolExecutionResult(
            ok=True,
            data={"run_id": OWNED, "pending": False, "status": status, "exit_code": exit_code},
            mutation_committed=False,
            provider_id="core",
            tool_name="terminal_read",
        ),
        side_effecting=False,
    )
    after = await original_launch(database, session)
    assert after["side_effecting"] is True and after["tool"] == "terminal_exec"
    assert after["mutation_committed"] is True and after["pending"] is False
    assert after["ok"] is (status == "succeeded")


async def test_unknown_mutation_remains_unknown_without_vetoing_new_call_or_completion(
    database, tmp_path
):
    control, session, store = await ready(database, tmp_path)
    await execute(
        session,
        store,
        "unknown-write",
        ToolExecutionResult(
            ok=False, uncertain=True, provider_id="plugin", tool_name="external_write"
        ),
        side_effecting=True,
    )
    async with database.sessions() as reader:
        original = dict(
            (
                await reader.execute(
                    select(effects).where(effects.c.effect_key == session.call_key("unknown-write"))
                )
            )
            .mappings()
            .one()
        )
    following = await execute(
        session, store, "following", ToolExecutionResult(ok=True, tool_name="write")
    )
    assert json.loads(following)["ok"] is True
    assert (await complete(control))["ok"] is True
    await control.settle(pending_inputs=False)
    assert control.current["state"] == "completed"
    async with database.sessions() as reader:
        retained = dict(
            (
                await reader.execute(
                    select(effects).where(effects.c.effect_key == session.call_key("unknown-write"))
                )
            )
            .mappings()
            .one()
        )
    assert retained == original
    assert json.loads(retained["receipt_json"])["outcome"]["uncertain"] is True


@pytest.mark.parametrize("tool", ["get_code_run", "terminal_read"])
async def test_external_running_read_through_real_kernel_does_not_block_completion(
    database, tmp_path, monkeypatch, tool
):
    from tests.support.sandbox_submission_unknown_lifecycle_helpers import (
        host_case,
        invoke,
        socket_receipts,
    )

    wire = socket_receipts(
        monkeypatch,
        [
            {"run_id": OWNED, "status": "succeeded", "pending": False, "exit_code": 0},
            {"run_id": EXTERNAL, "status": "running", "pending": True},
        ],
    )
    work, tasks, backend, runtime = await host_case(database, tmp_path)
    await invoke(
        work,
        backend,
        runtime,
        ToolCall("owned-completed", ToolFunction("terminal_exec", '{"command":"printf offline"}')),
    )
    result = await invoke(
        work,
        backend,
        runtime,
        ToolCall("external-status", ToolFunction(tool, json.dumps({"run_id": EXTERNAL}))),
    )
    public = json.loads(result.calls[0][1])
    assert public["ok"] is True and public["process"]["pending"] is True
    assert public["mutation_committed"] is False
    assert [item["method"] for item in wire] == ["terminal_exec", tool]
    # The read does not create an owned SandboxTask for the observed run.
    assert await tasks.get(wire[1]["request_id"]) is None
    assert (await complete(work.control))["ok"] is True
    await work.control.settle(pending_inputs=False)
    assert work.control.current["state"] == "completed"
