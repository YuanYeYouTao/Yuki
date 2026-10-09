"""Native VM continuation and main's caller completion share the original Work."""

import hashlib
import json
from dataclasses import replace

import pytest
from tests.conftest import build_harness, make_settings
from tests.integration.test_work_result_handoff import observed_receipts
from tests.support.codemode_cases import BINARY, requires_worker, worker
from tests.unit.test_caller_work_completion import CallerBackend, caller_case
from tests.unit.test_work_delivery_ownership import call

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState

pytestmark = requires_worker


class CodeCallerBackend(CallerBackend):
    async def execute_call(self, invocation):
        # The single-send fixture uses a fixed Social call ID. Each real code
        # child must instead bind its original invocation, as production does.
        control = invocation.context.runtime.work_control
        self.owners.append(control.current["id"])
        result = await self.env.service.execute(
            invocation.call.function.name,
            json.loads(invocation.call.function.arguments),
            replace(
                self.env.context,
                turn_id=control.current["id"],
                call_id=invocation.identity.operation_id,
            ),
        )
        return json.dumps({"ok": result["status"] == "succeeded", "data": result})


@pytest.mark.parametrize("result", ["internal result", ""])
async def test_pending_vm_then_caller_completion_resumes_without_duplicate_sends(
    database, tmp_path, result
):
    env, provider, _service, runtime = await caller_case(
        database,
        tmp_path,
        [
            call(
                "execute_code",
                {
                    "code": "for i in range(3):\n"
                    "    await yuki_send_message({'text': 'synthetic item ' + str(i)})"
                },
                "original-composition",
            ),
            call("task_control", {"action": "complete", "result": result}, "finish"),
        ],
    )
    settings = make_settings(
        database.url,
        runtime_work_enabled=True,
        code_mode_enabled=True,
        code_mode_worker_path=BINARY,
        code_mode_worker_sha256=hashlib.sha256(BINARY.read_bytes()).hexdigest(),
        code_mode_launcher_path=worker().launcher_path,
        code_mode_launcher_sha256=worker().launcher_sha256,
    )
    chat = build_harness(database, settings, provider).processor._chat
    service = chat.runtime.main_turns
    runner = chat.runtime.runner
    runner.code_mode_settings = settings
    runner.main_contract = MainAgentContract(chat, ShortState(env.store), code_enabled=True)
    messages = (ChatMessage("user", "send three distinct items and finish"),)
    # Two child calls exhaust the first activation. The next activation must
    # resume the original VM before asking for complete, rather than rerun it.
    bounded = replace(runtime, max_model_requests=1)
    first = await service.run(messages, bounded, CodeCallerBackend(env))
    assert first.work_state == "queued" and first.model_requests == 1, first.outcome
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 2
    scripted = provider._responder

    def respond(request):
        control = current_work_control.get()
        body = json.loads(observed_receipts(request.messages)["original-composition"])
        assert body["ok"] is True and body["status"] == "completed"
        assert control.accepted is None
        return scripted(request)

    provider._responder = respond
    # The resumed VM finishes its third send, then complete(result) in the
    # same activation ends it: no further model request is bought. The
    # empty result is legal only because the original sends are confirmed.
    second = await service.run(messages, bounded, CodeCallerBackend(env))
    assert second.work_state == "completed" and second.work_id == first.work_id
    assert second.model_requests == 1 and second.text == result
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 3
    assert len(provider.requests) == 2
    original_calls = list(env.bot.calls)
    row = await WorkRepository(database).get(first.work_id)
    assert row["model_requests"] == 2 and row["tool_calls"] == 3 and row["sent_messages"] == 3
    assert json.loads(row["checkpoint_json"])["sync_result"] == result
    repeated = await service.run(messages, runtime, CodeCallerBackend(env))
    assert repeated.work_state == "completed" and repeated.model_requests == 0
    assert repeated.text == second.text
    assert env.bot.calls == original_calls and len(provider.requests) == 2
