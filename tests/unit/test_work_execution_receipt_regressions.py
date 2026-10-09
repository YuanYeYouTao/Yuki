"""Observed terminal state is separate from this Work's execution dependencies."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.support.work_effect_results_helpers import execute, owned_session
from tests.support.work_session import invoke_tool

from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.domain.messages import ToolCall, ToolFunction
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.sandbox.persistent import PersistentManager

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


async def launch(session, store):
    await execute(
        session,
        store,
        "launch-owned",
        ToolExecutionResult(
            ok=True,
            data={"run_id": OWNED, "pending": True, "status": "running"},
            mutation_committed=True,
            provider_id="core",
            tool_name="terminal_exec",
        ),
        side_effecting=True,
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
    assert not await control.has_unresolved_effects()
    resumed = WorkControl(control.repository, control.lease, "result-test", {}, control.validate)
    resumed.current = await control.repository.get(control.current["id"])
    assert not await resumed.has_unresolved_effects()
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
    assert await complete(control) == {"ok": False, "error": "work_has_unresolved_execution"}


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
    assert await complete(control) == {"ok": False, "error": "work_has_unresolved_execution"}


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
    assert not await control.has_unresolved_effects(pending=False)
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
    control, session, store = await ready(database, tmp_path)
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
    assert not await control.has_unresolved_effects()


async def test_unknown_mutation_blocks_following_mutations_and_completion(database, tmp_path):
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
    assert await control.has_unresolved_effects(pending=False)
    following = await execute(
        session, store, "following", ToolExecutionResult(ok=True, tool_name="write")
    )
    assert json.loads(following)["error_code"] == "unresolved_prior_effect"
    assert await complete(control) == {"ok": False, "error": "work_has_unresolved_execution"}


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
    assert not await work.control.has_unresolved_effects()
    assert (await complete(work.control))["ok"] is True
