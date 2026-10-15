"""Native source metadata stays paired with its real server call and opaque tail."""

import json
from dataclasses import replace

import httpx
import pytest
from tests.unit.test_provider_protocols import provider, request

from qq_ai_bot.domain.messages import (
    ChatMessage,
    CitationOrigin,
    ModelResponseStatus,
    NativeToolDefinition,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    ProviderContinuation,
    ResponseCitation,
)
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.base import LLMInvalidResponseError
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.openai_responses import (
    OpenAICompatibleResponsesProvider,
    OpenAIResponsesProvider,
)
from qq_ai_bot.web.native_sources import recover_native_web_response


def native_request():
    return replace(
        request(), tools=(), native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),)
    )


@pytest.mark.parametrize("kind", [OpenAIResponsesProvider, OpenAICompatibleResponsesProvider])
async def test_responses_sources_include_and_original_tail_survive_replay(kind):
    wire = []
    output = [
        {"type": "reasoning", "id": "reason-1", "encrypted_content": "original-secret"},
        {
            "type": "web_search_call",
            "id": "search-1",
            "status": "completed",
            "action": {
                "type": "search",
                "query": "verified query",
                "sources": [{"type": "url", "url": "https://source.test/page", "title": "Real"}],
            },
        },
        {
            "type": "message",
            "id": "msg-1",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": "Uncited https://invented.test/ is not a source.",
                    "annotations": [
                        {"type": "url_citation", "url": "https://cited.test/page", "title": "Cited"}
                    ],
                }
            ],
        },
    ]

    def transport(req):
        wire.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "id": "resp-1",
                "status": "completed",
                "output": output,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 12,
                    "total_tokens": 112,
                    "input_tokens_details": {"cached_tokens": 80},
                    "output_tokens_details": {"reasoning_tokens": 3},
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        adapter = provider(kind, client)
        original = native_request()
        answer = await adapter.complete(original)
        assert wire[0]["include"] == [
            "reasoning.encrypted_content",
            "web_search_call.action.sources",
        ]
        assert wire[0]["store"] is False
        assert [(c.url, c.origin, c.call_id) for c in answer.citations] == [
            ("https://source.test/page", CitationOrigin.NATIVE_RESULT, "search-1"),
            ("https://cited.test/page", CitationOrigin.ANNOTATION, None),
        ]
        assert answer.native_tool_events[0].status is NativeToolStatus.COMPLETED
        assert (answer.prompt_tokens, answer.completion_tokens, answer.total_tokens) == (
            100,
            12,
            112,
        )
        assert answer.cached_prompt_tokens == 80 and answer.reasoning_tokens == 3
        assert answer.continuation is not None
        assert answer.continuation.payload == tuple(output)
        await adapter.complete(
            replace(
                original,
                continuation=answer.continuation,
                continuation_items=(ChatMessage("user", "continue original chain"),),
            )
        )
        assert wire[1]["input"][len(wire[0]["input"]) : -1] == output
        assert wire[1]["input"][-1]["content"] == "continue original chain"


@pytest.mark.parametrize("status", ["failed", "unknown-provider-status"])
async def test_responses_sources_keep_failed_or_unknown_call_state(status):
    async with httpx.AsyncClient() as client:
        adapter = provider(OpenAIResponsesProvider, client)
        answer = adapter._parse(
            httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "web_search_call",
                            "id": "search-1",
                            "status": status,
                            "action": {
                                "type": "search",
                                "sources": [{"url": "https://source.test/page"}],
                            },
                        }
                    ],
                },
            ),
            native_request(),
        )
        assert answer.citations[0].call_id == "search-1"
        assert answer.native_tool_events[0].status is (
            NativeToolStatus.FAILED if status == "failed" else NativeToolStatus.IN_PROGRESS
        )


async def test_deepseek_payload_keeps_existing_omissions():
    async with httpx.AsyncClient() as client:
        payload = provider(DeepSeekResponsesProvider, client)._build_payload(request())
        assert "include" not in payload and "tool_choice" not in payload
        assert all(tool["type"] == "function" for tool in payload["tools"])


async def test_messages_result_sources_need_real_call_and_preserve_opaque():
    calls = {
        "type": "server_tool_use",
        "id": "search-1",
        "name": "web_search",
        "input": {"query": "q"},
    }
    result = {
        "type": "web_search_tool_result",
        "tool_use_id": "search-1",
        "content": [
            {
                "type": "web_search_result",
                "url": "https://source.test/page",
                "title": "Real",
                "content": "Provider supplied source excerpt",
                "encrypted_content": "original-result",
            }
        ],
    }
    text = {
        "type": "text",
        "text": "Uncited https://invented.test/ is not a source.",
        "citations": [
            {
                "type": "web_search_result_location",
                "url": "https://cited.test/page",
                "title": "Cited",
                "cited_text": "Original citation excerpt",
                "encrypted_index": "original-index",
            }
        ],
    }
    blocks = [calls, result, text]
    async with httpx.AsyncClient() as client:
        adapter = provider(AnthropicMessagesProvider, client)
        answer = adapter._parse(
            httpx.Response(
                200,
                json={
                    "id": "msg-1",
                    "content": blocks,
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 5,
                        "cache_read_input_tokens": 7,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 3,
                    },
                },
            ),
            native_request(),
        )
        assert [(c.url, c.origin, c.call_id, c.snippet) for c in answer.citations] == [
            (
                "https://source.test/page",
                CitationOrigin.NATIVE_RESULT,
                "search-1",
                "Provider supplied source excerpt",
            ),
            (
                "https://cited.test/page",
                CitationOrigin.ANNOTATION,
                None,
                "Original citation excerpt",
            ),
        ]
        assert answer.native_tool_events[0].status is NativeToolStatus.COMPLETED
        assert answer.prompt_tokens == 12 and answer.cached_prompt_tokens == 7
        assert answer.completion_tokens == 3 and answer.total_tokens == 15
        assert answer.continuation is not None
        assert answer.continuation.payload[-1]["content"] == blocks
        assert answer.status is ModelResponseStatus.COMPLETED
        # Another server tool's ID in an opaque previous tail is not a search call.
        previous = ProviderContinuation(
            provider="anthropic",
            protocol="anthropic_messages",
            payload=({"role": "assistant", "content": [{**calls, "name": "code_execution"}]},),
        )
        with pytest.raises(LLMInvalidResponseError, match="matching call"):
            adapter._parse(
                httpx.Response(200, json={"content": [result], "stop_reason": "end_turn"}),
                replace(native_request(), continuation=previous),
            )


@pytest.mark.parametrize(
    "content, status",
    [
        (
            {"type": "web_search_tool_result_error", "error_code": "unavailable"},
            NativeToolStatus.FAILED,
        ),
        (None, NativeToolStatus.IN_PROGRESS),
    ],
)
async def test_messages_missing_or_failed_results_never_become_completed(content, status):
    async with httpx.AsyncClient() as client:
        answer = provider(AnthropicMessagesProvider, client)._parse(
            httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "search-1",
                            "name": "web_search",
                            "input": {"query": "q"},
                        },
                        {
                            "type": "web_search_tool_result",
                            "tool_use_id": "search-1",
                            "content": content,
                        },
                    ],
                    "stop_reason": "end_turn",
                },
            ),
            native_request(),
        )
        assert answer.native_tool_events[0].status is status
        assert answer.citations == ()


@pytest.mark.parametrize("status", list(NativeToolStatus))
@pytest.mark.parametrize("matched", [False, True])
def test_inline_recovery_keeps_only_completed_paired_native_results(status, matched):
    response = recover_native_web_response(
        events=(NativeToolEvent(NativeToolType.WEB_SEARCH, "search-1", status),),
        citations=(
            ResponseCitation(
                "https://native.test/source",
                title="Real native source",
                origin=CitationOrigin.NATIVE_RESULT,
                call_id="search-1" if matched else "other-call",
            ),
        ),
        answer_text="Body URL https://invented.test/ must not become a source",
    )
    accepted = matched and status is NativeToolStatus.COMPLETED
    assert [source.url for source in response.sources] == (
        ["https://native.test/source"] if accepted else []
    )
    assert response.partial_failure is not accepted


def test_inline_recovery_preserves_annotations_and_completed_open_page():
    response = recover_native_web_response(
        events=(
            NativeToolEvent(
                NativeToolType.WEB_SEARCH,
                "open-1",
                NativeToolStatus.COMPLETED,
                action_type="open_page",
                url="https://opened.test/source",
            ),
        ),
        citations=(ResponseCitation("https://cited.test/source"),),
        answer_text="Body URL https://invented.test/ must not become a source",
    )
    assert [source.url for source in response.sources] == [
        "https://cited.test/source",
        "https://opened.test/source",
    ]
