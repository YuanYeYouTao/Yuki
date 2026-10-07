"""Common terminal operations use the frozen direct view and original receipts."""

import json
from dataclasses import replace

from tests.integration.test_codemode_runner import ACCEPT, Backend, call, runner_env
from tests.support.codemode_cases import requires_worker

from qq_ai_bot.codemode.tool_visibility import model_definitions
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse
from qq_ai_bot.sandbox.environment_tools import environment_tools

pytestmark = requires_worker


class TerminalBackend(Backend):
    def definitions(self, runtime, **kwargs):
        return (*super().definitions(runtime, **kwargs), *environment_tools())

    def is_side_effecting(self, name, arguments, runtime):
        return name == "terminal_exec"

    async def execute_call(self, invocation):
        name = invocation.call.function.name
        self.log.append((name, invocation.identity.operation_id))
        if name == "terminal_exec":
            data = {"run_id": "original-run", "status": "running", "pending": True}
        elif name == "terminal_read":
            data = {
                "run_id": "original-run",
                "status": "succeeded",
                "pending": False,
                "exit_code": 0,
                "output": "done",
                "next_cursor": 4,
            }
        else:
            data = {"status": "ready"}
        return json.dumps({"ok": True, "data": data})


async def test_terminal_exec_read_and_status_are_direct_without_composition(database, tmp_path):
    responses = iter(
        [
            call("task_control", ACCEPT, "accept"),
            call("terminal_exec", {"command": "printf done"}, "exec"),
            call("terminal_read", {"run_id": "original-run", "cursor": 0}, "read"),
            call("environment_status", {}, "status"),
            ChatResponse("done", 0),
        ]
    )
    chat, provider, control, runtime, _repo = await runner_env(database, tmp_path, responses)
    backend = TerminalBackend()
    visible = model_definitions(backend.definitions(None))
    result = await chat.runtime.runner.run(
        (ChatMessage("user", "run and check command"),),
        replace(runtime, fixed_tools=visible),
        backend,
    )
    assert result.text == "done"
    assert {"terminal_exec", "terminal_read", "environment_status"} <= {t.name for t in visible}
    assert all(request.tools == visible for request in provider.requests)
    assert [name for name, _ in backend.log] == [
        "terminal_exec",
        "terminal_read",
        "environment_status",
    ]
    assert len({key for _, key in backend.log}) == 3
    assert all("/c" not in key for _, key in backend.log)
    receipt = next(m for m in provider.requests[2].messages if m.tool_call_id == "exec")
    assert json.loads(receipt.content)["data"]["run_id"] == "original-run"
    assert json.loads(receipt.content)["data"]["pending"] is True
    assert control.current["tool_calls"] == 3
