"""Real request serialization and tool execution through receipt recovery."""

import json
from dataclasses import replace
from itertools import pairwise

import pytest
from tests.conftest import build_harness, make_settings
from tests.support.runtime_wire import install_wire

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatResponse,
    ChatTool,
    ModelResponseStatus,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.services.agent_runner import AgentRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
@pytest.mark.parametrize("recovery", ["committed", "incomplete"])
async def test_receipt_continues_original_wire_chain(database, protocol, recovery):
    def call(identity, arguments="{}"):
        return ChatResponse(
            "", 0, tool_calls=(ToolCall(identity, ToolFunction("work", arguments)),)
        )

    responses = iter(
        [
            call("first", '{"broken":' if recovery == "incomplete" else "{}"),
            *([ChatResponse("unsupported", 0)] if recovery == "committed" else []),
            call("next", '{"query":true}'),
            ChatResponse("核对好了，由我自己说明结果", 0),
        ]
    )
    provider = FakeLLMProvider(lambda _: next(responses))
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    client, captured = install_wire(chat, provider, protocol)
    complete = provider.complete

    async def receive(request):
        response = await complete(request)
        if recovery == "incomplete" and len(captured) == 1:
            return replace(response, status=ModelResponseStatus.INCOMPLETE)
        return response

    provider.complete = receive
    executed = []

    class Backend:
        def definitions(self, runtime, **kwargs):
            return (ChatTool("work", "work", {"type": "object"}),)

        def begin_batch(self, *args):
            pass

        def parallel_safe(self, *args):
            return False

        def is_side_effecting(self, *args):
            return True

        async def execute(self, name, arguments, runtime):
            executed.append(json.loads(arguments))
            return json.dumps(
                {
                    "ok": True,
                    "data": {"state": "invalidated"},
                    "mutation_committed": True,
                    "finalize_after_commit": True,
                }
            )

        def terminal_memory_reply(self):
            # The obsolete hook must never override the model or stop its loop.
            return "固定记忆模板"

        def response_feedback(self, text, runtime):
            return "没有对应的持久化回执，请核对后继续" if text == "unsupported" else None

        def finalize(self, text, runtime):
            return text

        def exhausted(self, runtime):
            raise AssertionError("receipt must not exhaust the loop")

    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="receipt",
        current_group_id=None,
        bot_user_id="9999",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=8,
    )
    try:
        result = await chat._agent_runner.run(
            (ChatMessage(role="user", content="处理后继续核对"),), runtime, Backend()
        )
    finally:
        await client.aclose()
    assert result.text == "核对好了，由我自己说明结果"
    assert result.model_requests == (4 if recovery == "committed" else 3)
    assert executed == ([{}, {"query": True}] if recovery == "committed" else [{"query": True}])
    sequence = "input" if protocol == "responses" else "messages"
    for before, after in pairwise(captured):
        assert after[sequence][: len(before[sequence])] == before[sequence]
        assert after["tools"] == before["tools"]
        assert after.get("tool_choice", "auto") == "auto"
    entries = captured[1][sequence]
    receipts = [
        e for e in entries if e.get("type") == "function_call_output" or e.get("role") == "tool"
    ]
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].get("output", receipts[0].get("content")))
    if recovery == "incomplete":
        assert receipt["error"] == "provider_response_incomplete"
        assert receipt["executed"] is False and receipt["mutation_committed"] is False
    else:
        assert receipt["data"]["state"] == "invalidated"


@pytest.mark.asyncio
async def test_real_memory_receipt_returns_to_model_without_reusing_write_authority(
    database, tmp_path
):
    from tests.conftest import MemorySender
    from tests.unit.test_commands_and_chat import inbound
    from tests.unit.test_memory_mutation import _service

    service, facts, _, _ = _service(database)
    responses_seen = []

    def respond(request):
        responses_seen.append(request)
        if len(responses_seen) <= 2:
            if len(responses_seen) == 2:
                receipt = json.loads([m.content for m in request.messages if m.role == "tool"][-1])
                assert receipt["ok"] and receipt["data"]["outcome"] == "committed"
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        str(len(responses_seen)),
                        ToolFunction(
                            "memory_change",
                            json.dumps(
                                {
                                    "operation": "create",
                                    "target": {
                                        "subject_ref": "current_speaker",
                                        "scope_type": "person",
                                    },
                                    "new_content": "现在住在上海",
                                    "memory_key": "location:home",
                                    "category": "location",
                                    "reason": "用户明确要求记住",
                                    "confidence": 0.96,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    ),
                ),
            )
        receipt = json.loads([m.content for m in request.messages if m.role == "tool"][-1])
        assert not receipt["ok"]  # A second write does not reuse this turn's authority.
        return "好，我记住你现在住上海了。"

    provider = FakeLLMProvider(respond)
    harness = build_harness(database, make_settings(database.url), provider)

    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState
    from qq_ai_bot.workspace.store import WorkspaceStore

    chat = harness.processor._chat
    chat._tools._memory_mutations = service
    chat._agent_runner.main_contract = MainAgentContract(
        chat, ShortState(WorkspaceStore(tmp_path / "state"))
    )
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound("记住我现在住在上海", message_id="memory-receipt-loop"), sender
    )
    assert result.reason == "chat"
    assert len(responses_seen) == 3
    assert not sender.messages
    assert len(await facts.list_person("1001", limit=20)) == 1
    for before, after in pairwise(provider.requests):
        assert after.tools == before.tools
        assert after.messages[: len(before.messages)] == before.messages
