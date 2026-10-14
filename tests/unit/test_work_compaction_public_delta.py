"""Public chat deltas survive private-tail compaction and paid-stage recovery."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.support.work_compaction_capacity_helpers import _runtime, _session
from tests.support.work_session import WorkSession, invoke_tool

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.model_runtime.models import ModelExecutionPriority
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.services.turn_execution import TurnExecution
from qq_ai_bot.services.turn_transcript import TurnTranscript


@pytest.mark.parametrize("restart_paid", [False, True])
async def test_actual_main_keeps_public_delta_and_reuses_paid_response(
    database, tmp_path, monkeypatch, restart_paid
):
    control, session, initial = await _session(database, tmp_path)
    current_initial = session.transcript.request().messages
    call = ToolCall("original-read", ToolFunction("read_probe", "{}"))
    session.transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    invoke = AsyncMock(return_value=json.dumps({"ok": True, "body": "retained probe"}))
    output = await invoke_tool(session, call, invoke)
    session.transcript.append_result(call.id, output)
    session.transcript.append(ChatMessage("assistant", "temporary research " * 6000))
    ambient = ChatMessage("user", "public-ambient-must-remain-once")
    session.transcript.append(ambient)
    await session.save("paired")
    original_chain = session.transcript.chain_id
    async with database.sessions() as reader:
        receipt = dict((await reader.execute(select(effects))).mappings().one())

    def respond(request):
        assert ambient.content not in request.messages[-1].content
        return "Verified findings. Continue original task."

    provider = FakeLLMProvider(respond)
    runner, runtime = await _runtime(database, control, current_initial, provider)
    runtime = replace(runtime, auxiliary_requests=[0])
    execution = TurnExecution(runner, current_initial, runtime, None)
    execution.state.transcript = session.transcript
    execution.state.public_tail = [ambient]
    main = ChatRequest(messages=session.transcript.request().messages, max_output_tokens=8192)
    if restart_paid:

        async def fail_candidate():
            raise WorkConflict("candidate_publish_failed")

        monkeypatch.setattr(session, "finish_context_compaction", fail_candidate)
        with pytest.raises(WorkConflict, match="candidate_publish_failed"):
            await execution.compact_current(0, main, ModelExecutionPriority.FOREGROUND, 128000)
        assert session.progress["context_compaction"]["response"]
        assert len(provider.requests) == 1
        await database.close()
        restored = WorkSession(control, session.contract)
        control.session = restored
        await restored.restore(TurnTranscript((*initial[:-1], ambient, initial[-1])))
        session = restored
    else:
        await execution.compact_current(0, main, ModelExecutionPriority.FOREGROUND, 128000)
    candidate = session.transcript
    assert len(provider.requests) == 1
    assert sum(message == ambient for message in candidate.request().messages) == 1
    assert candidate.chain_id != original_chain
    adapter = GeminiProvider(
        base_url="https://wire.invalid", api_key="test", timeout_seconds=1, max_retries=0
    )
    try:
        sequence = candidate.request()
        wire = adapter._build_payload(
            replace(
                main,
                messages=sequence.messages,
                continuation=sequence.continuation,
                continuation_items=sequence.items,
            )
        )
        assert json.dumps(wire).count(ambient.content) == 1
    finally:
        await adapter.close()
    async with database.sessions() as reader:
        assert dict((await reader.execute(select(effects))).mappings().one()) == receipt
    invoke.assert_awaited_once()
    saved = await control.repository.get(control.current["id"])
    assert saved["model_requests"] == 1 and saved["tool_calls"] == 1
