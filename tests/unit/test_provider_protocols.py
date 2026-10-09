"""Wire-level parity and private checkpoint recovery, without paid calls."""

import json
from dataclasses import replace

import httpx
import pytest

# P10: explicit backend/Invocation fixture; original behavioral assertions retained.
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
    ProviderContinuation,
    ReasoningEffort,
)
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.base import (
    LLMInvalidRequestError,
    LLMInvalidResponseError,
)
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.runtime.work_journal import decode_transcript, encode_transcript
from qq_ai_bot.services.turn_transcript import TurnTranscript


def request():
    return ChatRequest(
        messages=(ChatMessage("system", "fixed"), ChatMessage("user", "task")),
        model="thinking-model",
        thinking_enabled=True,
        reasoning_effort=ReasoningEffort.LOW,
        max_output_tokens=8192,
        temperature=0.7,
        tools=(ChatTool("inspect", "Read evidence", {"type": "object", "properties": {}}),),
        tool_choice="auto",
    )


def provider(kind, client, **kwargs):
    return kind(
        base_url="https://wire.invalid/v1",
        api_key="synthetic-key",
        timeout_seconds=1,
        max_retries=0,
        client=client,
        **kwargs,
    )


@pytest.mark.parametrize(
    "kind", [OpenAICompatibleProvider, AnthropicMessagesProvider, GeminiProvider]
)
async def test_signed_tool_result_and_redirect_survive_journal(kind):
    wires = []

    def transport(req):
        wires.append(json.loads(req.content))
        if kind is AnthropicMessagesProvider:
            body = {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "thinking", "thinking": "private", "signature": "signed"},
                    {"type": "tool_use", "id": "call-1", "name": "inspect", "input": {}},
                ],
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 2,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 2,
                        "ephemeral_1h_input_tokens": 0,
                    },
                    "output_tokens": 4,
                },
            }
        elif kind is GeminiProvider:
            body = {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {"name": "inspect", "args": {}},
                                    "thoughtSignature": "signed",
                                }
                            ],
                        },
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 15,
                    "candidatesTokenCount": 4,
                    "thoughtsTokenCount": 2,
                    "totalTokenCount": 21,
                },
            }
        else:
            body = {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_details": [
                                {"type": "reasoning.encrypted", "data": "signed"}
                            ],
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "inspect",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        adapter = provider(kind, client)
        original = request()
        answer = await adapter.complete(original)
        assert answer.continuation is not None
        transcript = TurnTranscript(original.messages)
        transcript.accept(answer.continuation)
        transcript.append_result(answer.tool_calls[0].id, '{"ok": true}')
        transcript.append(ChatMessage("user", "redirect after receipt"))
        encoded = json.loads(json.dumps(encode_transcript(transcript)))
        restored = decode_transcript(encoded).request()
        assert restored == transcript.request()
        await adapter.complete(
            replace(
                original,
                messages=restored.messages,
                continuation=restored.continuation,
                continuation_items=restored.items,
            )
        )
        sequence_key = "contents" if kind is GeminiProvider else "messages"
        old_prefix = wires[0][sequence_key]
        replay_prefix = wires[1][sequence_key][: len(old_prefix)]
        if kind is AnthropicMessagesProvider:
            # A moving cache breakpoint changes metadata, not the replayed
            # content or signed thinking/tool blocks.
            def without_cache_control(value):
                if isinstance(value, list):
                    return [without_cache_control(item) for item in value]
                if isinstance(value, dict):
                    return {
                        key: without_cache_control(item)
                        for key, item in value.items()
                        if key != "cache_control"
                    }
                return value

            assert without_cache_control(replay_prefix) == without_cache_control(old_prefix)
        else:
            assert replay_prefix == old_prefix
        tail_text = json.dumps(wires[1][sequence_key], ensure_ascii=False)
        assert tail_text.index('"signed"') < tail_text.index("ok") < tail_text.index("redirect")
        assert "_call_ids" not in tail_text
        assert wires[1]["tools"] == wires[0]["tools"]
        if kind is AnthropicMessagesProvider:
            assert answer.prompt_tokens == 15 and answer.total_tokens == 19
            assert answer.cached_prompt_tokens == 3
            assert answer.cache_creation_input_tokens == 2
            assert answer.cache_creation_5m_input_tokens == 2
            assert answer.cache_creation_1h_input_tokens == 0
        elif kind is GeminiProvider:
            assert answer.completion_tokens == 6 and answer.reasoning_tokens == 2


@pytest.mark.parametrize(
    "kind", [OpenAICompatibleProvider, AnthropicMessagesProvider, GeminiProvider]
)
async def test_image_and_json_schema_reach_wire(kind):
    async with httpx.AsyncClient() as client:
        adapter = provider(kind, client)
        original = replace(
            request(),
            tools=(),
            messages=(
                ChatMessage(
                    "user",
                    "image",
                    images=(ChatImage("data:image/png;base64,aW1hZ2U="),),
                ),
            ),
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "answer",
                    "strict": True,
                    "schema": {"type": "object"},
                },
            },
        )
        payload = adapter._build_payload(original)
        assert "aW1hZ2U=" in json.dumps(payload)
        assert "schema" in json.dumps(payload).lower()
        with pytest.raises(LLMInvalidRequestError):
            adapter._build_payload(
                replace(original, messages=(replace(original.messages[0], role="assistant"),))
            )


@pytest.mark.parametrize("reason", ["length", "stop"])
async def test_chat_reasoning_citations_and_usage(reason):
    def transport(req):
        return httpx.Response(
            200,
            json={
                "id": "req-1",
                "choices": [
                    {
                        "finish_reason": reason,
                        "message": {
                            "content": [{"type": "text", "text": "answer"}],
                            "reasoning_content": "private",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {
                                        "url": "https://example.com/source",
                                        "title": "Source",
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "prompt_tokens_details": {"cached_tokens": 4},
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        result = await provider(OpenAICompatibleProvider, client).complete(request())
        assert result.content == "answer" and result.reasoning_content == "private"
        assert result.citations[0].url == "https://example.com/source"
        assert result.cached_prompt_tokens == 4 and result.reasoning_tokens == 3
        assert result.status is (
            ModelResponseStatus.INCOMPLETE if reason == "length" else ModelResponseStatus.COMPLETED
        )


async def test_mistral_thinking_chunks_are_private_and_replay_losslessly():
    async with httpx.AsyncClient() as client:
        adapter = provider(OpenAICompatibleProvider, client, provider_name="mistral")
        original = request()
        assert (
            adapter._build_payload(original)["reasoning_effort"] == original.reasoning_effort.value
        )
        chunks = [
            {"type": "thinking", "thinking": [{"type": "text", "text": "private"}]},
            {"type": "text", "text": "answer"},
        ]
        answer = adapter._parse(
            httpx.Response(
                200, json={"choices": [{"finish_reason": "stop", "message": {"content": chunks}}]}
            ),
            original,
        )
        assert answer.content == "answer" and answer.reasoning_content == "private"
        payload = adapter._build_payload(replace(original, continuation=answer.continuation))
        assert payload["messages"][-1]["content"] == chunks


async def test_deepseek_chat_dsml_remains_unexecuted_text():
    markup = (
        '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="inspect">'
        '<｜｜DSML｜｜parameter name="query" string="true">evidence</｜｜DSML｜｜parameter>'
        "</｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>"
    )
    async with httpx.AsyncClient() as client:
        adapter = provider(OpenAICompatibleProvider, client, provider_name="deepseek")
        response = httpx.Response(
            200,
            json={
                "id": "stable-response",
                "choices": [{"finish_reason": "stop", "message": {"content": markup}}],
            },
        )
        result = adapter._parse(response, request())
        assert result.content == markup and result.tool_calls == ()
        assert adapter._parse(response, replace(request(), tools=())).content == markup


async def test_gemini_parallel_receipts_keep_signature_and_call_order():
    async with httpx.AsyncClient() as client:
        adapter = provider(GeminiProvider, client)
        original = request()
        parts = [
            {
                "functionCall": {"name": "inspect", "args": {"index": index}},
                **({"thoughtSignature": "signed"} if index == 1 else {}),
            }
            for index in (1, 2)
        ]
        answer = adapter._parse(
            httpx.Response(
                200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": parts}}]}
            ),
            original,
        )
        transcript = TurnTranscript(original.messages)
        transcript.accept(answer.continuation)
        for call in answer.tool_calls:
            transcript.append_result(call.id, call.function.arguments)
        sequence = transcript.request()
        payload = adapter._build_payload(
            replace(original, continuation=sequence.continuation, continuation_items=sequence.items)
        )
        assert payload["contents"][-2]["parts"] == parts
        receipts = payload["contents"][-1]["parts"]
        assert [
            json.loads(part["functionResponse"]["response"]["output"])["index"] for part in receipts
        ] == [1, 2]
        assert all("_call_ids" not in item for item in payload["contents"])


async def test_gemini_38_flash_native_wire_and_usage_without_paid_call():
    async with httpx.AsyncClient() as client:
        adapter = provider(GeminiProvider, client)
        original = replace(
            request(), model="gemini-3.8-flash", reasoning_effort=ReasoningEffort.MEDIUM
        )
        payload = adapter._build_payload(original)
        assert adapter._path(original) == "models/gemini-3.8-flash:generateContent"
        assert adapter._request_headers()["x-goog-api-key"] == "synthetic-key"
        assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "medium"}
        assert "thinkingBudget" not in payload["generationConfig"]["thinkingConfig"]
        assert "temperature" not in payload["generationConfig"]
        assert payload["tools"][0]["functionDeclarations"][0]["name"] == "inspect"
        answer = adapter._parse(
            httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "inspect",
                                            "args": {},
                                            "id": "call-38",
                                        },
                                        "thoughtSignature": "opaque-38",
                                    }
                                ],
                            },
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 200,
                        "cachedContentTokenCount": 120,
                        "candidatesTokenCount": 10,
                        "thoughtsTokenCount": 5,
                        "totalTokenCount": 215,
                    },
                },
            ),
            original,
        )
        assert answer.tool_calls[0].id == "call-38"
        assert answer.cached_prompt_tokens == 120
        assert answer.total_tokens == 215
        transcript = TurnTranscript(original.messages)
        transcript.accept(answer.continuation)
        transcript.append_result("call-38", "{}")
        sequence = transcript.request()
        replay = adapter._build_payload(
            replace(original, continuation=sequence.continuation, continuation_items=sequence.items)
        )
        assert replay["contents"][-2]["parts"][0]["thoughtSignature"] == "opaque-38"
        assert replay["contents"][-1]["parts"][0]["functionResponse"]["id"] == "call-38"


async def test_claude_conversation_cache_moves_after_tool_receipt_without_touching_checkpoint():
    async with httpx.AsyncClient() as client:
        adapter = provider(AnthropicMessagesProvider, client)
        original = request()
        first = adapter._build_payload(original)
        checkpoint = ProviderContinuation(
            provider="anthropic",
            protocol="anthropic_messages",
            payload=(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "private", "signature": "signed"},
                        {"type": "tool_use", "id": "call-1", "name": "inspect", "input": {}},
                    ],
                },
            ),
        )
        later = adapter._build_payload(
            replace(
                original,
                continuation=checkpoint,
                continuation_items=(FunctionCallOutput("call-1", "result"),),
            )
        )
        assert first["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in later["messages"][0]["content"][0]
        assert later["messages"][1]["content"] == list(checkpoint.payload[0]["content"])
        assert later["messages"][-1]["content"] == [
            {
                "type": "tool_result",
                "tool_use_id": "call-1",
                "content": "result",
                "cache_control": {"type": "ephemeral"},
            }
        ]
        assert "cache_control" not in later


@pytest.mark.parametrize(
    ("usage_extra", "expected"),
    [
        ({}, None),
        (
            {
                "cache_creation_input_tokens": 0,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 0,
                },
            },
            0,
        ),
    ],
)
async def test_claude_cache_creation_usage_keeps_missing_distinct_from_zero(usage_extra, expected):
    async with httpx.AsyncClient() as client:
        adapter = provider(AnthropicMessagesProvider, client)
        body = {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "done"}],
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 0,
                "output_tokens": 2,
                **usage_extra,
            },
        }
        answer = adapter._parse(httpx.Response(200, json=body), request())
        expected_input = 10 if expected == 0 else None
        expected_total = 12 if expected == 0 else None
        assert answer.prompt_tokens == expected_input
        assert answer.total_tokens == expected_total
        assert adapter._usage_diagnostics(body)["usage"]["prompt_tokens"] == expected_input
        assert adapter._usage_diagnostics(body)["usage"]["total_tokens"] == expected_total
        assert answer.cache_creation_input_tokens is expected
        assert adapter._usage_diagnostics(body)["usage"]["cache_creation_input_tokens"] is expected
        assert answer.cache_creation_5m_input_tokens is expected
        assert answer.cache_creation_1h_input_tokens is expected
        assert (
            adapter._usage_diagnostics(body)["usage"]["cache_creation_5m_input_tokens"] is expected
        )


async def test_gemini_native_search_preserves_server_tool_context_across_function_receipt():
    wires = []

    def transport(req):
        wires.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "toolCall": {
                                        "toolType": "GOOGLE_SEARCH_WEB",
                                        "id": "search-1",
                                        "args": {"queries": ["today's weather"]},
                                    },
                                    "thoughtSignature": "signed-search",
                                },
                                {
                                    "toolResponse": {
                                        "toolType": "GOOGLE_SEARCH_WEB",
                                        "id": "search-1",
                                        "response": {"search_suggestions": "rain"},
                                    },
                                    "thoughtSignature": "signed-result",
                                },
                                {
                                    "functionCall": {
                                        "name": "inspect",
                                        "id": "function-1",
                                        "args": {},
                                    },
                                    "thoughtSignature": "signed-function",
                                },
                            ],
                        },
                    }
                ],
                "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 5},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        adapter = provider(GeminiProvider, client)
        original = replace(
            request(), native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),)
        )
        answer = await adapter.complete(original)
        assert answer.native_tool_events[0].call_id == "search-1"
        assert answer.native_tool_events[0].query == "today's weather"
        assert answer.tool_calls[0].id == "function-1"
        assert answer.continuation is not None
        await adapter.complete(
            replace(
                original,
                continuation=answer.continuation,
                continuation_items=(FunctionCallOutput("function-1", '{"ok": true}'),),
            )
        )
        replay = json.dumps(wires[1]["contents"], ensure_ascii=False)
        assert replay.index("signed-search") < replay.index("signed-result")
        assert replay.index("signed-result") < replay.index("signed-function")
        assert replay.index("signed-function") < replay.index("functionResponse")


async def test_claude_native_search_pause_requires_explicit_continuation():
    wires = []
    first_blocks = [
        {
            "type": "server_tool_use",
            "id": "srvtoolu_1",
            "name": "web_search",
            "input": {"query": "recent launch"},
        }
    ]

    def transport(req):
        wires.append(json.loads(req.content))
        if len(wires) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "msg-first",
                    "stop_reason": "pause_turn",
                    "content": first_blocks,
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 3,
                        "cache_creation_input_tokens": 2,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 2,
                            "ephemeral_1h_input_tokens": 0,
                        },
                        "output_tokens": 1,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "msg-second",
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "web_search_tool_result",
                        "tool_use_id": "srvtoolu_1",
                        "content": [
                            {
                                "type": "web_search_result",
                                "url": "https://example.org/launch",
                                "title": "Launch",
                                "encrypted_content": "opaque-result",
                            }
                        ],
                    },
                    {
                        "type": "text",
                        "text": "Launched today.",
                        "citations": [
                            {
                                "type": "web_search_result_location",
                                "url": "https://example.org/launch",
                                "title": "Launch",
                                "encrypted_index": "opaque-index",
                            }
                        ],
                    },
                ],
                "usage": {
                    "input_tokens": 2,
                    "cache_read_input_tokens": 5,
                    "cache_creation_input_tokens": 0,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": 0,
                    },
                    "output_tokens": 4,
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        from qq_ai_bot.model_runtime.request_accounting import (
            ProviderAttemptCounter,
            current_provider_attempts,
        )

        adapter = provider(AnthropicMessagesProvider, client)
        original = replace(
            request(), native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),)
        )
        attempts = ProviderAttemptCounter()
        token = current_provider_attempts.set(attempts)
        try:
            paused = await adapter.complete(original)
            assert paused.status is ModelResponseStatus.INCOMPLETE
            assert paused.incomplete_reason == "pause_turn"
            assert attempts.requests == len(wires) == 1
            assert paused.prompt_tokens == 15 and paused.completion_tokens == 1
            answer = await adapter.complete(replace(original, continuation=paused.continuation))
        finally:
            current_provider_attempts.reset(token)
        assert len(wires) == 2
        assert attempts.requests == 2
        assert attempts.unknown_usage_requests == 0
        assert wires[0]["tools"][-1] == {
            "type": "web_search_20250305",
            "name": "web_search",
            "cache_control": {"type": "ephemeral"},
        }
        assert wires[1]["messages"][-1] == {"role": "assistant", "content": first_blocks}
        assert answer.content == "Launched today."
        assert answer.prompt_tokens == 7
        assert answer.cached_prompt_tokens == 5
        assert answer.cache_creation_input_tokens == 0
        assert answer.cache_creation_5m_input_tokens == 0
        assert answer.cache_creation_1h_input_tokens == 0
        assert answer.completion_tokens == 4
        assert answer.total_tokens == 11
        assert answer.native_tool_events[0].call_id == "srvtoolu_1"
        assert answer.native_tool_events[0].query == "recent launch"
        assert answer.citations[0].url == "https://example.org/launch"
        assert answer.continuation is not None
        assert (
            answer.continuation.payload[-1]["content"][0]["content"][0]["encrypted_content"]
            == "opaque-result"
        )


async def test_claude_native_search_error_is_not_reported_as_completed():
    async with httpx.AsyncClient() as client:
        adapter = provider(AnthropicMessagesProvider, client)
        configured = replace(
            request(), native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),)
        )
        answer = adapter._parse(
            httpx.Response(
                200,
                json={
                    "stop_reason": "end_turn",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_2",
                            "name": "web_search",
                            "input": {"query": "news"},
                        },
                        {
                            "type": "web_search_tool_result",
                            "tool_use_id": "srvtoolu_2",
                            "content": {
                                "type": "web_search_tool_result_error",
                                "error_code": "unavailable",
                            },
                        },
                        {"type": "text", "text": "Search failed."},
                    ],
                },
            ),
            configured,
        )
        assert answer.native_tool_events[0].status is NativeToolStatus.FAILED
        assert answer.native_tool_events[0].error_category == "unavailable"


async def test_rejected_provider_response_keeps_only_numeric_usage_diagnostics():
    async with httpx.AsyncClient() as client:
        gemini = provider(GeminiProvider, client)
        with pytest.raises(LLMInvalidResponseError) as gemini_error:
            gemini._parse(
                httpx.Response(
                    200,
                    json={
                        "candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}],
                        "usageMetadata": {
                            "promptTokenCount": 12,
                            "candidatesTokenCount": 3,
                            "thoughtsTokenCount": 2,
                            "totalTokenCount": 17,
                            "cachedContentTokenCount": 4,
                        },
                        "secret": "private response body",
                    },
                ),
                request(),
            )
        assert gemini_error.value.diagnostics == {
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 5,
                "total_tokens": 17,
                "cached_prompt_tokens": 4,
            }
        }
        claude = provider(AnthropicMessagesProvider, client)
        with pytest.raises(LLMInvalidResponseError) as claude_error:
            claude._parse(
                httpx.Response(
                    200,
                    json={
                        "stop_reason": "refusal",
                        "content": [{"type": "text", "text": "private response body"}],
                        "usage": {
                            "input_tokens": 7,
                            "cache_read_input_tokens": 3,
                            "cache_creation_input_tokens": 1,
                            "cache_creation": {
                                "ephemeral_5m_input_tokens": 0,
                                "ephemeral_1h_input_tokens": 1,
                            },
                            "output_tokens": 2,
                        },
                    },
                ),
                request(),
            )
        assert claude_error.value.diagnostics == {
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 2,
                "total_tokens": 13,
                "cached_prompt_tokens": 3,
                "cache_creation_input_tokens": 1,
                "cache_creation_5m_input_tokens": 0,
                "cache_creation_1h_input_tokens": 1,
            }
        }
