"""A model must observe original paired receipts before completing resumed Work."""

import json
from dataclasses import replace

import pytest
from tests.integration.test_codemode_runner import ACCEPT, Backend, call, runner_env
from tests.support.codemode_cases import requires_worker
from tests.support.parent_receipts import observation_bodies

from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_supervisor import settle

pytestmark = requires_worker
MARKER = "result-known-only-to-the-business-backend"


class ResultBackend(Backend):
    async def execute_call(self, invocation):
        result = json.loads(await super().execute_call(invocation))
        result["data"]["observed"] = MARKER
        return json.dumps(result)


def observed_receipts(messages):
    """A fresh business chain carries evidence, not the retired protocol."""
    results = {m.tool_call_id: m.content for m in messages if m.tool_call_id}
    for message in messages:
        if message.role != "user" or not message.content:
            continue
        for material in observation_bodies(message.content):
            if material.get("kind") == "work_unobserved_tool_round":
                for item in material["calls"]:
                    assert item["call_id"] not in results
                    results[item["call_id"]] = item["result"]
    return results


@pytest.mark.parametrize(
    "mode,count,script_error",
    [
        ("direct", 5, False),
        ("code", 5, False),
        ("code", 6, False),
        ("code", 5, True),
        ("code", 6, True),
    ],
)
async def test_quantum_result_and_error_are_observed_once_without_effect_replay(
    database, tmp_path, mode, count, script_error
):
    if mode == "code":
        program = (
            f"for i in range({count - 1}):\n    await yuki_lookup({{'q': i}})\n"
            "r = await yuki_workspace_write({'path': 'original'})\n"
            + ("r.ok" if script_error else "r['data']['observed']")
        )
        response = call("execute_code", {"code": program}, "original")
    else:
        response = ChatResponse(
            "",
            0,
            tool_calls=(
                *(
                    ToolCall(f"read-{i}", ToolFunction("lookup", json.dumps({"q": i})))
                    for i in range(count - 1)
                ),
                ToolCall("original", ToolFunction("workspace_write", '{"path":"original"}')),
            ),
        )
    chat, provider, control, runtime, repo = await runner_env(
        database,
        tmp_path,
        iter([call("task_control", ACCEPT, "accept"), response]),
        max_tool_calls=5,
    )
    backend = ResultBackend()
    original_chat = (ChatMessage("user", "old creation-time chat must retire"),)
    # No model request remains for the optional closing handoff; the unseen
    # result must still be carried by the next activation.
    result = await chat.runtime.runner.run(
        original_chat, replace(runtime, max_model_requests=2), backend
    )
    assert result.work_state == "queued"
    await settle(control, delivered=False, pending_inputs=False)
    old_chain = control.session.transcript.chain_id
    fresh = WorkControl(
        repo, control.lease, control.source_key, dict(control.source), control.validate
    )
    fresh.bind_context_access(control.context_access)
    fresh.current = await repo.get(control.current["id"])
    seen = []

    def respond(request):
        if count == 5:
            assert all(m.content != original_chat[0].content for m in request.messages)
        paired = observed_receipts(request.messages)
        body = json.loads(paired["original"])
        if not seen:
            seen.append(body)
            if script_error:
                assert body["error"] == "code_runtime"
                assert "AttributeError" in body["detail"]
                return call(
                    "execute_code", {"code": "r = await yuki_lookup({'q': 99})\nr['ok']"}, "repair"
                )
            assert MARKER in paired["original"]
        return call("task_control", {"action": "complete"}, "complete")

    provider._responder = respond
    result = await chat.runtime.runner.run(
        (ChatMessage("user", "current approved chat"),),
        replace(runtime, work_control=fresh),
        backend,
    )
    assert result.work_state == "completed"
    assert len(seen) == 1
    assert [name for name, _ in backend.log].count("workspace_write") == 1
    assert len({key for _, key in backend.log}) == len(backend.log)
    assert (await repo.get(fresh.current["id"]))["tool_calls"] == count + int(script_error)
    assert not await fresh.has_unresolved_effects()
    # F9: fully paired compositions rebase in this same activation before
    # dispatch, whether settlement happened before or during the resume.
    assert fresh.session.transcript.chain_id != old_chain


@pytest.mark.parametrize("mode", ["direct", "code"])
async def test_three_quantum_handoffs_keep_each_new_result_and_a_valid_task_anchor(
    database, tmp_path, mode
):
    def batch(index):
        if mode == "code":
            return call(
                "execute_code",
                {
                    "code": f"for i in range(4):\n    await yuki_lookup({{'q': i + {index * 5}}})\n"
                    f"r = await yuki_workspace_write({{'path': 'output-{index}'}})\n"
                    "r['data']['observed']"
                },
                f"round-{index}",
            )
        return ChatResponse(
            "",
            0,
            tool_calls=(
                *(
                    ToolCall(
                        f"round-{index}-{i}",
                        ToolFunction("lookup", json.dumps({"q": i + index * 5})),
                    )
                    for i in range(4)
                ),
                ToolCall(
                    f"round-{index}-4",
                    ToolFunction("workspace_write", json.dumps({"path": f"output-{index}"})),
                ),
            ),
        )

    chat, provider, control, runtime, repo = await runner_env(
        database,
        tmp_path,
        iter(
            [
                call(
                    "task_control",
                    {**ACCEPT, "reporting": "quiet", "deliver_artifacts": False},
                    "accept",
                ),
                batch(0),
            ]
        ),
        max_tool_calls=5,
    )
    backend = ResultBackend()
    result = await chat.runtime.runner.run(
        (ChatMessage("user", "retired creation chat"),),
        replace(runtime, max_model_requests=2),
        backend,
    )
    for index in range(1, 4):
        assert result.work_state == "queued"
        await settle(control, delivered=False, pending_inputs=False)
        fresh = WorkControl(
            repo, control.lease, control.source_key, dict(control.source), control.validate
        )
        fresh.bind_context_access(control.context_access)
        fresh.current = await repo.get(control.current["id"])

        def respond(request, index=index):
            receipts = observed_receipts(request.messages)
            if "complete" in receipts:
                return ChatResponse("done", 0)
            expected = (
                {f"round-{index - 1}"}
                if mode == "code"
                else {f"round-{index - 1}-{i}" for i in range(5)}
            )
            assert set(receipts) == expected
            assert all(MARKER in receipt for receipt in receipts.values())
            assert all(m.content != "retired creation chat" for m in request.messages)
            return (
                batch(index)
                if index < 3
                else call("task_control", {"action": "complete"}, "complete")
            )

        provider._responder = respond
        result = await chat.runtime.runner.run(
            (ChatMessage("user", "current chat"),),
            replace(runtime, work_control=fresh, max_model_requests=1 if index < 3 else 8),
            backend,
        )
        control = fresh
    assert result.work_state == "completed"
    assert len(backend.log) == 15
    assert len({key for _, key in backend.log}) == 15
    assert (await repo.get(control.current["id"]))["tool_calls"] == 15


@pytest.mark.parametrize("mode", ["direct", "code"])
async def test_segment_allows_one_handoff_request_before_retiring_observed_working_data(
    database, tmp_path, mode
):
    chat, provider, control, runtime, repo = await runner_env(
        database, tmp_path, iter(()), max_tool_calls=5
    )
    assert json.loads(
        await control.execute(
            "task_control", {**ACCEPT, "reporting": "quiet", "deliver_artifacts": False}, "accept"
        )
    )["ok"]
    backend = ResultBackend()
    seen = []
    if mode == "code":
        batch = call(
            "execute_code",
            {
                "code": "rows = []\nfor i in range(5):\n    r = await yuki_lookup({'q': i})\n"
                "    rows.append(r['data'])\nrows"
            },
            "batch",
        )
    else:
        batch = ChatResponse(
            "",
            0,
            tool_calls=tuple(
                ToolCall(f"read-{i}", ToolFunction("lookup", json.dumps({"q": i})))
                for i in range(5)
            ),
        )

    def respond(request):
        if not seen:
            seen.append("business")
            return batch
        receipts = observed_receipts(request.messages)
        if mode == "code":
            rows = json.loads(receipts["batch"])["result"]
        else:
            rows = [json.loads(receipts[f"read-{i}"])["data"] for i in range(5)]
        assert [row["q"] for row in rows] == list(range(5))
        assert all(row["observed"] == MARKER for row in rows)
        assert any(m.content and "work_segment_handoff" in m.content for m in request.messages)
        seen.append("handoff")
        return call(
            "task_control",
            {
                "action": "update",
                "context_note": {
                    "version": 1,
                    "facts": [{"text": json.dumps(rows), "refs": ["goal"]}],
                    "unresolved": [],
                    "next_steps": [
                        {"text": "Write the collected rows, then complete", "refs": ["goal"]}
                    ],
                },
            },
            "handoff",
        )

    provider._responder = respond
    result = await chat.runtime.runner.run(
        (ChatMessage("user", "original chat"),), runtime, backend
    )
    assert result.work_state == "queued"
    assert seen == ["business", "handoff"]
    assert len(provider.requests) == 2
    await settle(control, delivered=False, pending_inputs=False)
    fresh = WorkControl(
        repo, control.lease, control.source_key, dict(control.source), control.validate
    )
    fresh.bind_context_access(control.context_access)
    fresh.current = await repo.get(control.current["id"])

    def finish(request):
        materials = [
            json.loads(m.content)
            for m in request.messages
            if m.content
            and m.content.startswith("{")
            and json.loads(m.content).get("kind") == "work_current_material"
        ]
        rows = json.loads(materials[-1]["context_note"]["facts"][0]["text"])
        assert [row["q"] for row in rows] == list(range(5))
        assert all(m.content != "original chat" for m in request.messages)
        if "write" not in observed_receipts(request.messages):
            return call("workspace_write", {"path": "collected", "text": json.dumps(rows)}, "write")
        return call("task_control", {"action": "complete"}, "complete")

    provider._responder = finish
    result = await chat.runtime.runner.run(
        (ChatMessage("user", "current chat"),), replace(runtime, work_control=fresh), backend
    )
    assert result.work_state == "completed"
    assert len(backend.log) == 6 and len({key for _, key in backend.log}) == 6
    assert (await repo.get(fresh.current["id"]))["tool_calls"] == 6


async def test_handoff_is_one_request_and_cannot_execute_more_business_calls(database, tmp_path):
    chat, provider, control, runtime, repo = await runner_env(
        database, tmp_path, iter(()), max_tool_calls=5
    )
    assert json.loads(await control.execute("task_control", ACCEPT, "accept"))["ok"]
    backend = ResultBackend()

    def respond(request):
        if len(provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=tuple(
                    ToolCall(f"read-{i}", ToolFunction("lookup", json.dumps({"q": i})))
                    for i in range(5)
                ),
            )
        assert len(provider.requests) == 2
        assert any(m.content and "work_segment_handoff" in m.content for m in request.messages)
        return call("workspace_write", {"path": "must-not-execute"}, "over-quota")

    provider._responder = respond
    result = await chat.runtime.runner.run((ChatMessage("user", "bounded task"),), runtime, backend)
    assert result.work_state == "queued"
    assert len(provider.requests) == 2
    assert len(backend.log) == 5
    assert all(name == "lookup" for name, _ in backend.log)
    assert (await repo.get(control.current["id"]))["tool_calls"] == 5
    assert not await control.has_unresolved_effects()
