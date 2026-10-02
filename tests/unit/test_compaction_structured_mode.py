"""Auxiliary summaries honor configured modes without adding function tools."""

import json

import httpx
import pytest
from tests.support.work_compaction import summary_json
from tests.unit.test_work_compaction_capacity import (
    _grow,
    _runtime,
    _seed_runner_contract,
    _session,
)

from qq_ai_bot.domain.messages import ChatRequest, ChatResponse
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.model_runtime.models import ModelExecutionPriority, StructuredOutputMode
from qq_ai_bot.services.ordinary_compaction import summarize_records


@pytest.mark.parametrize("mode", list(StructuredOutputMode))
async def test_tool_free_summary_uses_actual_format_on_chat_wire(mode):
    captured = []

    def transport(request):
        body = json.loads(request.content)
        captured.append(body)
        assert not body.get("tools") and not body.get("tool_choice")
        if mode is StructuredOutputMode.JSON_SCHEMA:
            assert body["response_format"]["type"] == "json_schema"
        else:
            assert "response_format" not in body
        source = json.loads(body["messages"][-1]["content"])
        content = json.dumps(
            {
                "facts": [{"text": "saved fact", "refs": source["source_refs"]}],
                "pending": [],
                "next_steps": [],
            }
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://summary.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://summary.invalid",
            api_key="unused",
            timeout_seconds=2,
            max_retries=0,
            client=client,
        )
        result = await summarize_records(
            [("record:0", "real supplied material")],
            main_request=ChatRequest(messages=(), model="configured-main-model"),
            structured_mode=mode,
            summary_budget=20000,
            output_tokens=1024,
            prepare=lambda item: item,
            execute=provider.complete,
        )
    assert len(captured) == 1
    assert captured[0]["model"] == "configured-main-model"
    assert result["facts"][0]["refs"] == ["record:0"]


@pytest.mark.parametrize("mode", list(StructuredOutputMode))
async def test_work_auxiliary_request_uses_main_mode_and_preserves_original_work(
    database, tmp_path, monkeypatch, mode
):
    control, session, initial = await _session(database, tmp_path)
    _grow(session.transcript)
    provider = FakeLLMProvider()
    provider._responder = lambda request: ChatResponse(
        summary_json(request.messages[-1].content), 0
    )
    runner, runtime = await _runtime(
        database, control, initial, provider, contract_workspace=tmp_path
    )
    monkeypatch.setattr(runner._models, "structured_output_mode", lambda task: mode)
    runtime, request = await _seed_runner_contract(runner, runtime, session, initial)
    identity = control.current["id"]
    await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 100000, request)
    assert provider.requests
    assert control.current["id"] == identity
    assert control.current["model_requests"] == len(provider.requests)
    for paid in provider.requests:
        assert paid.model == request.model
        assert paid.tools == paid.native_tools == () and paid.tool_choice is None
        assert (paid.response_format is not None) == (mode is StructuredOutputMode.JSON_SCHEMA)
        assert '"version":1' in paid.messages[0].content
