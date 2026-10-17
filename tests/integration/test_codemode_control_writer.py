"""Original program tails survive segment settlement by the real Runner/writer."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.integration.test_codemode_lifecycle_recovery import FakeEngine, environment
from tests.support.codemode_cases import TOOLS, FakeDomain, build_host, effect_rows, requires_worker

from yuki.codemode.api_projection import project
from yuki.codemode.driver import ChildClass, is_control_tool
from yuki.domain.messages import ChatMessage, ChatResponse, ToolCall, ToolFunction
from yuki.persistence.models import ChatEventModel
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.subagent_tools import subagent_tools
from yuki.runtime.work_control import WorkControl, work_control_tools


@pytest.mark.parametrize("native", [False, pytest.param(True, marks=requires_worker)])
@pytest.mark.parametrize("action", ["complete", "wait", "need_input", "ask", "handoff"])
async def test_original_program_control_and_tail_survive_runner_segment_writer(
    database, tmp_path, monkeypatch, native, action
):
    env = await environment(database, tmp_path)
    parent = env.control
    workers = None
    if action == "ask":
        database.subagents_enabled = True
        workers = SubagentRepository(parent.repository)
        identity = await workers.start(parent.lease, parent.current["id"], "child", {"goal": "ask"})
        lease = await workers.acquire(identity)
        assert lease is not None
        row = await parent.repository.get(identity)

        async def validate():
            assert await parent.repository.valid(lease)

        env.control = WorkControl(
            parent.repository,
            lease,
            row["source_key"],
            json.loads(row["source_json"]),
            validate,
            current=row,
        )
        env.domain = FakeDomain()
    control = env.control
    control.session = None
    if action == "handoff":
        async with database.sessions() as reader:
            event = await reader.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.canonical_conversation_id == control.lease.conversation_id,
                    ChatEventModel.direction == "inbound",
                )
            )
        control.source.update(
            origin="user_message",
            actor_user_id=event.sender_user_id,
            actor_person_id=event.author_person_id,
            trigger_event_id=event.id,
        )
    name = "subagent_control" if action == "ask" else "task_control"
    args = {
        "complete": {"action": "complete", "result": "original result"},
        "wait": {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 60}]},
        "need_input": {"action": "need_input", "reason": "Which file?"},
        "ask": {"action": "message", "text": "Which artifact?", "ask": True},
        "handoff": {"action": "accept", "goal": "independent next goal"},
    }[action]
    calls = [
        ("yuki_lookup", {"q": "before control"}),
        ("yuki_" + name, args),
        ("yuki_workspace_write", {"path": "original tail"}),
    ]
    code = "\n".join(f"await {tool}({arguments!r})" for tool, arguments in calls)
    call = ToolCall("original-code", ToolFunction("execute_code", json.dumps({"code": code})))
    case = env.test_case

    def response(_request):
        assert len(case.provider.requests) == 1, "the saved decision bought another model request"
        return ChatResponse("", 0, tool_calls=(call,))

    case.provider._responder = response
    definitions = tuple(
        {tool.name: tool for tool in (*TOOLS, *work_control_tools(), *subagent_tools())}.values()
    )
    api = project(definitions, "manifest-test")
    worker = env.host.worker if native else SimpleNamespace(execution_digest=lambda: "writer-test")

    def host(_tools, runtime, **_kwargs):
        built = build_host(
            runtime.work_control.session, env.domain, tool_limit=runtime.max_tool_calls
        )
        built.host.api = api
        built.host.worker = worker
        if not native:
            built.host.engine_factory = lambda *_args: FakeEngine(calls=calls)
        classify = built.host.classify
        built.host.classify = lambda child: (
            ChildClass("control", False, False)
            if is_control_tool(child.function.name)
            else classify(child)
        )
        return built.host

    monkeypatch.setattr(case.runner, "_code_host", host)
    runtime = replace(
        case.runtime,
        work_control=control,
        fixed_tools=definitions,
        script_api=api,
        max_tool_calls=1,
        origin=TurnOrigin.SYSTEM_TASK if action == "ask" else TurnOrigin.USER_MESSAGE,
    )
    first = await case.runner.run(
        (ChatMessage("user", "original known steps"),), runtime, case.backend
    )
    assert first.work_state == "queued" and control.yield_segment
    assert env.domain.log == [("lookup", {"q": "before control"})]
    expected = {
        "complete": "completed",
        "wait": "waiting_external",
        "need_input": "waiting_user",
        "ask": "waiting_user",
    }.get(action)
    await control.settle(pending_inputs=False)
    assert control.current["state"] == "queued"
    assert control.accepted_ending() == expected
    if action == "handoff":
        target = control.handoff_work_id
        assert target and (await control.repository.get(target))["state"] == "queued"
    if workers is not None:
        await control.repository.release(control.lease)
        resumed_lease = await workers.acquire(control.current["id"])
        assert resumed_lease is not None
        current = await control.repository.get(control.current["id"])
    else:
        current = await control.repository.transition(
            control.lease, control.current["id"], control.current["revision"], "running"
        )
        resumed_lease = control.lease

    async def resumed_validate():
        assert await control.repository.valid(resumed_lease)

    restored = WorkControl(
        control.repository,
        resumed_lease,
        current["source_key"],
        json.loads(current["source_json"]),
        resumed_validate,
        current=current,
    )
    result = await case.runner.run(
        (ChatMessage("user", "resume original steps"),),
        replace(runtime, work_control=restored),
        case.backend,
    )
    assert len(case.provider.requests) == 1
    assert env.domain.log == [
        ("lookup", {"q": "before control"}),
        ("workspace_write", {"path": "original tail"}),
    ]
    transcript = restored.session.transcript.request().messages
    results = [
        message
        for message in transcript
        if message.role == "tool" and message.tool_call_id == call.id
    ]
    # Business rebase may expose the same paired receipt as portable evidence;
    # the original Code parent and child identities remain the single authority.
    rows, tools, _root = await effect_rows(database, restored.current["id"])
    parents = [row for row in rows.values() if row["kind"] == "code_composition"]
    assert len(parents) == 1 and parents[0]["state"] == "accepted" and tools == 2
    body = json.loads(json.loads(parents[0]["receipt_json"])["result"])
    assert body["status"] == "completed" and all(
        op["status"] == "succeeded" for op in body["operations"]
    )
    assert len(results) <= 1
    assert restored.accepted_ending() == expected
    await restored.settle(pending_inputs=False)
    assert restored.current["state"] == (expected or "suspended")
    assert result.suppress_delivery is True
