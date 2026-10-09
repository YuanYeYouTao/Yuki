"""Offline audit: real Work/SQLite/driver, deterministic fake engine and business tools.
The native variant uses the pinned worker; business IO, providers and QQ remain inert.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from tests.support.codemode_cases import effect_rows, environment, outer_call, requires_worker
from tests.support.work_effect_results_helpers import execute, owned_session
from tests.support.work_session import invoke_tool

from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.codemode.driver import ChildClass, CodeModeDriver, _Child, _State
from qq_ai_bot.codemode.driver_types import EngineCall, EngineOutcome, HostCounters
from qq_ai_bot.runtime.work_repository import WorkRepository


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
        args["run_id"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

        async def owned_run(identity):
            return {"pending": True} if identity == args["run_id"] else None

        env.control.resolve_child = owned_run
    if action == "complete":
        # A state_change Work may complete only after an original real receipt.
        from qq_ai_bot.domain.messages import ToolCall, ToolFunction

        async def prior_write():
            return await env.domain("workspace_write", '{"path":"already-done"}')

        await invoke_tool(
            env.owner,
            ToolCall("prerequisite", ToolFunction("workspace_write", "{}")),
            prior_write,
            side_effecting=True,
        )
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
            origin="user_message", actor_user_id=event.sender_user_id, trigger_event_id=event.id
        )
        args.update(goal="independent next task", output_kind="answer")
    before_log = list(env.domain.log)
    if not native:
        env.host.worker = SimpleNamespace(execution_digest=lambda: "fake-engine-for-audit")
        env.host.engine_factory = lambda *_args: FakeEngine(args)
    outer = outer_call(
        env,
        f"await yuki_task_control({args!r})\n"
        "await yuki_workspace_write({'path':'should-not-run'})",
    )
    from qq_ai_bot.domain.messages import ChatMessage

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
    from tests.support.codemode_cases import build_host
    from tests.support.work_session import WorkSession

    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.services.turn_transcript import TurnTranscript

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
    print("RESTORED", restored_control.ending, len(restored_session.pending_compositions))
    assert len(restored_session.pending_compositions) == 1
    assert restored_control.ending is None
    new_env = build_host(restored_session, env.domain)
    new_env.host.worker = env.host.worker
    new_env.host.engine_factory = env.host.engine_factory
    env = new_env
    result = json.loads(await CodeModeDriver(env.host, outer).resume())
    print("RESUME_RESULT", json.dumps(result), "DOWNSTREAM", env.domain.log)
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
    assert tools == (root or 0) == (1 if action == "complete" else 0)
    assert len([row for row in rows.values() if row["kind"] == "tool"]) == (
        2 if action == "complete" else 1
    )


async def test_pending_owned_execution_allows_code_wait_control(database, tmp_path):
    control, session, store = await owned_session(database, tmp_path)
    control.session = session
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

    async def resolve(identity):
        return {"pending": True} if identity == run_id else None

    control.resolve_child = resolve
    from tests.support.codemode_cases import build_host

    env = build_host(session, SimpleNamespace())
    args = {"action": "wait", "run_id": run_id}
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
        await driver._dispatch_child(_State(outer.identity.operation_id, 0), child, peers=[child])
    assert stopped.value.reason == "host_control"
    print("CODE_WAIT", child.receipt)
    direct = json.loads(await control.execute("task_control", args, "direct-wait-control"))
    print("DIRECT_WAIT", direct)
    assert direct["ok"] is True
    assert json.loads(child.receipt)["ok"] is True, "Code Mode must retain direct wait semantics"
