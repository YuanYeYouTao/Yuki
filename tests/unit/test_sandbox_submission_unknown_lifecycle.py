"""Unconfirmed sandbox dispatch stays unknown through the real Host tool chain."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.conftest import build_harness, make_settings
from tests.unit.test_tool_effect_audit import active_work

from qq_ai_bot.capabilities.coordinator import ToolInvocationCoordinator
from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity, ToolCall, ToolFunction
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.sandbox.client import SandboxClient
from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
from qq_ai_bot.services.main_agent_backend import MainAgentBackend


def socket_receipts(monkeypatch, responses):
    """Only transport is fake; no socket, model or terminal process is opened."""
    wire = []
    replies = iter(responses)

    class Writer:
        def write(self, data):
            wire.append(json.loads(data))

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(*args, **kwargs):
        reply = next(replies)
        read = (
            AsyncMock(side_effect=reply)
            if isinstance(reply, Exception)
            else AsyncMock(return_value=json.dumps(reply).encode() + b"\n")
        )
        return SimpleNamespace(readline=read), Writer()

    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    return wire


async def host_case(database, tmp_path):
    env, work, tool_runtime = await active_work(database, tmp_path)
    chat = build_harness(database, make_settings(database.url)).processor._chat
    tasks = SandboxTaskRepository(database)
    chat._tools.sandbox_client = SandboxClient(tmp_path / "never-opened.sock", tasks=tasks)
    inbound = InboundMessage(
        message_id="offline-rpc-fixture",
        event_type="message:test",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text="offline",
        bot_user_id="80001",
        group_id="20001",
        person_id=env.person,
        space_id=env.space,
        conversation_id=env.context.conversation_id,
    )
    tool_runtime = replace(
        tool_runtime,
        inbound=inbound,
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        current_group_id="20001",
        space_id=env.space,
        runtime_config=await chat._runtime_config.snapshot(),
    )
    backend = MainAgentBackend(chat, tool_runtime)
    runtime = SimpleNamespace(
        work_control=work.control, origin=TurnOrigin.USER_MESSAGE, delegated_authority=None
    )
    await backend.prepare()
    backend.definitions(runtime, web_was_used=False)
    return work, tasks, backend, runtime


async def invoke(work, backend, runtime, call):
    backend.begin_batch((call,), runtime)
    token = current_work_control.set(work.control)
    try:
        return await ToolInvocationCoordinator().execute_batch(
            (call,), backend, runtime, remaining_calls=1, max_parallel_calls=1
        )
    finally:
        current_work_control.reset(token)


async def effect_receipt(database, work, call):
    async with database.sessions() as reader:
        return await reader.scalar(
            select(effects.c.receipt_json).where(effects.c.effect_key == work.call_key(call.id))
        )


def terminal_receipt(request_id, status):
    run_id = str(uuid4())
    result = {
        "run_id": run_id,
        "status": status,
        "pending": False,
        "exit_code": {"succeeded": 0, "failed": 1, "cancelled": -15}[status],
    }
    return {"request_id": request_id, "run_id": run_id, "result": result}


@pytest.mark.parametrize(
    "name,args",
    [
        ("terminal_exec", {"command": "printf offline"}),
        ("run_python", {"code": "print('offline')"}),
        ("environment_packages", {"action": "install", "packages": ["fixture"]}),
    ],
)
async def test_unconfirmed_submission_keeps_unknown_fence_identity_and_attempt_budget(
    database, tmp_path, monkeypatch, name, args
):
    wire = socket_receipts(monkeypatch, [OSError("lost first receipt"), OSError("lost lookup")])
    work, tasks, backend, runtime = await host_case(database, tmp_path)
    call = ToolCall("original-submission", ToolFunction(name, json.dumps(args)))
    result = await invoke(work, backend, runtime, call)
    assert [item["method"] for item in wire] == [name, "get_code_run_by_request"]
    request_id = wire[0]["request_id"]
    assert wire[1]["request_id"] == wire[1]["args"]["request_id"] == request_id
    public = json.loads(result.calls[0][1])
    assert public["provider_id"] == "core" and public["tool_name"] == name
    assert public["ok"] is False and public["uncertain"] is True
    assert public["error_code"] == "sandbox_submission_unknown"
    assert public.get("mutation_committed") is None and public["retryable"] is False
    assert public["data"]["request_id"] == request_id
    assert public["data"]["uncertain"] is True
    async with database.sessions() as reader:
        row = (
            (
                await reader.execute(
                    select(effects).where(effects.c.effect_key == work.call_key(call.id))
                )
            )
            .mappings()
            .one()
        )
    fact = json.loads(row["receipt_json"])["outcome"]
    task = await tasks.get(request_id)
    assert task is not None and task.status == "waiting" and task.run_id is None
    source = json.loads(task.source_json)
    assert source["work_id"] == work.control.current["id"]
    assert source["generation"] == work.control.lease.generation
    assert row["state"] == "accepted" and fact["side_effecting"] is True
    assert fact["ok"] is False and fact["uncertain"] is True
    assert fact["mutation_committed"] is None
    assert await work.control.has_unresolved_effects()
    assert work.control.tools_started == 1
    following = ToolCall(
        "different-mutation", ToolFunction("terminal_exec", '{"command":"printf different"}')
    )
    blocked = await invoke(work, backend, runtime, following)
    assert blocked.executed_count == 0 and blocked.calls[0][2] is False
    assert json.loads(blocked.calls[0][1])["error"] == "unresolved_prior_effect"
    assert len(wire) == 2 and work.control.tools_started == 1
    completion = json.loads(
        await work.control.execute("task_control", {"action": "complete"}, "complete-after-unknown")
    )
    assert completion["ok"] is False and completion["error"] == "work_has_unresolved_execution"
    # Original cumulative budget, receipt and source survive a new control.
    resumed = WorkControl(
        work.control.repository,
        work.control.lease,
        work.control.source_key,
        work.control.source,
        work.control.validate,
    )
    resumed.current = await resumed.repository.get(work.control.current["id"])
    assert resumed.current["tool_calls"] == 1 and await resumed.has_unresolved_effects()
    assert (await tasks.get(request_id)).source_json == task.source_json


@pytest.mark.parametrize("status", ["running", "succeeded", "failed"])
async def test_original_request_lookup_recovers_the_same_run_without_resubmission(
    database, tmp_path, monkeypatch, status
):
    run_id = str(uuid4())
    recovered = {"run_id": run_id, "status": status, "pending": status == "running"}
    if status != "running":
        recovered["exit_code"] = 0 if status == "succeeded" else 1
    wire = socket_receipts(monkeypatch, [OSError("lost first receipt"), recovered])
    work, tasks, backend, runtime = await host_case(database, tmp_path)
    call = ToolCall(
        "recover-original", ToolFunction("terminal_exec", '{"command":"printf offline"}')
    )
    result = await invoke(work, backend, runtime, call)
    assert [item["method"] for item in wire] == ["terminal_exec", "get_code_run_by_request"]
    request_id = wire[0]["request_id"]
    assert wire[1]["request_id"] == request_id
    public = json.loads(result.calls[0][1])
    assert public["ok"] is True and not public.get("uncertain")
    assert public["data"]["run_id"] == run_id
    task = await tasks.get(request_id)
    assert task.run_id == run_id
    assert task.status == ("waiting" if status == "running" else "completed")
    assert work.control.tools_started == 1
    assert await work.control.has_unresolved_effects() == (status == "running")


async def test_known_admission_rejection_is_not_promoted_to_submission_unknown(
    database, tmp_path, monkeypatch
):
    rejection = {"error": "host_memory_pressure", "retryable": False}
    wire = socket_receipts(monkeypatch, [rejection])
    work, tasks, backend, runtime = await host_case(database, tmp_path)
    call = ToolCall(
        "known-rejection", ToolFunction("terminal_exec", '{"command":"printf offline"}')
    )
    result = await invoke(work, backend, runtime, call)
    assert len(wire) == 1
    public = json.loads(result.calls[0][1])
    assert public["data"] == rejection and not public.get("uncertain")
    assert not await work.control.has_unresolved_effects()
    fact = (await work.control.effect_evidence())[0]
    assert fact["ok"] is False and not fact["uncertain"]
    completion = json.loads(
        await work.control.execute(
            "task_control", {"action": "complete"}, "complete-after-rejection"
        )
    )
    assert completion["ok"] is False
    assert completion["error"] == "work_completion_requires_execution_evidence"
    task = await tasks.get(wire[0]["request_id"])
    assert task.status == "completed" and task.run_id is None
    assert json.loads(task.completion_json) == rejection


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
async def test_late_terminal_receipt_resolves_original_unknown_request(
    database, tmp_path, monkeypatch, status
):
    wire = socket_receipts(monkeypatch, [OSError("lost receipt"), OSError("lost lookup")])
    work, tasks, backend, runtime = await host_case(database, tmp_path)
    call = ToolCall("late-original", ToolFunction("terminal_exec", '{"command":"printf offline"}'))
    await invoke(work, backend, runtime, call)
    assert await work.control.has_unresolved_effects()
    previous = json.loads(await effect_receipt(database, work, call))
    event = terminal_receipt(wire[0]["request_id"], status)
    await tasks.receive(event)
    await work.control.reconcile_completed_children()
    assert not await work.control.has_unresolved_effects()
    receipt_json = await effect_receipt(database, work, call)
    receipt = json.loads(receipt_json)
    fact = receipt["outcome"]
    assert fact["run_id"] == event["run_id"] and fact["status"] == status
    assert fact["ok"] is (status == "succeeded")
    assert fact["pending"] is False and fact["uncertain"] is False
    assert fact["mutation_committed"] is None
    assert fact["tool"] == "terminal_exec" and fact["side_effecting"] is True
    assert {key: value for key, value in receipt.items() if key != "outcome"} == {
        key: value for key, value in previous.items() if key != "outcome"
    }
    assert work.control.tools_started == 1 and len(wire) == 2
    assert (await work.control.repository.get(work.control.current["id"]))["tool_calls"] == 1
    # Reconciliation is idempotent and never submits another execution.
    await work.control.reconcile_completed_children()
    assert await effect_receipt(database, work, call) == receipt_json
    assert len(wire) == 2 and work.control.tools_started == 1
    completion = json.loads(
        await work.control.execute("task_control", {"action": "complete"}, "complete-after-late")
    )
    if status == "succeeded":
        assert completion["ok"] is True and completion["ending_proposed"] == "completed"
    else:
        assert completion["ok"] is False
        assert completion["error"] == "work_completion_requires_execution_evidence"


async def test_late_receipt_does_not_rewrite_observation_receipt(database, tmp_path, monkeypatch):
    wire = socket_receipts(monkeypatch, [OSError("lost receipt"), OSError("lost lookup")])
    work, tasks, backend, runtime = await host_case(database, tmp_path)
    call = ToolCall(
        "observed-original", ToolFunction("terminal_exec", '{"command":"printf offline"}')
    )
    await invoke(work, backend, runtime, call)
    event = terminal_receipt(wire[0]["request_id"], "succeeded")
    # A successful poll precedes the durable completion inbox. It is an
    # observation of the execution, not the original dispatch receipt.
    poll_wire = socket_receipts(monkeypatch, [event["result"]])
    observation = ToolCall(
        "original-observation",
        ToolFunction("get_code_run", json.dumps({"run_id": event["run_id"]})),
    )
    await invoke(work, backend, runtime, observation)
    before = await effect_receipt(database, work, observation)
    assert json.loads(before)["outcome"]["side_effecting"] is False
    assert work.control.tools_started == 2
    assert await work.control.has_unresolved_effects()
    await tasks.receive(event)
    await work.control.reconcile_completed_children()
    assert not await work.control.has_unresolved_effects()
    assert await effect_receipt(database, work, observation) == before
    assert work.control.tools_started == 2 and len(wire) == 2 and len(poll_wire) == 1


@pytest.mark.parametrize(
    "mismatch",
    ["conversation", "source_conversation_only", "work", "generation", "bool_generation"],
)
async def test_late_receipt_from_other_source_cannot_settle_original_unknown(
    database, tmp_path, monkeypatch, mismatch
):
    wire = socket_receipts(monkeypatch, [OSError("lost receipt"), OSError("lost lookup")])
    work, tasks, backend, runtime = await host_case(database, tmp_path)
    call = ToolCall("foreign-late", ToolFunction("terminal_exec", '{"command":"printf offline"}'))
    await invoke(work, backend, runtime, call)
    request_id = wire[0]["request_id"]
    task = await tasks.get(request_id)
    source = json.loads(task.source_json)
    async with database.sessions() as writer, writer.begin():
        values = {}
        if mismatch == "conversation":
            person_id = await ensure_person(writer, "10001")
            other = await ensure_canonical_conversation(
                writer, kind="private", primary_scope_key="private:10001", person_id=person_id
            )
            source["conversation_id"] = other.conversation_id
            values["source_conversation_id"] = other.conversation_id
        elif mismatch == "work":
            source["work_id"] = str(uuid4())
        elif mismatch == "source_conversation_only":
            source["conversation_id"] = str(uuid4())
        elif mismatch == "bool_generation":
            assert work.control.lease.generation == 1
            source["generation"] = True
        else:
            source["generation"] += 1
        values["source_json"] = json.dumps(source)
        await writer.execute(
            update(SandboxTaskRunModel)
            .where(SandboxTaskRunModel.request_id == request_id)
            .values(**values)
        )
    before = await effect_receipt(database, work, call)
    await tasks.receive(terminal_receipt(request_id, "succeeded"))
    await work.control.reconcile_completed_children()
    assert await work.control.has_unresolved_effects()
    assert await effect_receipt(database, work, call) == before
    assert work.control.tools_started == 1 and len(wire) == 2
