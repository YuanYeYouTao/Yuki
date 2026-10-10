"""Offline audit: real Work/SQLite/driver, deterministic fake engine and business tools.
The native variant uses the pinned worker; business IO, providers and QQ remain inert.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from tests.support.codemode_cases import (
    FakeDomain,
    build_host,
    effect_rows,
    outer_call,
    requires_worker,
)
from tests.support.work_effect_results_helpers import execute
from tests.support.work_runner_helpers import case
from tests.support.work_session import WorkSession

from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.codemode.driver import ChildClass, CodeModeDriver, _Child, _State
from qq_ai_bot.codemode.driver_types import EngineCall, EngineOutcome, HostCounters
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository


async def environment(database, tmp_path, *, reporting=None):
    test_case = await case(database, tmp_path, [], reporting=reporting)
    owner = WorkSession(test_case.control, "contract")
    test_case.control.session = owner
    owner.transcript = TurnTranscript(
        (ChatMessage("system", "test"), ChatMessage("user", "compose"))
    )
    return build_host(owner, FakeDomain())


class FakeRun:
    """Model exactly two sequential awaited wrappers: need_input then a write."""

    def __init__(self, arguments=None):
        self.arguments = arguments or {"action": "need_input", "reason": "Which file?"}
        self.index = 0
        self.waiting = False
        self.counters = HostCounters()

    def event(self):
        if self.index == 2:
            return EngineOutcome("completed", output="done")
        name, args = [
            ("yuki_task_control", self.arguments),
            ("yuki_workspace_write", {"path": "should-not-run"}),
        ][self.index]
        return EngineOutcome(
            "suspended",
            call=EngineCall(
                "future" if self.waiting else "function",
                self.index,
                None if self.waiting else self.index,
                None if self.waiting else name,
                args=() if self.waiting else (args,),
                pending_call_ids=(self.index,) if self.waiting else (),
            ),
        )

    async def start(self, code, inputs):
        return self.event()

    async def answer(self, call_id, answer):
        self.waiting = True
        return self.event()

    async def settle(self, results):
        self.index += 1
        self.waiting = False
        return self.event()

    def dump(self):
        return b"MONTY\0" + json.dumps([self.index, self.waiting]).encode()

    async def restore(self, dump, saved, counters):
        self.index, self.waiting = json.loads(dump[6:])
        self.counters = counters
        return self.event()

    async def terminate(self):
        pass


class FakeEngine:
    def __init__(self, arguments=None):
        self.arguments = arguments

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    def run(self, names):
        return FakeRun(self.arguments)


@pytest.mark.parametrize("native", [False, pytest.param(True, marks=requires_worker)])
@pytest.mark.parametrize(
    "action,ending",
    [
        ("need_input", "waiting_user"),
        ("wait", "waiting_external"),
        ("complete", "completed"),
        ("fail", "failed"),
        ("cancel", "cancelled"),
        ("accept", None),
    ],
)
async def test_control_receipt_crash_must_not_resume_after_need_input(
    database, tmp_path, monkeypatch, native, action, ending
):
    env = await environment(database, tmp_path, reporting="quiet" if action == "complete" else None)
    args = {"action": action}
    if action in {"need_input", "fail"}:
        args["reason"] = "Which file?"
    if action == "wait":
        args["conditions"] = [{"kind": "time_due", "after_seconds": 60}]
    if action == "accept":
        from sqlalchemy import select

        from qq_ai_bot.persistence.models import ChatEventModel

        async with database.sessions() as reader:
            event = await reader.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.canonical_conversation_id == env.control.lease.conversation_id,
                    ChatEventModel.direction == "inbound",
                )
            )
        # A genuinely recorded new input is required for independent handoff.
        env.control.source.update(
            origin="user_message",
            actor_user_id=event.sender_user_id,
            actor_person_id=event.author_person_id,
            trigger_event_id=event.id,
        )
        args.update(goal="independent next task")
    before_log = list(env.domain.log)
    if not native:
        env.host.worker = SimpleNamespace(execution_digest=lambda: "fake-engine-for-audit")
        env.host.engine_factory = lambda *_args: FakeEngine(args)
    outer = outer_call(
        env,
        f"await yuki_task_control({args!r})\n"
        "await yuki_workspace_write({'path':'should-not-run'})",
    )
    env.owner.transcript.append(ChatMessage("assistant", tool_calls=(outer.call,)))
    await env.owner.save("response", (outer.call,))
    original = WorkRepository.record_effect
    crashed = False

    async def save_then_crash(self, key, state, receipt, **kwargs):
        nonlocal crashed
        await original(self, key, state, receipt, **kwargs)
        if receipt.get("outcome", {}).get("tool") == "task_control" and not crashed:
            assert receipt["outcome"]["ok"], receipt
            crashed = True
            raise asyncio.CancelledError("simulated crash after child control commit")

    monkeypatch.setattr(WorkRepository, "record_effect", save_then_crash)
    with pytest.raises(asyncio.CancelledError):
        await CodeModeDriver(env.host, outer).run()
    assert env.domain.log == before_log
    # Recreate the activation objects and use the real journal restore path.
    from qq_ai_bot.runtime.work_control import WorkControl

    old = env.control
    restored_control = WorkControl(
        old.repository, old.lease, old.source_key, old.source, old.validate
    )
    restored_control.current = await old.repository.get(old.current["id"])
    restored_session = WorkSession(restored_control, env.owner.contract)
    restored_control.session = restored_session
    await restored_session.restore(
        TurnTranscript((ChatMessage("system", "test"), ChatMessage("user", "resume")))
    )
    assert len(restored_session.pending_compositions) == 1
    assert restored_control.ending is None
    new_env = build_host(restored_session, env.domain)
    new_env.host.worker = env.host.worker
    new_env.host.engine_factory = env.host.engine_factory
    env = new_env
    result = json.loads(await CodeModeDriver(env.host, outer).resume())
    assert env.domain.log == before_log, (
        "A committed need_input must prevent subsequent script effects"
    )
    assert result["stop_reason"] == "host_control" and restored_control.ending == ending
    if action == "accept":
        assert restored_control.handoff_work_id == result["control"]["queued_work_id"]
        assert restored_control.handoff_work_id != restored_control.current["id"]
    assert env.controls == []  # Already committed control is never dispatched on restore.
    rows, tools, root = await effect_rows(database, restored_control.current["id"])
    assert rows[outer.identity.operation_id]["state"] == "accepted"
    assert tools == root == 0
    assert len([row for row in rows.values() if row["kind"] == "tool"]) == 1


async def test_pending_owned_execution_allows_code_wait_condition(database, tmp_path):
    env = await environment(database, tmp_path)
    control, session = env.control, env.owner
    store = ToolArtifactRepository(database, tmp_path / "results", retention_seconds=60)
    run_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    await execute(
        session,
        store,
        "launch",
        ToolExecutionResult(
            ok=True,
            data={"run_id": run_id, "status": "running", "pending": True},
            mutation_committed=True,
            provider_id="core",
            tool_name="terminal_exec",
        ),
    )

    args = {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 60}]}
    outer = outer_call(env, "await yuki_task_control(...)")
    driver = CodeModeDriver(env.host, outer)
    child = _Child(
        "audit-wait-control",
        0,
        0,
        0,
        "task_control",
        json.dumps(args),
        ChildClass("control", False, False),
    )
    await control.repository.prepare_effect(
        control.lease,
        control.current["id"],
        child.operation_id,
        "tool",
        outcome={"side_effecting": False},
        invocation={"version": 1, "dispatch_started": False, "revision": 0},
    )
    # A successful wait is a Host stop, not an answer that lets the VM run on.
    from qq_ai_bot.codemode.driver import _Stop

    with pytest.raises(_Stop) as stopped:
        await driver._dispatch_child(_State(outer.identity.operation_id, 0), child)
    assert stopped.value.reason == "host_control"
    assert json.loads(child.receipt)["ok"] is True, "Code Mode must retain direct wait semantics"
