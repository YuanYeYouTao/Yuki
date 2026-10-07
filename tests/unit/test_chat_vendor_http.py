"""Configured vendor options, endpoint and cache usage reach actual HTTP unchanged."""

import json
from dataclasses import replace

import httpx
import pytest
from tests.unit.test_provider_protocols import request

from qq_ai_bot.domain.messages import ReasoningEffort
from qq_ai_bot.llm.base import LLMUnsupportedFeatureError
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.llm.vendor_policy import CHAT_VENDORS, ChatWireOptions


@pytest.mark.parametrize("vendor", sorted(CHAT_VENDORS))
async def test_vendor_http_preserves_configured_endpoint_declarations_and_cache(vendor):
    captured = []
    endpoint = "https://vendor.invalid/configured/deployment/"

    def transport(value):
        captured.append(value)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 4,
                    "total_tokens": 24,
                    "prompt_tokens_details": {"cached_tokens": 0 if len(captured) == 1 else 7},
                },
            },
        )

    async with httpx.AsyncClient(
        base_url=endpoint, transport=httpx.MockTransport(transport)
    ) as client:
        adapter = OpenAICompatibleProvider(
            base_url=endpoint,
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
            provider_name=vendor,
            headers={"api-key": "synthetic-configured-header", "x-fixture": "explicit"},
        )
        original = replace(request(), tool_choice="inspect")
        cold = await adapter.complete(original)
        hot = await adapter.complete(original)
    assert [cold.cached_prompt_tokens, hot.cached_prompt_tokens] == [0, 7]
    assert cold.prompt_tokens == hot.prompt_tokens == 20
    assert cold.total_tokens == hot.total_tokens == 24
    assert captured[0].content == captured[1].content
    assert str(captured[0].url) == endpoint + "chat/completions"
    assert captured[0].headers["api-key"] == "synthetic-configured-header"
    assert captured[0].headers["x-fixture"] == "explicit"
    wire = json.loads(captured[0].content)
    assert wire["messages"] == [
        {"role": "system", "content": "fixed"},
        {"role": "user", "content": "task"},
    ]
    assert wire["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "inspect",
                "description": "Read evidence",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assert "temperature" not in wire and wire["stream"] is False
    if vendor == "deepseek":
        assert "tool_choice" not in wire
    else:
        assert wire["tool_choice"] == {"type": "function", "function": {"name": "inspect"}}
    if vendor in {"openai", "azure_openai", "groq"}:
        assert wire["max_completion_tokens"] == 8192 and "max_tokens" not in wire
    else:
        assert wire["max_tokens"] == 8192 and "max_completion_tokens" not in wire
    if vendor in {"deepseek", "moonshot", "zhipu", "doubao"}:
        assert wire["thinking"] == {"type": "enabled"}
        assert wire.get("reasoning_effort") == ("low" if vendor in {"deepseek", "doubao"} else None)
    elif vendor == "qwen":
        assert wire["enable_thinking"] is True and wire["thinking_budget"] == 4096
    elif vendor == "minimax":
        assert wire["reasoning_split"] is True and "reasoning_effort" not in wire
    elif vendor == "openrouter":
        assert wire["reasoning"] == {"effort": "low", "exclude": False}
    else:
        assert wire["reasoning_effort"] == ("high" if vendor == "mistral" else "low")
    if vendor == "groq":
        assert wire["include_reasoning"] is True


@pytest.mark.parametrize(
    "vendor,options,expected",
    [
        (
            "qwen",
            {"thinking_budget_tokens": 1024},
            {"enable_thinking": True, "thinking_budget": 2048},
        ),
        (
            "groq",
            {"include_reasoning": False, "reasoning_format": "hidden"},
            {"reasoning_effort": "medium", "reasoning_format": "hidden"},
        ),
        (
            "siliconflow",
            {"send_temperature": True, "token_field": "max_completion_tokens"},
            {"reasoning_effort": "medium", "temperature": 0.7, "max_completion_tokens": 8192},
        ),
        (
            "openai_compatible",
            {"reasoning": "thinking", "send_reasoning_effort": True},
            {"thinking": {"type": "enabled"}, "reasoning_effort": "medium"},
        ),
    ],
)
async def test_supported_overrides_are_observed_on_http(vendor, options, expected):
    wires = []

    def transport(value):
        wires.append(json.loads(value.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}
        )

    async with httpx.AsyncClient(
        base_url="https://override.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        adapter = OpenAICompatibleProvider(
            base_url="https://override.invalid/",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
            provider_name=vendor,
            options=ChatWireOptions(**options),
        )
        await adapter.complete(replace(request(), reasoning_effort=ReasoningEffort.MEDIUM))
    assert len(wires) == 1
    assert all(wires[0].get(key) == value for key, value in expected.items())
    if vendor == "groq":
        assert "include_reasoning" not in wires[0]  # Explicit format takes precedence.


async def test_effort_above_declared_levels_is_rejected_before_http():
    wires = []

    def transport(value):
        wires.append(value)
        pytest.fail("unsupported effort must be rejected before dispatch")

    async with httpx.AsyncClient(
        base_url="https://refused.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        adapter = OpenAICompatibleProvider(
            base_url="https://refused.invalid/",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
            provider_name="groq",
        )
        with pytest.raises(LLMUnsupportedFeatureError, match="exceeds"):
            await adapter.complete(replace(request(), reasoning_effort=ReasoningEffort.MAX))
    assert wires == []
