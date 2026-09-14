"""Reflection wire, budget and report contracts without unrelated full-suite work."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ModelResponseStatus,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.base import LLMInvalidRequestError
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.openai_responses import OpenAIResponsesProvider
from qq_ai_bot.memory.self_reflection.models import SelfReflectionOutput
from qq_ai_bot.model_runtime.models import ModelTask, StructuredOutputMode
from qq_ai_bot.model_runtime.request_accounting import (
    after_provider_request,
    before_provider_request,
)
from qq_ai_bot.model_runtime.structured import StructuredTaskError, StructuredTaskRunner


def test_responses_schema_is_flat_and_tools_do_not_change():
    schema = SelfReflectionOutput.model_json_schema()
    request = ChatRequest(
        model="test",
        messages=(ChatMessage(role="user", content="synthetic"),),
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "emit_result", "strict": True, "schema": schema},
        },
    )
    for kind in (DeepSeekResponsesProvider, OpenAIResponsesProvider):
        provider = kind(
            base_url="https://example.test", api_key="synthetic", timeout_seconds=1, max_retries=0
        )
        payload = provider._build_payload(request)
        assert payload["text"]["format"] == {
            "type": "json_schema",
            "name": "emit_result",
            "strict": True,
            "schema": schema,
        }
        assert not payload.get("tools")
        assert request.response_format["json_schema"]["schema"] == schema


@pytest.mark.asyncio
async def test_structured_incomplete_and_tools_are_never_accepted():
    responses = [
        ChatResponse(
            latency_seconds=0,
            content="{}",
            status=ModelResponseStatus.INCOMPLETE,
            incomplete_reason="max_output_tokens",
        ),
        ChatResponse(latency_seconds=0, content="{}", completion_tokens=32768),
        ChatResponse(
            latency_seconds=0,
            content="{}",
            status=ModelResponseStatus.INCOMPLETE,
            incomplete_reason="other",
        ),
        ChatResponse(latency_seconds=0, content="```json\n{}\n```"),
        ChatResponse(latency_seconds=0, content="[]"),
        ChatResponse(
            latency_seconds=0,
            content="{}",
            tool_calls=(
                ToolCall(id="x", function=ToolFunction(name="emit_result", arguments="{}")),
            ),
        ),
    ]
    for response in responses:
        models = SimpleNamespace(
            model_name=lambda task: "test", execute=AsyncMock(return_value=response)
        )
        runner = StructuredTaskRunner(models)
        with pytest.raises(StructuredTaskError):
            await runner.run(
                task=ModelTask.MEMORY_SELF_REFLECTION,
                instruction="test",
                structured_input={},
                output_model=SelfReflectionOutput,
                mode=StructuredOutputMode.JSON_SCHEMA,
                max_output_tokens=32768,
            )
        assert models.execute.await_count == 1


@pytest.mark.asyncio
async def test_schema_fallback_requires_explicit_provider_rejection_and_opt_in():
    for allow, code, succeeds in (
        (False, "unsupported_json_schema", False),
        (True, "invalid_request", False),
        (True, "unsupported_json_schema", True),
    ):
        models = SimpleNamespace(
            model_name=lambda task: "test",
            execute=AsyncMock(
                side_effect=[
                    LLMInvalidRequestError("rejected", diagnostics={"code": code}),
                    ChatResponse(latency_seconds=0, content='{"proposals":[],"episodes":[]}'),
                ]
            ),
        )
        runner = StructuredTaskRunner(models)
        args = dict(
            task=ModelTask.MEMORY_SELF_REFLECTION,
            instruction="test",
            structured_input={},
            output_model=SelfReflectionOutput,
            mode=StructuredOutputMode.JSON_SCHEMA,
            allow_schema_fallback=allow,
        )
        if succeeds:
            result = await runner.run(**args)
            assert not result.proposals
            request = models.execute.call_args.args[1]
            assert not request.tools and request.response_format is None
        else:
            with pytest.raises(LLMInvalidRequestError):
                await runner.run(**args)
            assert models.execute.await_count == 1


@pytest.mark.asyncio
async def test_each_physical_transport_attempt_is_accounted():
    attempts = 0

    def respond(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": {"code": "busy"}})
        return httpx.Response(
            200,
            json={
                "id": "r",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "{}"}],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        )

    reserve = AsyncMock()
    finish = AsyncMock()
    t = before_provider_request.set(reserve)
    u = after_provider_request.set(finish)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://example.test"
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://example.test",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=1,
            client=client,
        )
        try:
            await provider.complete(
                ChatRequest(model="test", messages=(ChatMessage(role="user", content="test"),))
            )
        finally:
            before_provider_request.reset(t)
            after_provider_request.reset(u)
    assert reserve.await_count == finish.await_count == 2
    assert finish.await_args_list[-1].args == ("completed", 3)
    from qq_ai_bot.memory.self_reflection.reporting import deliver_report

    # A report receipt remains authoritative even if its original event is gone.
    prior = SimpleNamespace(
        status=SimpleNamespace(value="succeeded"),
        model_dump=lambda **kwargs: {"status": "succeeded"},
    )
    social = SimpleNamespace(
        receipts=SimpleNamespace(find=AsyncMock(return_value=prior)), execute=AsyncMock()
    )
    assert await deliver_report(social, {"id": "sr_same"}) == {"status": "succeeded"}
    social.execute.assert_not_awaited()
