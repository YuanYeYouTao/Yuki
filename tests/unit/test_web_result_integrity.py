"""Returned search evidence remains complete until the existing result budgeter."""

import json

import httpx
import pytest

from qq_ai_bot.domain.messages import ChatResponse, CitationOrigin, ResponseCitation
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.models import ModelProfile
from qq_ai_bot.web.bridge_state import BridgeState
from qq_ai_bot.web.deepseek_bridge import DeepSeekSearchBridge
from qq_ai_bot.web.gemini_bridge import GeminiSearchBridge
from qq_ai_bot.web.models import WebSearchRequest
from qq_ai_bot.web.tavily import TavilyWebSearchProvider


async def test_tavily_passes_requested_counts_and_preserves_all_returned_evidence():
    urls = [f"https://example.com/page/{i}?q={'x' * 2200}" for i in range(7)]
    title = "title " * 60
    snippet = "snippet " * 200
    content = "article " * 1500 + "original tail"
    captured = []

    def transport(request):
        body = json.loads(request.content)
        captured.append((request.url.path, body))
        if request.url.path == "/search":
            return httpx.Response(
                200,
                json={"results": [dict(url=url, title=title, content=snippet) for url in urls]},
            )
        return httpx.Response(
            200,
            json={"results": [dict(url=url, raw_content=content) for url in body["urls"]]},
        )

    client = httpx.AsyncClient(
        base_url="https://api.tavily.com", transport=httpx.MockTransport(transport)
    )
    provider = TavilyWebSearchProvider(api_key="synthetic", client=client)
    query = "q" * 700
    try:
        result = await provider.search(
            WebSearchRequest(query, max_results=7, extract_max_results=5)
        )
    finally:
        await provider.close()
    assert captured[0] == (
        "/search",
        dict(
            query=query,
            topic="general",
            search_depth="advanced",
            max_results=7,
            include_answer=False,
            include_raw_content=False,
            include_images=False,
        ),
    )
    assert captured[1] == (
        "/extract",
        dict(urls=urls[:5], extract_depth="basic", format="markdown", include_images=False),
    )
    assert [source.url for source in result.sources] == urls
    assert all(
        source.title == title.strip() and source.snippet == snippet.strip()
        for source in result.sources
    )
    assert [source.relevant_content for source in result.sources] == [content] * 5 + [
        snippet.strip(),
        snippet.strip(),
    ]


async def test_deepseek_native_search_keeps_wire_evidence_without_local_response_caps(tmp_path):
    urls = [f"https://example.com/page/{i}" for i in range(7)]
    title = "title" * 70
    content = "native search evidence " * 1000
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "original-response",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": "x" * (2 * 1024 * 1024)},
                    {"type": "server_tool_use", "name": "web_search", "id": "search-1"},
                    {
                        "type": "web_search_tool_result",
                        "tool_use_id": "search-1",
                        "content": [
                            dict(type="web_search_result", url=url, title=title, content=content)
                            for url in urls
                        ],
                    },
                ],
            },
        )

    provider = DeepSeekSearchBridge(
        api_key="synthetic",
        state_path=tmp_path / "search.sqlite3",
        client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )
    try:
        result = await provider.search(
            WebSearchRequest("q" * 700, max_results=7, extract_max_results=0)
        )
    finally:
        await provider.close()
    assert captured[0]["tools"] == [{"type": "web_search_20250305", "name": "web_search"}]
    assert [source.url for source in result.sources] == urls
    assert all(
        source.title == title and source.relevant_content == content for source in result.sources
    )


async def test_gemini_keeps_original_grounding_and_full_summary(tmp_path):
    urls = [f"https://example.com/page/{i}" for i in range(7)]
    summary = "returned synthesis " * 1100 + "original tail"
    title = "title" * 70
    provider = FakeLLMProvider(
        lambda _: ChatResponse(
            content=summary,
            latency_seconds=0,
            citations=tuple(
                ResponseCitation(url=url, title=title, origin=CitationOrigin.ANNOTATION)
                for url in urls
            ),
        )
    )
    bridge = GeminiSearchBridge(
        profile=ModelProfile(
            id="fake-search",
            provider="fake",
            model="fake",
            timeout_seconds=1,
            max_retries=0,
            default_temperature=0.5,
            default_max_output_tokens=32768,
        ),
        credential="synthetic",
        provider=provider,
        state=BridgeState(tmp_path / "search.sqlite3"),
    )
    try:
        result = await bridge.search(WebSearchRequest("q" * 700, max_results=7))
    finally:
        await bridge.close()
    assert [source.url for source in result.sources] == urls
    assert all(source.title == title for source in result.sources)
    assert result.provider_summary == summary
    assert len(provider.requests) == 1


@pytest.mark.parametrize("work_enabled", [True, False])
async def test_main_history_search_archives_original_requested_page(
    database, tmp_path, work_enabled
):
    from tests.support.automation_unified_delivery_helpers import setup_run

    from qq_ai_bot.automation.models import RunStatus
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.domain.messages import ToolCall, ToolFunction
    from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository

    case = await setup_run(database, tmp_path, delivery="none", mode="silent")
    case.settings.runtime_work_enabled = work_enabled
    case.settings.context_window_tokens = 500000
    case.settings.context_compaction_window_tokens = 500000
    case.chat._context_assembler._history_budget = lambda runtime: 500000
    case.chat._context_assembler._history_capacity = lambda runtime: 500000
    store = ToolArtifactRepository(database, tmp_path / "artifacts", retention_seconds=60)
    case.chat._tool_artifacts = store
    originals = []
    for index in range(101):
        content = f"history-needle {index}: " + "原始资料" * 500 + f" tail-{index}"
        await case.env.service.writer.append(
            scope=ConversationScope.group("80001", "20001"),
            platform_message_id=f"history-{index}",
            sender_user_id="10001",
            direction="inbound",
            content=content,
        )
        originals.append(content)

    def respond(request):
        if len(case.provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "original-history-read",
                        ToolFunction(
                            "search_chat_history",
                            json.dumps({"keyword": "history-needle", "limit": 101}),
                        ),
                    ),
                ),
            )
        return "The original page is available from the artifact."

    case.provider._responder = respond
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED, result
    assert len(case.provider.requests) == 2
    reply = next(
        message for message in case.provider.requests[1].messages if message.role == "tool"
    )
    manifest = json.loads(reply.content)
    assert manifest["ok"] and manifest["original_characters"] > 200000
    page = await store.read(manifest["artifact_handle"], limit=1000000, max_characters=1000000)
    original = json.loads(page["content"])
    assert original["data"]["returned_count"] == 101
    assert original["data"]["truncated"] is False
    assert [row["content"] for row in original["data"]["events"]] == originals
    assert not [action for action, _ in case.env.bot.calls if action.startswith("send_")]
