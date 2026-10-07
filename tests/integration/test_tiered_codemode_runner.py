"""Real Monty composes hidden tools while direct calls remain fenced."""

import json
from dataclasses import replace

import pytest
from tests.integration.test_codemode_runner import ACCEPT, Backend, call, runner_env
from tests.support.codemode_cases import effect_rows, requires_worker
from tests.support.parent_receipts import parent_receipts

from qq_ai_bot.codemode.api_projection import project
from qq_ai_bot.codemode.tool_visibility import LOOKUP_TOOLS, model_definitions
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ChatTool
from qq_ai_bot.runtime.work_control import WorkControl

pytestmark = requires_worker


class TieredBackend(Backend):
    def __init__(self, *, denied=False):
        super().__init__()
        self.denied = denied

    def definitions(self, runtime, **kwargs):
        terminal = ChatTool(
            "terminal_exec",
            "specialized command",
            {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        )
        return tuple(
            sorted((*super().definitions(runtime), terminal, LOOKUP_TOOLS), key=lambda t: t.name)
        )

    async def execute_call(self, invocation):
        if (
            self.denied
            and json.loads(invocation.call.function.arguments).get("command") == "second"
        ):
            return '{"ok":false,"executed":false,"error":"capability_no_longer_authorized"}'
        return await super().execute_call(invocation)


@pytest.mark.parametrize("segmented", [False, True])
@pytest.mark.parametrize("denied", [False, True])
async def test_discovery_hidden_child_authorization_and_original_resume(
    database, tmp_path, segmented, denied
):
    program = (
        "a = await yuki_terminal_exec({'command': 'first'})\n"
        "if a['ok']:\n    b = await yuki_terminal_exec({'command': 'second'})\n"
        "a['status']"
    )
    responses = iter(
        [
            call("lookup_tools", {"name": "terminal_exec"}, "discover"),
            call("task_control", ACCEPT, "accept"),
            call("terminal_exec", {"command": "forged-direct"}, "forged"),
            call("execute_code", {"code": program}, "code"),
            *(
                [call("task_control", {"action": "fail", "reason": "permission denied"}, "fail")]
                if denied
                else []
            ),
            ChatResponse("permission denied" if denied else "done", 0),
        ]
    )
    chat, provider, control, runtime, repo = await runner_env(database, tmp_path, responses)
    backend = TieredBackend(denied=denied)
    complete = backend.definitions(None)
    visible = model_definitions(complete)
    api = project(complete, "tiered-test")
    runtime = replace(
        runtime, fixed_tools=visible, script_api=api, max_tool_calls=1 if segmented else 8
    )
    initial = (ChatMessage("user", "execute two commands in order"),)
    result = await chat.runtime.runner.run(initial, runtime, backend)
    active = control
    if segmented:
        assert result.work_state == "queued"
        assert len(backend.log) == 1
        active = WorkControl(repo, control.lease, "code-runner", {}, control.validate)
        active.current = await repo.get(control.current["id"])
        result = await chat.runtime.runner.run(
            initial, replace(runtime, work_control=active, max_tool_calls=8), backend
        )
    # Standalone Runner fixtures leave lifecycle settlement to their caller,
    # just as main/worker entrypoints do; persist the actual proposed ending.
    await active.settle(delivered=False, pending_inputs=False)
    if denied:
        assert result.text == "permission denied"
        assert (await repo.get(active.current["id"]))["state"] == "failed"
    else:
        assert result.text == "done"
    assert all(request.tools == visible for request in provider.requests)
    assert "terminal_exec" not in {tool.name for tool in visible}
    discovery = next(m for m in provider.requests[1].messages if m.tool_call_id == "discover")
    assert json.loads(discovery.content)["data"]["parameters"] == api.schemas["terminal_exec"]
    forged = next(m for m in provider.requests[3].messages if m.tool_call_id == "forged")
    assert json.loads(forged.content)["error"] == "tool_not_declared"
    paired = parent_receipts(provider.requests[-1], "code")
    assert len(paired) == 1
    body = json.loads(paired[0])
    if denied:
        assert [name for name, _ in backend.log] == ["terminal_exec"]
        assert body["error"] == "admission_closed"
        # The parent contains the bounded summary; verify the exact permission
        # refusal in the durable original child receipt, rather than demanding
        # that all child bodies be duplicated into the model response.
        rows, _, _ = await effect_rows(database, active.current["id"])
        child_id = backend.log[0][1].removesuffix("c0") + "c1"
        receipt = json.loads(rows[child_id]["receipt_json"])
        assert json.loads(receipt["result"])["error"] == "capability_no_longer_authorized"
    else:
        assert body["result"] == "succeeded"
        assert [name for name, _ in backend.log] == ["terminal_exec", "terminal_exec"]
        assert len({identity for _, identity in backend.log}) == 2
        assert all("/c" in identity for _, identity in backend.log)
        assert (await repo.get(active.current["id"]))["tool_calls"] == 2
