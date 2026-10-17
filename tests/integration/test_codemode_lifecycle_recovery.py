"""Offline audit: real Work/SQLite/driver, deterministic fake engine and business tools.
The native variant uses the pinned worker; business IO, providers and QQ remain inert.
"""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import update
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
from qq_ai_bot.codemode.api_projection import project
from qq_ai_bot.codemode.driver import (
    ChildClass,
    CodeCompositionYield,
    CodeModeDriver,
    _Child,
    _State,
    is_control_tool,
)
from qq_ai_bot.codemode.driver_types import EngineCall, EngineOutcome, HostCounters
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatMessage, ChatTool
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.subagent_tools import subagent_tools
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import scope
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository


async def environment(database, tmp_path, *, reporting=None):
    test_case = await case(database, tmp_path, [], reporting=reporting)
    owner = WorkSession(test_case.control, "contract")
    test_case.control.session = owner
    owner.transcript = TurnTranscript(
        (ChatMessage("system", "test"), ChatMessage("user", "compose"))
    )
    env = build_host(owner, FakeDomain())
    env.test_case = test_case
    return env


class FakeRun:
    """Host-only model of two sequential awaited wrappers: control then a write."""

    def __init__(self, arguments=None, calls=None):
        self.arguments = arguments or {"action": "need_input", "reason": "Which file?"}
        self.calls = calls or [
            ("yuki_task_control", self.arguments),
            ("yuki_workspace_write", {"path": "after-control"}),
        ]
        self.index = 0
        self.waiting = False
        self.counters = HostCounters()

    def event(self):
        if self.index == len(self.calls):
            return EngineOutcome("completed", output="done")
        name, args = self.calls[self.index]
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
    def __init__(self, arguments=None, calls=None):
        self.arguments = arguments
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    def run(self, names):
        return FakeRun(self.arguments, self.calls)


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
async def test_control_receipt_crash_restores_decision_and_valid_tail(
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
        "await yuki_workspace_write({'path':'after-control'})",
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
    await restored_control.restore_handoff(None)
    assert len(restored_session.pending_compositions) == 1
    assert restored_control.ending is None
    new_env = build_host(restored_session, env.domain)
    new_env.host.worker = env.host.worker
    new_env.host.engine_factory = env.host.engine_factory
    env = new_env
    outer = replace(outer, context=replace(outer.context, runtime=env.agent))
    result = json.loads(await CodeModeDriver(env.host, outer).resume())
    assert restored_control.ending == ending
    revoked = action in {"fail", "cancel"}
    assert env.domain.log == (
        before_log if revoked else [*before_log, ("workspace_write", {"path": "after-control"})]
    )
    if revoked:
        assert result["stop_reason"] == "admission_closed"
    else:
        assert result["status"] == "completed"
    if action == "accept":
        checkpoint = json.loads(restored_control.current["checkpoint_json"])
        assert restored_control.handoff_work_id == checkpoint["handoff_work_id"]
        assert restored_control.handoff_work_id != restored_control.current["id"]
    assert env.controls == []  # Already committed control is never dispatched on restore.
    rows, tools, root = await effect_rows(database, restored_control.current["id"])
    assert rows[outer.identity.operation_id]["state"] == "accepted"
    assert tools == root == (0 if revoked else 1)
    assert len([row for row in rows.values() if row["kind"] == "tool"]) == (1 if revoked else 2)


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
    # Waiting is a real persisted decision; it does not close the original VM.
    await driver._dispatch_child(_State(outer.identity.operation_id, 0), child)
    assert control.ending == "waiting_external"
    assert json.loads(child.receipt)["ok"] is True, "Code Mode must retain direct wait semantics"


@pytest.mark.parametrize("native", [False, pytest.param(True, marks=requires_worker)])
@pytest.mark.parametrize(
    "action,state", [("cancel", "cancelled"), ("fail", "failed"), ("resume", "queued")]
)
async def test_control_of_other_target_keeps_current_program(
    database, tmp_path, native, action, state
):
    env = await environment(database, tmp_path)
    target = json.loads(
        await env.control.execute(
            "task_control", {"action": "derive", "goal": "other target"}, "derive"
        )
    )["derived_work_id"]
    if action == "resume":
        row = await env.control.repository.get(target)
        await env.control.repository.transition(
            env.control.lease, target, row["revision"], "suspended"
        )
    args = {"action": action, "work_id": target, "reason": "target only"}
    if not native:
        env.host.worker = SimpleNamespace(execution_digest=lambda: "fake-engine-for-audit")
        env.host.engine_factory = lambda *_args: FakeEngine(args)
    body = json.loads(
        await CodeModeDriver(
            env.host,
            outer_call(
                env,
                f"c = await yuki_task_control({args!r})\n"
                "await yuki_workspace_write({'path':'after-control'})\n"
                "[c['ok'], 6 * 7]",
            ),
        ).run()
    )
    assert body["status"] == "completed"
    assert env.domain.log == [("workspace_write", {"path": "after-control"})]
    assert env.control.ending is None and env.control.current["state"] == "running"
    assert (await env.control.repository.get(target))["state"] == state
    if native:
        assert body["result"] == [True, 42]


@pytest.mark.parametrize("native", [False, pytest.param(True, marks=requires_worker)])
@pytest.mark.parametrize("action,ending", [("cancel", "cancelled"), ("fail", "failed")])
async def test_real_current_revocation_after_completion_proposal_stops_tail(
    database, tmp_path, native, action, ending
):
    env = await environment(database, tmp_path)
    calls = [
        ("yuki_task_control", {"action": "complete"}),
        ("yuki_task_control", {"action": action, "reason": "revoked original execution"}),
        ("yuki_workspace_write", {"path": "revoked-tail"}),
    ]
    if not native:
        env.host.worker = SimpleNamespace(execution_digest=lambda: "fake-engine-for-audit")
        env.host.engine_factory = lambda *_args: FakeEngine(calls=calls)
    body = json.loads(
        await CodeModeDriver(
            env.host,
            outer_call(env, "\n".join(f"await {name}({args!r})" for name, args in calls)),
        ).run()
    )
    assert body["stop_reason"] == "admission_closed"
    assert env.control.ending == ending and env.control.accepted_ending() == ending
    assert len(env.controls) == 2 and env.domain.log == []
    _, tools, root = await effect_rows(database, env.control.current["id"])
    assert tools == root == 0


@pytest.mark.parametrize("native", [False, pytest.param(True, marks=requires_worker)])
async def test_completion_proposal_survives_segment_yield_until_original_tail_settles(
    database, tmp_path, native
):
    env = await environment(database, tmp_path)
    env.host.tool_limit = 1
    calls = [
        ("yuki_lookup", {"q": "before completion"}),
        ("yuki_task_control", {"action": "complete", "result": "original decision"}),
        ("yuki_workspace_write", {"path": "next segment"}),
    ]
    if not native:
        env.host.worker = SimpleNamespace(execution_digest=lambda: "fake-engine-for-audit")
        env.host.engine_factory = lambda *_args: FakeEngine(calls=calls)
    outer = outer_call(env, "\n".join(f"await {name}({args!r})" for name, args in calls))
    env.owner.transcript.append(ChatMessage("assistant", tool_calls=(outer.call,)))
    await env.owner.save("response", (outer.call,))
    with pytest.raises(CodeCompositionYield):
        await CodeModeDriver(env.host, outer).run()
    assert env.control.ending == "completed"
    assert env.control.current["state"] == "running"
    assert env.domain.log == [("lookup", {"q": "before completion"})]
    from qq_ai_bot.runtime.work_control import WorkControl

    old = env.control
    restored_control = WorkControl(
        old.repository, old.lease, old.source_key, old.source, old.validate
    )
    restored_control.current = await old.repository.get(old.current["id"])
    owner = WorkSession(restored_control, env.owner.contract)
    restored_control.session = owner
    await owner.restore(
        TurnTranscript((ChatMessage("system", "test"), ChatMessage("user", "resume")))
    )
    assert len(owner.pending_compositions) == 1
    restored = build_host(owner, env.domain)
    restored.host.worker, restored.host.engine_factory = env.host.worker, env.host.engine_factory
    outer = replace(outer, context=replace(outer.context, runtime=restored.agent))
    body = json.loads(await CodeModeDriver(restored.host, outer).resume())
    assert body["status"] == "completed" and restored_control.ending == "completed"
    assert env.domain.log == [
        ("lookup", {"q": "before completion"}),
        ("workspace_write", {"path": "next segment"}),
    ]
    _, tools, root = await effect_rows(database, restored_control.current["id"])
    assert tools == root == 2 and restored.controls == []


@pytest.mark.parametrize("native", [False, pytest.param(True, marks=requires_worker)])
@pytest.mark.parametrize("action", ["cancel", "fail"])
async def test_revoked_original_work_settles_pending_program_without_reopening_vm(
    database, tmp_path, native, action
):
    env = await environment(database, tmp_path)
    env.host.tool_limit = 1
    calls = [
        ("yuki_lookup", {"q": "original"}),
        ("yuki_workspace_write", {"path": "revoked tail"}),
    ]
    if not native:
        env.host.worker = SimpleNamespace(execution_digest=lambda: "fake-engine-for-audit")
        env.host.engine_factory = lambda *_args: FakeEngine(calls=calls)
    outer = outer_call(env, "\n".join(f"await {name}({args!r})" for name, args in calls))
    with pytest.raises(CodeCompositionYield):
        await CodeModeDriver(env.host, outer).run()
    # This actual current-work decision was made outside the saved program;
    # there is no old code_stop child to infer it from.
    revoked = json.loads(
        await env.control.execute("task_control", {"action": action}, "revoke-original-work")
    )
    assert revoked["ok"]
    env.host.tool_limit = 32
    env.host.engine_factory = lambda *_args: pytest.fail("revoked execution reopened its VM")
    body = json.loads(await CodeModeDriver(env.host, outer).resume())
    assert body["stop_reason"] == "admission_closed"
    assert env.domain.log == [("lookup", {"q": "original"})]
    _, tools, root = await effect_rows(database, env.control.current["id"])
    assert tools == root == 1


@pytest.mark.parametrize("native", [False, pytest.param(True, marks=requires_worker)])
@pytest.mark.parametrize("interrupt", [None, "receipt_crash", "segment_yield"])
async def test_child_question_preserves_original_program_and_wait_on_restore(
    database, tmp_path, monkeypatch, native, interrupt
):
    parent_env = await environment(database, tmp_path)
    database.subagents_enabled = True
    workers = SubagentRepository(parent_env.control.repository)
    identity = await workers.start(
        parent_env.control.lease,
        parent_env.control.current["id"],
        "code-child",
        {"goal": "child question with independent tail"},
    )
    lease = await workers.acquire(identity)
    assert lease is not None
    row = await parent_env.control.repository.get(identity)
    from qq_ai_bot.runtime.work_control import WorkControl

    async def validate():
        assert await parent_env.control.repository.valid(lease)

    control = WorkControl(
        parent_env.control.repository,
        lease,
        row["source_key"],
        json.loads(row["source_json"]),
        validate,
        current=row,
    )
    owner = WorkSession(control, "contract")
    control.session = owner
    owner.transcript = TurnTranscript(
        (ChatMessage("system", "test"), ChatMessage("user", "compose child"))
    )
    env = build_host(owner, FakeDomain(), tool_limit=1 if interrupt == "segment_yield" else 32)
    env.host.api = project(
        (
            ChatTool("lookup", "lookup", {"type": "object"}),
            ChatTool("workspace_write", "write", {"type": "object"}),
            *subagent_tools(),
        ),
        "manifest-test",
    )
    original_classify = env.host.classify
    env.host.classify = lambda call: (
        ChildClass("control", False, False)
        if is_control_tool(call.function.name)
        else original_classify(call)
    )
    calls = [
        ("yuki_lookup", {"q": "first"}),
        ("yuki_subagent_control", {"action": "message", "text": "Which artifact?", "ask": True}),
        ("yuki_workspace_write", {"path": "after question"}),
    ]
    if not native:
        env.host.worker = SimpleNamespace(execution_digest=lambda: "fake-engine-for-audit")
        env.host.engine_factory = lambda *_args: FakeEngine(calls=calls)
    outer = outer_call(
        env,
        "await yuki_lookup({'q':'first'})\n"
        "q = await yuki_subagent_control({'action':'message',"
        "'text':'Which artifact?','ask':True})\n"
        "await yuki_workspace_write({'path':'after question'})\n"
        "print('child tail completed')\n"
        "[q['ok'], q['data']['waiting_parent'], 6 * 7]",
    )
    owner.transcript.append(ChatMessage("assistant", tool_calls=(outer.call,)))
    await owner.save("response", (outer.call,))
    if interrupt == "receipt_crash":
        original = WorkRepository.record_effect
        crashed = False

        async def save_then_crash(self, key, state, receipt, **kwargs):
            nonlocal crashed
            await original(self, key, state, receipt, **kwargs)
            if receipt.get("outcome", {}).get("tool") == "subagent_control" and not crashed:
                crashed = True
                raise asyncio.CancelledError("crash after durable child question")

        monkeypatch.setattr(WorkRepository, "record_effect", save_then_crash)
    if interrupt:
        with pytest.raises(
            asyncio.CancelledError if interrupt == "receipt_crash" else CodeCompositionYield
        ):
            await CodeModeDriver(env.host, outer).run()
        assert control.accepted_ending() == "waiting_user"
        old = control
        control = WorkControl(old.repository, old.lease, old.source_key, old.source, old.validate)
        control.current = await old.repository.get(identity)
        owner = WorkSession(control, "contract")
        control.session = owner
        await owner.restore(
            TurnTranscript((ChatMessage("system", "test"), ChatMessage("user", "resume child")))
        )
        assert len(owner.pending_compositions) == 1
        restored = build_host(owner, env.domain)
        restored.host.api = env.host.api
        restored.host.worker, restored.host.engine_factory = (
            env.host.worker,
            env.host.engine_factory,
        )
        restored.host.classify = env.host.classify
        env = restored
        outer = replace(outer, context=replace(outer.context, runtime=env.agent))
        raw = await CodeModeDriver(env.host, outer).resume()
    else:
        raw = await CodeModeDriver(env.host, outer).run()
    body = json.loads(raw)
    assert body["status"] == "completed" and control.ending == "waiting_user"
    assert env.domain.log == [
        ("lookup", {"q": "first"}),
        ("workspace_write", {"path": "after question"}),
    ]
    if native:
        assert body["result"] == [True, True, 42]
        assert body["stdout"] == "child tail completed\n"
    pending = await parent_env.control.pending()
    assert len(pending) == 1
    assert await parent_env.control.has_pending_business_inputs()
    rows, tools, _root = await effect_rows(database, identity)
    questions = [
        row
        for row in rows.values()
        if json.loads(row["receipt_json"]).get("outcome", {}).get("tool") == "subagent_control"
    ]
    assert len(questions) == 1 and tools == 2
    _, parent_tools, root = await effect_rows(database, parent_env.control.current["id"])
    assert parent_tools == 0 and root == 2
    accepted = control.accepted
    assert accepted["call_key"] == questions[0]["effect_key"]
    await control.settle(pending_inputs=False)
    assert control.current["state"] == "waiting_user"


@pytest.mark.parametrize("native", [False, pytest.param(True, marks=requires_worker)])
@pytest.mark.parametrize("crash", [False, True])
@pytest.mark.parametrize("error", ["capability_not_allowed", "capability_no_longer_authorized"])
async def test_single_tool_refusal_keeps_valid_tail_on_run_and_restore(
    database, tmp_path, monkeypatch, native, crash, error
):
    env = await environment(database, tmp_path)
    refusals = []

    async def business(name, arguments):
        if name == "workspace_write":
            refusals.append(json.loads(arguments))
            return json.dumps({"ok": False, "executed": False, "error": error})
        return await env.domain(name, arguments)

    env.host.execute_business = build_host(env.owner, business).host.execute_business
    if not native:
        env.host.worker = SimpleNamespace(execution_digest=lambda: "fake-engine-for-audit")
        calls = [
            ("yuki_workspace_write", {"path": "denied"}),
            ("yuki_lookup", {"q": "authorized"}),
        ]
        env.host.engine_factory = lambda *_args: FakeEngine(calls=calls)
    outer = outer_call(
        env,
        "r = await yuki_workspace_write({'path':'denied'})\n"
        "s = await yuki_lookup({'q':'authorized'})\n"
        "[r['ok'], r['error']['code'], s['ok'], 6 * 7]",
    )
    env.owner.transcript.append(ChatMessage("assistant", tool_calls=(outer.call,)))
    await env.owner.save("response", (outer.call,))
    if crash:
        original = WorkRepository.record_effect
        crashed = False

        async def save_then_crash(self, key, state, receipt, **kwargs):
            nonlocal crashed
            await original(self, key, state, receipt, **kwargs)
            if receipt.get("outcome", {}).get("tool") == "workspace_write" and not crashed:
                crashed = True
                raise asyncio.CancelledError("crash after durable individual refusal")

        monkeypatch.setattr(WorkRepository, "record_effect", save_then_crash)
        with pytest.raises(asyncio.CancelledError):
            await CodeModeDriver(env.host, outer).run()
        assert env.domain.log == []
        from qq_ai_bot.runtime.work_control import WorkControl

        old = env.control
        control = WorkControl(old.repository, old.lease, old.source_key, old.source, old.validate)
        control.current = await old.repository.get(old.current["id"])
        owner = WorkSession(control, env.owner.contract)
        control.session = owner
        await owner.restore(
            TurnTranscript((ChatMessage("system", "test"), ChatMessage("user", "resume")))
        )
        restored = build_host(owner, env.domain)
        restored.host.execute_business = build_host(owner, business).host.execute_business
        restored.host.worker, restored.host.engine_factory = (
            env.host.worker,
            env.host.engine_factory,
        )
        env = restored
        outer = replace(outer, context=replace(outer.context, runtime=env.agent))
        raw = await CodeModeDriver(env.host, outer).resume()
    else:
        raw = await CodeModeDriver(env.host, outer).run()
    body = json.loads(raw)
    assert body["status"] == "completed"
    assert refusals == [{"path": "denied"}]
    assert env.domain.log == [("lookup", {"q": "authorized"})]
    assert [operation["status"] for operation in body["operations"]] == [
        "not_executed",
        "succeeded",
    ]
    if native:
        assert body["result"] == [False, error, True, 42]
    assert await CodeModeDriver(env.host, outer).resume() == raw
    assert refusals == [{"path": "denied"}] and len(env.domain.log) == 1


@pytest.mark.parametrize("changed_api", [False, True])
async def test_original_code_stop_and_changed_api_never_reopen_program(
    database, tmp_path, monkeypatch, changed_api
):
    """Upgrade reader: a saved VM-close fact is not a license to run its tail."""
    env = await environment(database, tmp_path)
    env.host.worker = SimpleNamespace(execution_digest=lambda: "fake-engine-for-audit")
    env.host.engine_factory = lambda *_args: FakeEngine()
    if changed_api:
        env.host.api = replace(env.host.api, api_revision="yuki.codemode.api.v1")
    outer = outer_call(
        env,
        "await yuki_task_control({'action':'need_input','reason':'Which file?'})\n"
        "await yuki_workspace_write({'path':'after-control'})",
    )
    env.owner.transcript.append(ChatMessage("assistant", tool_calls=(outer.call,)))
    await env.owner.save("response", (outer.call,))
    original = WorkRepository.record_effect
    crashed = False

    async def save_original_stop_then_crash(self, key, state, receipt, **kwargs):
        nonlocal crashed
        if receipt.get("outcome", {}).get("tool") == "task_control" and not crashed:
            receipt = {
                **receipt,
                "code_stop": {
                    "reason": "host_control",
                    "payload": {"control": json.loads(receipt["result"])},
                    "ending": "waiting_user",
                    "handoff_work_id": None,
                },
            }
            crashed = True
            await original(self, key, state, receipt, **kwargs)
            raise asyncio.CancelledError("old Host closed after saving its control result")
        await original(self, key, state, receipt, **kwargs)

    monkeypatch.setattr(WorkRepository, "record_effect", save_original_stop_then_crash)
    with pytest.raises(asyncio.CancelledError):
        await CodeModeDriver(env.host, outer).run()
    # A separately prepared old child has never been dispatched. Upgrade must
    # preserve that fact, not make it the new tail of the saved VM.
    driver = CodeModeDriver(env.host, outer)
    child_key = outer.identity.operation_id + "/c1"
    invocation = driver._child_invocation(
        child_key, 1, 1, 1, "workspace_write", '{"path":"old-tail"}'
    )
    arguments_ref = await env.owner.journal.objects.put(invocation.call.function.arguments)
    await env.control.repository.prepare_effect(
        env.control.lease,
        env.control.current["id"],
        child_key,
        "tool",
        invocation={**invocation.durable_metadata(), "arguments_ref": arguments_ref},
        outcome={"tool": "workspace_write", "executed": False, "ok": False},
    )
    saved_rows, tools, root = await effect_rows(database, env.control.current["id"])
    original_control = next(
        row["receipt_json"]
        for row in saved_rows.values()
        if json.loads(row["receipt_json"]).get("outcome", {}).get("tool") == "task_control"
    )
    if changed_api:
        env.host.api = replace(env.host.api, api_revision="yuki.codemode.api.v2")
    env.host.engine_factory = lambda *_args: pytest.fail("closed original program reopened its VM")
    raw = await driver.resume()
    body = json.loads(raw)
    if changed_api:
        assert body["stop_reason"] == "snapshot_unavailable"
        assert body["snapshot_reason"] == "code_api_changed"
    else:
        assert body["stop_reason"] == "host_control" and env.control.ending == "waiting_user"
    assert env.domain.log == [] and len(env.controls) == 1
    rows, after_tools, after_root = await effect_rows(database, env.control.current["id"])
    assert after_tools == tools == 0 and after_root == root == 0
    assert any(row["receipt_json"] == original_control for row in rows.values())
    assert rows[child_key]["state"] == "failed"
    assert json.loads(rows[child_key]["receipt_json"])["outcome"]["executed"] is False
    # An already paired old parent is readable even without today's engine.
    env.host.api = replace(env.host.api, api_revision="yuki.codemode.api.v2")
    env.host.worker = None
    assert await driver.run() == raw
    assert await driver.resume() == raw
    assert env.domain.log == []


@pytest.mark.parametrize("native", [False, pytest.param(True, marks=requires_worker)])
@pytest.mark.parametrize("unavailable", ["worker", "api", "backend"])
async def test_runner_restores_original_program_with_unavailable_current_code_contract(
    database, tmp_path, native, unavailable
):
    from tests.support.codemode_runner_helpers import Backend

    from qq_ai_bot.runtime.work_control import WorkControl

    env = await environment(database, tmp_path)
    env.host.tool_limit = 1
    calls = [
        ("yuki_lookup", {"q": "paid original evidence"}),
        ("yuki_workspace_write", {"path": "unstarted tail"}),
    ]
    if not native:
        env.host.worker = SimpleNamespace(execution_digest=lambda: "fake-engine-for-audit")
        env.host.engine_factory = lambda *_args: FakeEngine(calls=calls)
    outer = outer_call(env, "\n".join(f"await {name}({args!r})" for name, args in calls))
    env.owner.transcript.append(ChatMessage("assistant", tool_calls=(outer.call,)))
    await env.owner.save("response", (outer.call,))
    with pytest.raises(CodeCompositionYield):
        await CodeModeDriver(env.host, outer).run()
    old_rows, old_tools, old_root = await effect_rows(database, env.control.current["id"])
    original_child = next(
        row["receipt_json"]
        for row in old_rows.values()
        if json.loads(row["receipt_json"]).get("outcome", {}).get("tool") == "lookup"
    )
    old = env.control
    control = WorkControl(old.repository, old.lease, old.source_key, old.source, old.validate)
    control.current = await old.repository.get(old.current["id"])
    owner = WorkSession(control, "contract")
    control.session = owner
    await owner.restore(
        TurnTranscript((ChatMessage("system", "test"), ChatMessage("user", "resume")))
    )
    assert len(owner.pending_compositions) == 1
    runtime = replace(env.test_case.runtime, work_control=control, script_api=None)
    runner = env.test_case.runner
    runner.code_mode_settings = None  # Current deployment has disabled its Code worker.
    runner.main_contract = SimpleNamespace(
        revision="current-direct-contract",
        script_api=None if unavailable == "api" else env.host.api,
    )
    backend = None if unavailable == "backend" else Backend()
    yielded = await runner._resume_tool_response(
        owner.pending,
        owner.transcript,
        backend,
        runtime,
        (ChatTool("read_fixture", "current direct tool", {"type": "object"}),),
    )
    assert yielded is False
    paired = [
        message
        for message in owner.transcript.request().messages
        if message.role == "tool" and message.tool_call_id == outer.call.id
    ]
    assert len(paired) == 1
    body = json.loads(paired[0].content)
    assert body["replay_forbidden"] is True
    if unavailable == "backend":
        assert body["error"] == "execution_outcome_unknown" and body["uncertain"] is True
    else:
        assert body["stop_reason"] == "snapshot_unavailable"
        assert body["snapshot_reason"] == "code_engine_unavailable"
    rows, tools, root = await effect_rows(database, control.current["id"])
    assert any(row["receipt_json"] == original_child for row in rows.values())
    assert tools == old_tools == 1 and root == old_root == 1
    assert rows[outer.identity.operation_id]["state"] == (
        "prepared" if unavailable == "backend" else "accepted"
    )
    assert env.domain.log == [("lookup", {"q": "paid original evidence"})]
    assert backend is None or backend.log == []


@pytest.mark.parametrize("native", [False, pytest.param(True, marks=requires_worker)])
@pytest.mark.parametrize("revoked", ["generation", "lease"])
async def test_actual_execution_source_revocation_stops_new_program_effects(
    database, tmp_path, native, revoked
):
    env = await environment(database, tmp_path)
    calls = [
        ("yuki_lookup", {"q": "original evidence"}),
        ("yuki_workspace_write", {"path": "invalid source tail"}),
    ]
    original = env.host.execute_business

    async def revoke_after_original_receipt(invocation, side_effecting):
        result = await original(invocation, side_effecting)
        # Apply the real persisted source change after the already admitted
        # result, before the next boundary; no fake permission receipt is used.
        async with database.immediate_session() as writer:
            if revoked == "generation":
                await writer.execute(
                    update(CanonicalConversationModel)
                    .where(CanonicalConversationModel.id == env.control.lease.conversation_id)
                    .values(generation=CanonicalConversationModel.generation + 1)
                )
            else:
                await writer.execute(
                    update(scope)
                    .where(scope.c.conversation_id == env.control.lease.conversation_id)
                    .values(owner=None, fence=scope.c.fence + 1)
                )
        return result

    env.host.execute_business = revoke_after_original_receipt
    if not native:
        env.host.worker = SimpleNamespace(execution_digest=lambda: "source-revocation-test")
        env.host.engine_factory = lambda *_args: FakeEngine(calls=calls)
    body = json.loads(
        await CodeModeDriver(
            env.host,
            outer_call(env, "\n".join(f"await {name}({args!r})" for name, args in calls)),
        ).run()
    )
    assert body["stop_reason"] == "admission_closed"
    assert env.domain.log == [("lookup", {"q": "original evidence"})]
    rows, tools, root = await effect_rows(database, env.control.current["id"])
    assert tools == root == 1
    original_child = [row for row in rows.values() if row["kind"] == "tool"]
    assert len(original_child) == 1 and original_child[0]["state"] == "accepted"
