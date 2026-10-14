"""Current Main compact retains original input, receipt and paid-budget facts."""

import json
from dataclasses import replace

import pytest
from tests.support.work_compaction_capacity_helpers import _runtime, _session
from tests.support.work_session import WorkSession, invoke_tool

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ModelResponseStatus,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.capacity import estimate_request_tokens
from qq_ai_bot.model_runtime.models import ModelExecutionPriority
from qq_ai_bot.runtime.work_repository import WorkCapacityError
from qq_ai_bot.services.context_compaction import EXECUTION_COMPACT_INSTRUCTION
from qq_ai_bot.services.turn_execution import TurnExecution
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def _paid_root(database, tmp_path):
    control, session, _ = await _session(database, tmp_path)
    initial = session.transcript.request().messages
    transcript = session.transcript
    transcript.append(ChatMessage("user", "a real public message"))
    transcript.append(ChatMessage("assistant", "a real reply"))
    await control.repository.checkpoint(
        control.lease, control.current["id"], None, models=3, tools=1
    )
    control.current = await control.repository.get(control.current["id"])
    return control, session, transcript, initial


@pytest.mark.parametrize("retained_inputs", [0, 2])
@pytest.mark.parametrize("provider_incomplete", [False, True])
async def test_current_compact_retains_original_inputs_and_business_resume(
    database, tmp_path, retained_inputs, provider_incomplete
):
    control, first, transcript, initial = await _paid_root(database, tmp_path)
    identity = control.current["id"]
    original_source = control.current["source_json"]
    first.progress["task_material"] = {
        "directives": [{"text": "keep the original restriction", "refs": ["goal"]}]
    }
    original_request = await first._original_request()
    input_ids = []
    for index in range(19):
        text = f"original requirement {index}: " + "specific requirement " * 100
        input_id = await control.repository.enqueue(
            control.lease.conversation_id,
            control.lease.generation,
            f"current-compact-input:{index}",
            kind="message",
            work_id=identity,
            ready=False,
        )
        assert await control.repository.prepare_input(input_id, {"text": text})
        input_ids.append(input_id)
        transcript.append(ChatMessage("user", text))
    await control.repository.stage(control.lease, input_ids, "original-inputs")
    await control.repository.consume(control.lease, "original-inputs")
    await first.save("paired")
    original = transcript.request()
    checkpoint = "Keep the original restriction and all uncompleted requirements."
    provider = FakeLLMProvider(
        lambda request: ChatResponse(
            content=checkpoint,
            latency_seconds=0,
            status=ModelResponseStatus.INCOMPLETE
            if provider_incomplete
            else ModelResponseStatus.COMPLETED,
            incomplete_reason="max_output_tokens" if provider_incomplete else None,
        )
    )
    runner, runtime = await _runtime(database, control, initial, provider)
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = transcript
    main = ChatRequest(messages=original.messages, max_output_tokens=8192)
    boundary = len(original.messages) - retained_inputs
    paid_prefix = (
        *original.messages[:boundary],
        ChatMessage("user", EXECUTION_COMPACT_INSTRUCTION),
    )
    budget = estimate_request_tokens(runner._capacity_request(replace(main, messages=paid_prefix)))
    if provider_incomplete and estimate_request_tokens(runner._capacity_request(main)) > budget:
        with pytest.raises(WorkCapacityError, match="model_request_capacity"):
            await execution.compact_current(0, main, ModelExecutionPriority.FOREGROUND, budget)
    else:
        candidate = await execution.compact_current(
            0, main, ModelExecutionPriority.FOREGROUND, budget
        )
        if provider_incomplete:
            assert candidate == main
        else:
            assert candidate.messages[: len(initial)] == initial
            if retained_inputs:
                assert candidate.messages[-retained_inputs:] == original.messages[-retained_inputs:]
    assert len(provider.requests) == 1
    assert provider.requests[0].messages == paid_prefix
    assert provider.requests[0].tools == main.tools
    if provider_incomplete:
        assert first.transcript.request() == original
        assert "execution_checkpoint" not in first.progress["task_material"]
    else:
        assert first.progress["task_material"]["execution_checkpoint"] == checkpoint
    resumed = WorkSession(control, first.contract)
    control.session = resumed
    restored = await resumed.restore(TurnTranscript((ChatMessage("user", "fresh business input"),)))
    material = json.loads(restored.request().messages[-1].content)
    assert material["original_request"] == original_request
    assert material["task_material"]["directives"][0]["text"] == "keep the original restriction"
    for index in range(19):
        assert restored.request().messages[-1].content.count(f"original requirement {index}:") == 1
    assert len(provider.requests) == 1
    saved = await control.repository.get(identity)
    assert saved["goal"] == "prepare an artifact"
    assert saved["source_json"] == original_source
    assert (saved["model_requests"], saved["tool_calls"]) == (4, 1)
    await control.repository.release(control.lease)


async def test_incomplete_compact_keeps_original_paired_work_and_paid_budget(database, tmp_path):
    control, session, transcript, initial = await _paid_root(database, tmp_path)
    before = transcript.request()
    provider = FakeLLMProvider(
        lambda request: ChatResponse(
            content="Incomplete paid checkpoint",
            latency_seconds=0,
            status=ModelResponseStatus.INCOMPLETE,
            incomplete_reason="max_output_tokens",
        )
    )
    runner, runtime = await _runtime(database, control, initial, provider)
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = transcript
    main = ChatRequest(messages=before.messages, max_output_tokens=8192)
    assert (
        await execution.compact_current(0, main, ModelExecutionPriority.FOREGROUND, 128000) == main
    )
    assert session.transcript.request() == before
    assert session.transcript.chain_id == transcript.chain_id
    assert len(provider.requests) == 1
    saved = await control.repository.get(control.current["id"])
    assert saved["goal"] == "prepare an artifact"
    assert (saved["model_requests"], saved["tool_calls"]) == (4, 1)
    await control.repository.release(control.lease)


async def test_current_compact_selects_a_fitting_prefix_without_splitting_tool_pairs(
    database, tmp_path
):
    control, session, transcript, initial = await _paid_root(database, tmp_path)
    for index in range(60):
        transcript.append(ChatMessage("assistant", f"Record {index}: " + "中文 observation " * 120))
    call = ToolCall("original-paired-call", ToolFunction("terminal_exec", "{}"))
    transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    invoked = []

    async def invoke():
        invoked.append(call.id)
        return json.dumps({"ok": True, "data": "original real result " * 1000})

    await session.save("response", (call,))
    result = await invoke_tool(session, call, invoke)
    original_call_key = session.call_key(call.id)
    transcript.append_result(call.id, result)
    await session.save("paired")
    original = transcript.request()
    provider = FakeLLMProvider(
        lambda request: "Verified prior records; keep the original tool receipt."
    )
    runner, runtime = await _runtime(database, control, initial, provider)
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = transcript
    main = ChatRequest(messages=original.messages, max_output_tokens=8192)
    # The real capacity fits the declaration but not its result. The paid
    # prefix must stop before the complete pair, never in its middle.
    halfway = (*original.messages[:-1], ChatMessage("user", EXECUTION_COMPACT_INSTRUCTION))
    budget = estimate_request_tokens(runner._capacity_request(replace(main, messages=halfway)))
    candidate = await execution.compact_current(0, main, ModelExecutionPriority.FOREGROUND, budget)
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[:-1] == original.messages[:-2]
    assert estimate_request_tokens(runner._capacity_request(provider.requests[0])) <= budget
    assert not any(message.tool_calls for message in provider.requests[0].messages)
    assert candidate.messages[-2:] == original.messages[-2:]
    assert candidate.messages[-2].tool_calls == (call,)
    assert candidate.messages[-1].tool_call_id == call.id
    assert await session.journal.effect_result(original_call_key) == result
    assert invoked == [call.id]
    assert (await control.repository.get(control.current["id"]))["model_requests"] == 4
    await control.repository.release(control.lease)
