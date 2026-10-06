"""Real request serialization and tool execution through receipt recovery."""

import json
from dataclasses import replace
from itertools import pairwise

import pytest
from tests.conftest import build_harness, make_settings

# P10: explicit Invocation fixture contract; existing assertions are retained.
from tests.support.agent_backend import StubAgentBackend
from tests.support.runtime_wire import install_wire

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatResponse,
    ChatTool,
    ModelResponseStatus,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.base import LLMMalformedFunctionCallError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.services.agent_runner import AgentRuntime


# New upstream replay uses this branch's typed Invocation boundary; assertions stay unchanged.
class _MalformedRecoveryBackend(StubAgentBackend):
    def __init__(self):
        self.executions = []
        self.exposure_confirmations = 0

    async def confirm_memory_prompt_exposure(self):
        self.exposure_confirmations += 1

    def definitions(self, runtime, **kwargs):
        return (ChatTool("effect_probe", "Apply the authorized effect", {"type": "object"}),)

    def begin_batch(self, *args):
        pass

    def parallel_safe(self, *args):
        return False

    def is_side_effecting(self, *args):
        return True

    async def execute_call(self, invocation):
        call = invocation.call
        self.executions.append((call.function.name, json.loads(call.function.arguments)))
        return '{"ok":true,"mutation_committed":true}'

    def finalize(self, text, runtime):
        return text

    def exhausted(self, runtime):
        raise AssertionError("malformed requests must stop through the typed failure")


async def _malformed_runtime(chat, *, max_requests=8):
    return AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="malformed-recovery",
        current_group_id=None,
        bot_user_id="9999",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=max_requests,
    )


@pytest.mark.parametrize("private_continuation", [False, True])
async def test_malformed_recovers_without_pseudo_call_and_executes_valid_effect_once(
    database, private_continuation
):
    checkpoint = ProviderContinuation(
        provider="fake", protocol="chat_completions", payload={"private": "unchanged checkpoint"}
    )
    steps = iter(
        [
            *(
                [
                    ChatResponse(
                        "",
                        0,
                        status=ModelResponseStatus.INCOMPLETE,
                        incomplete_reason="pause_turn",
                        continuation=checkpoint,
                    )
                ]
                if private_continuation
                else []
            ),
            LLMMalformedFunctionCallError(
                "sensitive malformed provider bytes must not be feedback"
            ),
            ChatResponse(
                "",
                0,
                tool_calls=(ToolCall("valid-effect", ToolFunction("effect_probe", '{"value":1}')),),
            ),
            ChatResponse("已核对真实回执", 0),
        ]
    )

    def respond(_request):
        step = next(steps)
        if isinstance(step, Exception):
            raise step
        return step

    provider = FakeLLMProvider(respond)
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    backend = _MalformedRecoveryBackend()
    initial = (ChatMessage("system", "fixed contract"), ChatMessage("user", "apply once"))
    result = await chat.runtime.runner.run(initial, await _malformed_runtime(chat), backend)
    assert result.text == "已核对真实回执"
    assert result.model_requests == (4 if private_continuation else 3)
    assert result.tool_calls_used == 1
    assert backend.executions == [("effect_probe", {"value": 1})]
    # A known malformed response still confirms that the request was seen.
    assert backend.exposure_confirmations == len(provider.requests)
    failed_index = 1 if private_continuation else 0
    failed, corrected = provider.requests[failed_index : failed_index + 2]
    assert corrected.tools == failed.tools and corrected.tool_choice == failed.tool_choice
    assert corrected.request_chain_id == failed.request_chain_id
    assert (
        corrected.continuation
        == failed.continuation
        == (checkpoint if private_continuation else None)
    )
    assert corrected.messages[: len(initial)] == initial
    entries = (*corrected.messages, *corrected.continuation_items)
    assert all(not item.tool_calls for item in entries if isinstance(item, ChatMessage))
    assert all(item.role != "assistant" for item in entries if isinstance(item, ChatMessage))
    feedback = [item for item in entries if isinstance(item, ChatMessage) and item not in initial]
    assert len(feedback) == 1 and feedback[0].role == "system"
    assert "sensitive malformed provider bytes" not in str(entries)
    assert "private" not in (feedback[0].content or "")


@pytest.mark.parametrize("max_requests,expected_requests", [(1, 1), (2, 2), (8, 3)])
async def test_malformed_stops_after_two_corrections_or_original_request_budget(
    database, max_requests, expected_requests
):
    def respond(_request):
        raise LLMMalformedFunctionCallError("malformed again")

    provider = FakeLLMProvider(respond)
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    backend = _MalformedRecoveryBackend()
    with pytest.raises(LLMMalformedFunctionCallError):
        await chat.runtime.runner.run(
            (ChatMessage("user", "apply once"),),
            await _malformed_runtime(chat, max_requests=max_requests),
            backend,
        )
    assert len(provider.requests) == expected_requests
    assert backend.executions == []
    assert all(request.tools == provider.requests[0].tools for request in provider.requests)
    assert all(request.tool_choice == "auto" for request in provider.requests)


async def test_paused_provider_tool_replays_without_synthetic_recovery_message(database):
    checkpoint = ProviderContinuation(
        provider="fake",
        protocol="chat_completions",
        payload={"paused": "server-side search"},
    )
    responses = iter(
        (
            ChatResponse(
                "",
                0,
                status=ModelResponseStatus.INCOMPLETE,
                incomplete_reason="pause_turn",
                continuation=checkpoint,
            ),
            ChatResponse("完成", 0),
        )
    )
    provider = FakeLLMProvider(lambda _request: next(responses))
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="pause-check",
        current_group_id=None,
        bot_user_id="9999",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=0,
        max_model_requests=2,
    )
    result = await chat.runtime.runner.run(
        (ChatMessage(role="user", content="搜索后回答"),), runtime, None
    )
    assert result.text == "完成"
    assert len(provider.requests) == 2
    assert provider.requests[1].continuation == checkpoint
    assert provider.requests[1].messages == provider.requests[0].messages
    assert provider.requests[1].continuation_items == ()


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

    class Backend(StubAgentBackend):
        def definitions(self, runtime, **kwargs):
            return (ChatTool("work", "work", {"type": "object"}),)

        def begin_batch(self, *args):
            pass

        def parallel_safe(self, *args):
            return False

        def is_side_effecting(self, *args):
            return True

        async def execute_call(self, invocation):
            arguments = invocation.call.function.arguments
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
        result = await chat.runtime.runner.run(
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
        return ChatResponse("", 0)

    provider = FakeLLMProvider(respond)
    harness = build_harness(database, make_settings(database.url), provider)

    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState
    from qq_ai_bot.workspace.store import WorkspaceStore

    chat = harness.processor._chat
    chat._tools._memory_mutations = service
    chat.runtime.runner.main_contract = MainAgentContract(
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


@pytest.mark.parametrize(
    "valid_write,accepted_work", [(True, False), (False, False), (None, False), (True, True)]
)
@pytest.mark.parametrize(
    "extra_feedback",
    [None, {"target": {"kind": "person", "id": "elsewhere"}}, {"artifact_id": "unrelated-file"}],
)
async def test_user_write_feedback_is_explicit_once_after_real_receipt(
    database, tmp_path, valid_write, accepted_work, extra_feedback
):
    await _replay_user_write_feedback(
        database, tmp_path, valid_write, accepted_work, extra_feedback
    )


@pytest.mark.parametrize("batch_order", ["write_then_send", "send_then_write"])
async def test_memory_feedback_in_same_batch_requires_observed_write_receipt(
    database, tmp_path, batch_order
):
    await _replay_user_write_feedback(database, tmp_path, True, False, None, batch_order)


async def _replay_user_write_feedback(
    database, tmp_path, valid_write, accepted_work, extra_feedback, batch_order=None
):
    """Replay real preparation/write/send; scripted output is not a live-model evaluation."""
    from tests.conftest import MemorySender
    from tests.support.social_identity_cases import social_env
    from tests.unit.test_commands_and_chat import inbound
    from tests.unit.test_memory_mutation import _service

    from qq_ai_bot.conversation.hydrate import require_primary_alias_for_conversation
    from qq_ai_bot.prompting.contracts import CORE_CONTRACT
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState
    from qq_ai_bot.workspace.store import WorkspaceStore

    env = await social_env(database, tmp_path)
    async with database.sessions() as reader:
        primary_alias = await require_primary_alias_for_conversation(
            reader, env.context.conversation_id
        )
    service, facts, _, _ = _service(database)
    requests = []
    text = (
        "是否保存尚未确认，需要先核对原结果。"
        if valid_write is None
        else "已记住你现在住在上海。"
        if valid_write
        else "这次没有保存：参数校验未通过。"
    )
    feedback_args = {"text": text, **({"work_report": {"kind": "final"}} if accepted_work else {})}
    write_request = 3 if accepted_work else 1
    sent_request = write_request + 2 + int(extra_feedback is not None)

    def respond(request):
        requests.append(request)
        # The ordinary entrypoint compiles the shared rule into the real static
        # instructions once; it is not injected from the user or a tool result.
        system = "\n\n".join(m.content or "" for m in request.messages if m.role == "system")
        assert system.count(CORE_CONTRACT) == 1
        assert "执行后用 send_message 给当前会话一次简短的结果反馈" in system
        assert "用户明确要求安静执行时遵从" in system
        if accepted_work and len(requests) == 1:
            assert not any(m.role == "tool" for m in request.messages)
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "accept",
                        ToolFunction(
                            "task_control",
                            json.dumps(
                                {
                                    "action": "accept",
                                    "goal": "保存用户要求的居住地记忆",
                                    "output_kind": "state_change",
                                    "reporting": "interactive",
                                }
                            ),
                        ),
                    ),
                ),
            )
        if accepted_work and len(requests) == 2:
            receipt = json.loads([m.content for m in request.messages if m.role == "tool"][-1])
            assert receipt["ok"]
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "start",
                        ToolFunction(
                            "send_message",
                            json.dumps(
                                {
                                    "text": "准备保存你要求的记忆。",
                                    "work_report": {"kind": "start"},
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    ),
                ),
            )
        if len(requests) == write_request:
            write_call = ToolCall(
                "write",
                ToolFunction(
                    "memory_change",
                    json.dumps(
                        {
                            "operation": "create",
                            "target": {
                                "subject_ref": "current_speaker",
                                "scope_type": "person",
                            },
                            "new_content": "" if valid_write is False else "现在住在上海",
                            "memory_key": "location:home",
                            "category": "location",
                            "reason": "用户明确要求记住",
                            "confidence": 0.96,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            calls = (write_call,)
            if batch_order is not None:
                premature = ToolCall(
                    "premature-feedback",
                    ToolFunction("send_message", json.dumps(feedback_args, ensure_ascii=False)),
                )
                calls = (
                    (write_call, premature)
                    if batch_order == "write_then_send"
                    else (premature, write_call)
                )
            return ChatResponse("", 0, tool_calls=calls)
        receipt = json.loads([m.content for m in request.messages if m.role == "tool"][-1])
        if len(requests) == write_request + 1:
            if batch_order is not None:
                paired = {
                    m.tool_call_id: json.loads(m.content)
                    for m in request.messages
                    if m.role == "tool"
                }
                rejected = paired["premature-feedback"]
                assert rejected["error"] == "delivery_requires_observed_result"
                assert rejected["ok"] is False and rejected["executed"] is False
                assert not [
                    action for action, _ in env.bot.calls if action == "send_group_msg"
                ]  # Neither order may send before observing the write.
                receipt = paired["write"]
            assert receipt["ok"] is (valid_write is True)
            if valid_write is None:
                assert receipt["uncertain"] is True
            elif valid_write:
                assert receipt["data"]["outcome"] == "committed"
            else:
                assert receipt.get("error") or receipt.get("error_code")
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "feedback",
                        ToolFunction(
                            "send_message",
                            json.dumps(
                                {**feedback_args, **(extra_feedback or {})}, ensure_ascii=False
                            ),
                        ),
                    ),
                ),
            )
        if extra_feedback is not None and len(requests) == write_request + 2:
            assert not receipt["ok"] and receipt["error"] in {
                "memory_feedback_current_text_only",
                "work_report_target_not_current",
            }
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "current-feedback",
                        ToolFunction("send_message", json.dumps(feedback_args, ensure_ascii=False)),
                    ),
                ),
            )
        assert receipt["ok"], receipt
        if accepted_work and len(requests) == sent_request:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "complete",
                        ToolFunction("task_control", '{"action":"complete","artifact_ids":[]}'),
                    ),
                ),
            )
        assert len(requests) == sent_request
        return ChatResponse("", 0)

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database,
        make_settings(database.url, enabled_groups_csv="20001", runtime_work_enabled=accepted_work),
        provider,
    )
    chat = harness.processor._chat
    chat._tools._memory_mutations = service
    if valid_write is None:
        # Synthetic unknown service boundary; the rest is the real capability,
        # receipt, Runner and explicit Social-delivery path, with no write replay.
        from unittest.mock import AsyncMock

        chat._tools._memory_change = AsyncMock(
            return_value=json.dumps(
                {
                    "ok": False,
                    "error": "verification_unavailable",
                    "uncertain": True,
                    "mutation_committed": None,
                }
            )
        )
    chat._tools.social_service = env.service
    chat.runtime.runner.main_contract = MainAgentContract(
        chat, ShortState(WorkspaceStore(tmp_path / "state"))
    )
    sender = MemorySender()
    message = replace(
        inbound(
            "记住我现在住在上海",
            message_id=f"write-feedback-{valid_write}",
            user_id="10001",
            group_id="20001",
            mentions_bot=True,
        ),
        bot_user_id="80001",
        conversation_id=env.context.conversation_id,
        legacy_conversation_key=primary_alias,
        person_id=env.person,
        space_id=env.space,
        presence_id=env.presence,
    )
    result = await harness.processor.handle(message, sender)
    assert result.reason == "chat" and result.sent_messages == 1 + int(accepted_work), [
        [(m.tool_call_id, m.content) for m in request.messages if m.role == "tool"]
        for request in requests
    ]
    assert not sender.messages  # Only the explicit Social send reaches the gateway.
    sends = [params for action, params in env.bot.calls if action == "send_group_msg"]
    assert len(sends) == 1 + int(accepted_work)
    assert sends[-1]["message"] == [{"type": "text", "data": {"text": text}}]
    if accepted_work:
        assert sends[0]["message"] == [{"type": "text", "data": {"text": "准备保存你要求的记忆。"}}]
    assert len(await facts.list_person("10001", limit=20)) == int(valid_write is True)
    assert len(requests) == sent_request
    for before, after in pairwise(requests):
        assert before.tools == after.tools
        assert after.messages[: len(before.messages)] == before.messages
