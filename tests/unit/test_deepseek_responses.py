"""DeepSeek Responses request, response, and continuation contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from qq_ai_bot.domain.messages import (
    ChatImage,
    ChatMessage,
    ChatRequest,
    ChatTool,
    FunctionCallOutput,
    ModelResponseStatus,
    NativeToolDefinition,
    NativeToolStatus,
    NativeToolType,
    ReasoningEffort,
)
from qq_ai_bot.llm.base import (
    LLMAuthenticationError,
    LLMInvalidRequestError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import ModelCapability, ModelProfile, ModelRoute, ModelTask
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "deepseek_responses"


def _fixture(name: str) -> dict[str, object]:
    payload = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _request(**overrides: object) -> ChatRequest:
    values: dict[str, object] = {
        "messages": (
            ChatMessage(role="system", content="trusted system"),
            ChatMessage(role="developer", content="trusted developer"),
            ChatMessage(role="user", content="hello"),
        ),
        "model": "deepseek-v4-flash",
        "max_output_tokens": 100,
    }
    values.update(overrides)
    return ChatRequest(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_request_mapping_is_responses_native_and_flat() -> None:
    image = ChatImage(data_url="data:image/png;base64,aW1hZ2U=")
    instructions, parts = DeepSeekResponsesProvider._convert_messages(
        (
            ChatMessage(role="system", content="fixed"),
            ChatMessage(role="user", content="picture", images=(image,)),
        )
    )
    assert instructions == "fixed"
    assert parts[0]["content"][1] == {"type": "input_image", "image_url": image.data_url}
    with pytest.raises(LLMInvalidRequestError):
        DeepSeekResponsesProvider._convert_messages((ChatMessage(role="system", images=(image,)),))
    with pytest.raises(LLMInvalidRequestError):
        DeepSeekResponsesProvider._convert_messages(
            (ChatMessage(role="assistant", images=(image,)),)
        )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/responses"
        payload = json.loads(request.content)
        assert payload["instructions"] == "trusted system\n\ntrusted developer"
        assert payload["input"] == [{"role": "user", "content": "hello"}]
        assert payload["tools"] == [
            {
                "type": "function",
                "name": "memory_change",
                "description": "change memory",
                "parameters": {"type": "object"},
            },
            {"type": "web_search"},
        ]
        assert "tool_choice" not in payload
        assert "conversation_prefix_hash" not in payload
        assert "request_shape_hash" not in payload
        assert "prompt_snapshot_fingerprint" not in payload
        assert "static_prompt_revision" not in payload
        assert payload["reasoning"] == {"effort": "max"}
        assert payload["stream"] is False
        return httpx.Response(200, request=request, json=_fixture("text_completed.json"))

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        response = await provider.complete(
            _request(
                thinking_enabled=True,
                reasoning_effort=ReasoningEffort.MAX,
                tools=(
                    ChatTool(
                        name="memory_change",
                        description="change memory",
                        parameters={"type": "object"},
                    ),
                ),
                native_tools=(NativeToolDefinition(type=NativeToolType.WEB_SEARCH),),
                tool_choice="required",
                conversation_prefix_hash="prefix-diagnostic",
                request_shape_hash="shape-diagnostic",
                prompt_snapshot_fingerprint="snapshot-diagnostic",
                static_prompt_revision="static-diagnostic",
            )
        )

    assert response.content == "这是脱敏后的测试回答。"
    assert response.status is ModelResponseStatus.COMPLETED
    assert response.prompt_tokens == 12
    assert response.completion_tokens == 8
    assert response.cached_prompt_tokens == 3
    assert response.reasoning_tokens == 2
    assert response.continuation is not None

    from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider

    def chat_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["messages"][0]["content"][1] == {
            "type": "image_url",
            "image_url": {"url": image.data_url},
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": "seen"}}]})

    async with httpx.AsyncClient(
        base_url="https://example.com", transport=httpx.MockTransport(chat_handler)
    ) as client:
        compatible = OpenAICompatibleProvider(
            base_url="https://example.com",
            api_key="test",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        seen = await compatible.complete(
            _request(messages=(ChatMessage(role="user", content="image", images=(image,)),))
        )
        assert seen.content == "seen"


@pytest.mark.asyncio
async def test_non_thinking_request_omits_unsupported_tool_choice() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "tool_choice" not in payload
        assert "temperature" not in payload
        assert "reasoning" not in payload
        return httpx.Response(200, request=request, json=_fixture("text_completed.json"))

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        await provider.complete(
            _request(
                thinking_enabled=False,
                tools=(
                    ChatTool(
                        name="required_test_tool",
                        description="submit response",
                        parameters={"type": "object"},
                    ),
                ),
                tool_choice="required",
            )
        )


@pytest.mark.parametrize(
    ("thinking_enabled", "reasoning_effort", "expected_reasoning"),
    [
        (True, ReasoningEffort.NONE, {"effort": "none"}),
        (True, ReasoningEffort.MINIMAL, {"effort": "minimal"}),
        (True, ReasoningEffort.LOW, {"effort": "low"}),
        (True, ReasoningEffort.MEDIUM, {"effort": "medium"}),
        (True, ReasoningEffort.HIGH, {"effort": "high"}),
        (True, ReasoningEffort.XHIGH, {"effort": "xhigh"}),
        (True, ReasoningEffort.MAX, {"effort": "max"}),
        (False, None, None),
        (None, None, None),
    ],
)
@pytest.mark.asyncio
async def test_responses_reasoning_payload_matches_thinking_preference(
    thinking_enabled: bool | None,
    reasoning_effort: ReasoningEffort | None,
    expected_reasoning: dict[str, str] | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "temperature" not in payload
        if expected_reasoning is None:
            assert "reasoning" not in payload
        else:
            assert payload["reasoning"] == expected_reasoning
        return httpx.Response(200, request=request, json=_fixture("text_completed.json"))

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        await provider.complete(
            _request(
                temperature=0.7,
                thinking_enabled=thinking_enabled,
                reasoning_effort=reasoning_effort,
            )
        )
        # The wire adapter retains its raw protocol contract; application calls
        # must enforce low before reaching either protocol adapter.
        expected_effort = (
            ReasoningEffort.LOW
            if reasoning_effort in {None, ReasoningEffort.NONE, ReasoningEffort.MINIMAL}
            else reasoning_effort
        )
        expected_reasoning = {"effort": expected_effort.value}
        profile = ModelProfile(
            id="test",
            provider="fake",
            model="test",
            timeout_seconds=1,
            max_retries=0,
            default_temperature=0.7,
            default_max_output_tokens=1000,
            thinking_enabled=False,
            reasoning_effort=None,
            capabilities=frozenset(ModelCapability),
        )
        assert profile.thinking_enabled is True
        assert profile.reasoning_effort is ReasoningEffort.LOW
        stale = profile.model_copy(
            update={
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com",
                "api_key_env": "TEST_KEY",
            }
        )
        stale_catalog = ModelProfileCatalog(
            profiles={"test": stale},
            routes={task: ModelRoute(task=task, profile_id="test") for task in ModelTask},
        )
        stale_executor = TaskModelExecutor(
            router=ModelRouter(stale_catalog),
            pool=ModelClientPool(injected_profiles={"test": provider}),
        )
        assert ModelCapability.NATIVE_WEB_SEARCH not in stale_executor.capabilities(
            ModelTask.CHAT_AGENT
        )
        assert ModelCapability.IMAGE_INPUT in stale_executor.capabilities(ModelTask.CHAT_AGENT)
        catalog = ModelProfileCatalog(
            profiles={"test": profile},
            routes={task: ModelRoute(task=task, profile_id="test") for task in ModelTask},
        )
        executor = TaskModelExecutor(
            router=ModelRouter(catalog),
            pool=ModelClientPool(injected_profiles={"test": provider}),
        )
        await executor.execute(
            ModelTask.MEMORY_SELF_REFLECTION,
            _request(
                thinking_enabled=thinking_enabled,
                reasoning_effort=reasoning_effort,
            ),
        )
        # A per-request low setting must not lower a higher configured profile.
        higher = profile.model_copy(update={"reasoning_effort": ReasoningEffort.HIGH})
        executor = TaskModelExecutor(
            router=ModelRouter(catalog.model_copy(update={"profiles": {"test": higher}})),
            pool=ModelClientPool(injected_profiles={"test": provider}),
        )
        expected_reasoning = {"effort": "high"}
        await executor.execute(
            ModelTask.MEMORY_DREAM, _request(reasoning_effort=ReasoningEffort.LOW)
        )


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "gpt-5.6-luna"])
@pytest.mark.asyncio
async def test_responses_omit_temperature_for_provider_defaults(model: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == model
        assert "temperature" not in payload
        assert payload["reasoning"] == {"effort": "high"}
        return httpx.Response(200, request=request, json=_fixture("text_completed.json"))

    async with httpx.AsyncClient(
        base_url="https://opencode.ai/zen/go/v1", transport=httpx.MockTransport(handler)
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://opencode.ai/zen/go/v1",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        await provider.complete(
            _request(
                model=model,
                temperature=0.7,
                thinking_enabled=True,
                reasoning_effort=ReasoningEffort.HIGH,
            )
        )


@pytest.mark.asyncio
async def test_function_output_follows_cumulative_continuation(caplog) -> None:
    from qq_ai_bot.llm.wire_diagnostics import WireRequestObserver, wire_hash
    from qq_ai_bot.services.turn_transcript import TurnTranscript

    caplog.set_level("INFO", logger="qq_ai_bot.llm.wire_diagnostics")

    requests: list[dict[str, object]] = []
    declared = (ChatTool(name="lookup", description="fixed", parameters={"type": "object"}),)
    transcript = TurnTranscript(_request().messages)

    def current_request():
        sequence = transcript.request()
        return _request(
            messages=sequence.messages,
            request_chain_id=transcript.chain_id,
            tools=declared,
            continuation=sequence.continuation,
            continuation_items=sequence.items,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        fixture = (
            _fixture("function_calls.json")
            if len(requests) == 1
            else _fixture("text_completed.json")
        )
        return httpx.Response(200, request=request, json=fixture)

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        first = await provider.complete(current_request())
        assert [call.id for call in first.tool_calls] == ["call_fixture_1", "call_fixture_2"]
        assert first.continuation is not None
        transcript.accept(first.continuation)
        transcript.append_result("call_fixture_1", '{"ok":true}')
        transcript.append(ChatMessage(role="system", content="control between results"))
        transcript.append_result("call_fixture_2", '{"ok":true}')
        second = await provider.complete(current_request())
        transcript.accept(second.continuation)
        await provider.complete(current_request())

    second_inputs = requests[1]["input"]
    assert isinstance(second_inputs, list)
    assert second_inputs[0] == {"role": "user", "content": "hello"}
    assert [item["type"] for item in second_inputs[1:]] == [
        "function_call",
        "function_call",
        "function_call_output",
        "message",
        "function_call_output",
    ]
    assert second_inputs[-3]["call_id"] == "call_fixture_1"

    assert second_inputs[-2]["content"] == "control between results"
    assert second_inputs[-1]["call_id"] == "call_fixture_2"
    assert requests[2]["input"][: len(second_inputs)] == second_inputs
    assert requests[0]["tools"] == requests[1]["tools"] == requests[2]["tools"]
    assert requests[0]["tools"]
    with pytest.raises(LLMInvalidRequestError, match="conflicting results"):
        provider._build_payload(
            _request(
                continuation=second.continuation,
                continuation_items=(
                    FunctionCallOutput(call_id="call_fixture_1", output="different"),
                ),
            )
        )
    with pytest.raises(LLMInvalidRequestError, match="mixed ordered"):
        provider._build_payload(
            _request(
                continuation=second.continuation,
                continuation_items=(ChatMessage(role="system", content="tail"),),
                function_outputs=(
                    FunctionCallOutput(call_id="call_fixture_1", output="different"),
                ),
            )
        )
    assert requests[0]["instructions"] == requests[1]["instructions"] == requests[2]["instructions"]

    observations = [
        json.loads(record.getMessage().split(" ", 1)[1])
        for record in caplog.records
        if record.name == "qq_ai_bot.llm.wire_diagnostics"
    ]
    assert [o["relation"] for o in observations] == ["first_observation", "append", "append"]
    for payload, observation in zip(requests, observations, strict=True):
        assert observation["tools_hash"] == wire_hash(payload["tools"])
        assert observation["instructions_hash"] == wire_hash(payload["instructions"])
        assert observation["input_items"] == len(payload["input"])
        assert observation["changed_fields"] == []
    encoded = json.dumps(observations)
    assert "trusted system" not in encoded
    assert "control between results" not in encoded
    assert "call_fixture_1" not in encoded

    observer = WireRequestObserver()
    observer.observe(requests[1], "responses", chain_id="independent")
    rewritten = {**requests[1], "input": [*requests[1]["input"]]}
    rewritten["input"][1] = {"type": "message", "role": "user", "content": "changed"}
    difference = observer.observe(rewritten, "responses", chain_id="independent")
    assert difference["relation"] == "input_rewritten"
    assert difference["first_difference_index"] == 1
    assert (
        observer.observe(rewritten, "responses", chain_id="other")["relation"]
        == "first_observation"
    )
    without_tools = {**rewritten, "tools": []}
    assert observer.observe(without_tools, "responses", chain_id="independent")[
        "changed_fields"
    ] == ["tools"]


@pytest.mark.asyncio
async def test_function_outputs_remain_paired_across_three_requests() -> None:
    requests: list[dict[str, object]] = []

    def function_call_response(index: int) -> dict[str, object]:
        return {
            "id": f"resp_{index}",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "id": f"fc_{index}",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": f"call_{index}",
                    "name": "demo",
                    "arguments": "{}",
                }
            ],
            "usage": {},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            response = function_call_response(1)
        elif len(requests) == 2:
            response = function_call_response(2)
        else:
            response = _fixture("text_completed.json")
        return httpx.Response(200, request=request, json=response)

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        first = await provider.complete(_request())
        assert first.continuation is not None
        second = await provider.complete(
            _request(
                continuation=first.continuation,
                function_outputs=(FunctionCallOutput(call_id="call_1", output='{"ok":true}'),),
            )
        )
        assert second.continuation is not None
        await provider.complete(
            _request(
                continuation=second.continuation,
                function_outputs=(FunctionCallOutput(call_id="call_2", output='{"ok":true}'),),
            )
        )

    third_inputs = requests[2]["input"]
    assert isinstance(third_inputs, list)
    assert [item["type"] for item in third_inputs[1:]] == [
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
    ]
    assert [item["call_id"] for item in third_inputs[1:]] == [
        "call_1",
        "call_1",
        "call_2",
        "call_2",
    ]


@pytest.mark.asyncio
async def test_textual_dsml_tool_call_is_recovered_without_leaking_markup() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            response: dict[str, object] = {
                "id": "resp_dsml_1",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_dsml_1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "<｜｜DSML｜｜tool_calls>\n"
                                    '<｜｜DSML｜｜invoke name="read_tool_artifact">\n'
                                    '<｜｜DSML｜｜parameter name="query" string="true">'
                                    "麦当劳 套餐</｜｜DSML｜｜parameter>\n"
                                    "</｜｜DSML｜｜invoke>\n"
                                    "</｜｜DSML｜｜tool_calls>"
                                ),
                            }
                        ],
                    }
                ],
                "usage": {},
            }
        else:
            response = _fixture("text_completed.json")
        return httpx.Response(200, request=request, json=response)

    tool = ChatTool(
        name="read_tool_artifact",
        description="read artifact",
        parameters={"type": "object"},
    )
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        first = await provider.complete(_request(tools=(tool,), tool_choice="auto"))
        assert first.content == ""
        assert len(first.tool_calls) == 1
        call = first.tool_calls[0]
        assert call.function.name == "read_tool_artifact"
        assert json.loads(call.function.arguments) == {"query": "麦当劳 套餐"}
        assert first.continuation is not None

        second = await provider.complete(
            _request(
                tools=(tool,),
                tool_choice="auto",
                continuation=first.continuation,
                function_outputs=(FunctionCallOutput(call_id=call.id, output='{"ok":true}'),),
            )
        )

    assert second.content == "这是脱敏后的测试回答。"
    second_inputs = requests[1]["input"]
    assert isinstance(second_inputs, list)
    assert [item["type"] for item in second_inputs[1:]] == [
        "function_call",
        "function_call_output",
    ]
    assert all("DSML" not in json.dumps(item) for item in second_inputs)


@pytest.mark.asyncio
async def test_textual_dsml_call_to_undeclared_tool_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_dsml_unknown",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": (
                            "<｜｜DSML｜｜tool_calls>"
                            '<｜｜DSML｜｜invoke name="unknown_tool">'
                            "</｜｜DSML｜｜invoke>"
                            "</｜｜DSML｜｜tool_calls>"
                        ),
                    }
                ],
                "usage": {},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        with pytest.raises(LLMInvalidResponseError):
            await provider.complete(_request())


@pytest.mark.asyncio
async def test_native_web_events_and_last_message_are_parsed_from_incomplete_fixture() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=_fixture("native_web_incomplete.json"),
        )

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        response = await DeepSeekResponsesProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        ).complete(_request())

    assert response.status is ModelResponseStatus.INCOMPLETE
    assert response.incomplete_reason == "max_output_tokens"
    assert response.content.startswith("最终信息来自公开文档")
    assert len(response.native_tool_events) == 3
    assert response.native_tool_events[1].status is NativeToolStatus.FAILED
    assert not response.tool_calls


@pytest.mark.asyncio
async def test_failed_and_malformed_responses_are_not_normal_answers() -> None:
    fixtures: list[object] = [_fixture("failed.json"), ["not", "an", "object"]]
    expected = [LLMUnavailableError, LLMInvalidResponseError]
    for payload, error in zip(fixtures, expected, strict=True):

        def handler(request: httpx.Request, body: object = payload) -> httpx.Response:
            return httpx.Response(200, request=request, json=body)

        async with httpx.AsyncClient(
            base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
        ) as client:
            provider = DeepSeekResponsesProvider(
                base_url="https://api.deepseek.com",
                api_key="secret",
                timeout_seconds=1,
                max_retries=0,
                client=client,
            )
            with pytest.raises(error):
                await provider.complete(_request())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, LLMInvalidRequestError),
        (401, LLMAuthenticationError),
        (403, LLMAuthenticationError),
        (429, LLMRateLimitError),
    ],
)
async def test_http_errors_remain_distinguishable(status: int, error: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request, json={"error": "sanitized"})

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        with pytest.raises(error):
            await provider.complete(_request())
