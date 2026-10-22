"""A shared soft watermark maintains context without becoming a capacity limit."""

from dataclasses import replace

import pytest
from tests.support.work_compaction_capacity_helpers import _runtime, _session
from tests.unit.test_current_context_compaction import _append_public, _public_context

from yuki.domain.messages import (
    ChatMessage,
    ChatResponse,
    ChatTool,
    ModelResponseStatus,
    ToolCall,
    ToolFunction,
)
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.capacity import estimate_request_tokens
from yuki.model_runtime.models import ModelExecutionPriority
from yuki.runtime.work_repository import WorkCapacityError
from yuki.services.context_compaction import CompactReceipt
from yuki.services.turn_execution import TurnExecution
from yuki.services.turn_transcript import DispatchOrigin, TurnTranscript
from yuki.web.models import WebMode


@pytest.mark.parametrize("entry", ["ordinary", "work", "worker"])
@pytest.mark.parametrize("trigger", [96000, 400000])
async def test_shared_watermark_compacts_public_before_main_without_profile_limit(
    database, tmp_path, entry, trigger
):
    control, session, initial = await _session(database, tmp_path, worker=entry == "worker")
    await _append_public(database, "A public decision to preserve. " * 12000)
    context = await _public_context(database, control, initial)
    coverage = max(context.fragments.event_ids)
    initial = (*context.messages, ChatMessage("user", "private current request"))
    session.transcript = TurnTranscript(initial)

    def respond(request):
        if "Host 会话上下文压缩要求" in (request.messages[-1].content or ""):
            return "The public decision remains in effect."
        return "Original request completed."

    provider = FakeLLMProvider(respond)
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(
        runtime,
        work_control=None if entry == "ordinary" else control,
        public_compaction=context,
        execution_id=f"soft-public-{entry}",
        max_model_requests=5,
        auxiliary_requests=[0],
        fixed_tools=(ChatTool("probe_read", "Stable read capability", {"type": "object"}),),
        runtime_config=replace(
            runtime.runtime_config,
            context=replace(runtime.runtime_config.context, compact_trigger_tokens=trigger),
        ),
    )
    result = await runner.run(initial, runtime, None)
    assert result.text == "Original request completed."
    saved = await context.repository.read(context.version.conversation_id)
    if trigger == 96000:
        assert len(provider.requests) == 2
        compact, main = provider.requests
        assert compact.messages[0] == main.messages[0] == initial[0]
        assert compact.tools == main.tools == runtime.fixed_tools
        assert not any(
            "private current request" in (item.content or "") for item in compact.messages
        )
        assert "private current request" in str(main.messages)
        assert "The public decision remains in effect." in str(main.messages)
        assert "A public decision to preserve. " * 10 not in str(main.messages)
        assert saved.selected_summary_kind == "compact"
        assert saved.selected_summary_coverage == coverage
        assert result.model_requests == 2
    else:
        assert len(provider.requests) == result.model_requests == 1
        assert "A public decision to preserve. " * 10 in str(provider.requests[0].messages)
        assert saved is None
    if entry != "ordinary":
        assert control.requests_started == len(provider.requests)


async def test_soft_watermark_does_not_require_summary_below_trigger_or_recompact_checkpoint(
    database, tmp_path
):
    control, session, initial = await _session(database, tmp_path)
    session.transcript.append(ChatMessage("assistant", "An actual completed finding. " * 12000))
    provider = FakeLLMProvider(lambda request: "A long valid checkpoint. " * 16000)
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(runtime, max_model_requests=10, auxiliary_requests=[0])
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = session.transcript
    prepared = await execution.prepare_request(0, (), WebMode.DISABLED)
    assert (
        estimate_request_tokens(prepared.request)
        > runtime.runtime_config.context.compact_trigger_tokens
    )
    assert len(provider.requests) == 1
    await execution.prepare_request(0, (), WebMode.DISABLED)
    assert len(provider.requests) == 1
    execution.state.transcript.append(ChatMessage("assistant", "A new completed finding."))
    await execution.prepare_request(0, (), WebMode.DISABLED)
    assert len(provider.requests) == 2
    assert control.requests_started == 2


@pytest.mark.parametrize("outcome", ["tool_call", "incomplete", "empty"])
async def test_invalid_soft_checkpoint_preserves_recovery_request_and_does_not_execute_calls(
    database, tmp_path, outcome
):
    control, session, initial = await _session(database, tmp_path)
    session.transcript.append(ChatMessage("assistant", "A completed finding. " * 15000))
    invalid_call = ToolCall("not-executed", ToolFunction("send", '{"text":"wrong"}'))
    responses = {
        "tool_call": ChatResponse(content="", latency_seconds=0, tool_calls=(invalid_call,)),
        "incomplete": ChatResponse(
            content="An unfinished checkpoint",
            latency_seconds=0,
            status=ModelResponseStatus.INCOMPLETE,
        ),
        "empty": "",
    }
    provider = FakeLLMProvider(lambda request: responses[outcome])
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(runtime, max_model_requests=5, auxiliary_requests=[0])
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = session.transcript
    await execution._finalize_initial_layout()
    session.uses_recovery_transcript = True
    chain = execution.state.transcript.chain_id
    original = execution.state.transcript.entries()
    prepared = await execution.prepare_request(0, (), WebMode.DISABLED)
    assert prepared.request.messages == original
    assert prepared.sequence.origin is DispatchOrigin.WORK_RECOVERY
    assert prepared.request.request_chain_id == chain == execution.state.transcript.chain_id
    assert control.tools_started == 0
    assert len(provider.requests) == 1
    failed = session.progress["context_compaction"]
    assert failed["phase"] == "paired" and failed["valid"] is False


async def test_soft_noop_keeps_recovery_origin_and_original_chain(database, tmp_path):
    control, session, initial = await _session(database, tmp_path)
    initial = (*initial[:-1], ChatMessage("user", "Current material. " * 30000))
    session.transcript = TurnTranscript(initial)
    session.uses_recovery_transcript = True
    provider = FakeLLMProvider()
    runner, runtime = await _runtime(database, control, initial, provider)
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = session.transcript
    prepared = await execution.prepare_request(0, (), WebMode.DISABLED)
    assert prepared.sequence.origin is DispatchOrigin.WORK_RECOVERY
    assert prepared.request.request_chain_id == session.transcript.chain_id
    assert provider.requests == []


async def test_large_current_input_without_eligible_history_still_runs(database, tmp_path):
    control, _, initial = await _session(database, tmp_path)
    initial = (*initial[:-1], ChatMessage("user", "Current unprocessed material. " * 18000))
    provider = FakeLLMProvider(lambda request: "Read the current material.")
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(runtime, max_model_requests=5, auxiliary_requests=[0])
    result = await runner.run(initial, runtime, None)
    assert result.text == "Read the current material."
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[-1] == initial[-1]


async def test_ordinary_invalid_paid_checkpoint_is_reused_after_cold_restore(database, tmp_path):
    control, _, initial = await _session(database, tmp_path)
    await _append_public(database, "An unchanged public decision. " * 12000)
    context = await _public_context(database, control, initial)
    initial = (*context.messages, ChatMessage("user", "Current request"))
    provider = FakeLLMProvider(lambda request: "")
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(
        runtime,
        work_control=None,
        public_compaction=context,
        execution_id="ordinary-invalid-paid",
        max_model_requests=5,
        auxiliary_requests=[0],
    )
    execution = TurnExecution(runner, initial, runtime, None)
    original = execution.state.transcript.entries()
    first = await execution.prepare_request(0, (), WebMode.DISABLED)
    assert first.request.messages == original
    receipt = CompactReceipt(context, runtime.execution_id, runtime.actor_user_id)
    state = await receipt.read()
    assert state["phase"] == "paired" and state["valid"] is False
    assert state["error_category"] == "LLMEmptyResponseError"
    assert "model_response" not in state
    recovered = TurnExecution(runner, initial, runtime, None)
    await recovered.restore_compact_receipt()
    prepared = await recovered.prepare_request(0, (), WebMode.DISABLED)
    assert prepared.request.messages == first.request.messages
    assert len(provider.requests) == 1
    assert runtime.auxiliary_requests == [1]
    enlarged = (*initial[:-1], ChatMessage("user", "New unprocessed current material. " * 24000))
    over_capacity = TurnExecution(runner, enlarged, runtime, None)
    with pytest.raises(WorkCapacityError, match="model_request_capacity"):
        await over_capacity.compact_current(
            0,
            replace(first.request, messages=enlarged),
            ModelExecutionPriority.FOREGROUND,
            125000,
        )
    assert len(provider.requests) == 1
