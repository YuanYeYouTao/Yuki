"""Native search has one explicit model owner and only genuine source receipts."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from yuki.domain.messages import (
    ChatResponse,
    CitationOrigin,
    ModelResponseStatus,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    ResponseCitation,
)
from yuki.llm.base import LLMTimeoutError
from yuki.llm.fake import FakeLLMProvider
from yuki.llm.gemini import GeminiProvider
from yuki.model_runtime.models import ModelProfile, ModelProtocol
from yuki.web.base import WebSearchError
from yuki.web.bridge_state import BridgeState
from yuki.web.models import WebSearchRequest, WebSearchResponse, WebSearchSource
from yuki.web.native_bridge import NativeSearchBridge
from yuki.web.page_extractor import DirectPageExtractor


def _profile(**updates):
    return ModelProfile(
        id="selected-search",
        provider="fake",
        protocol=ModelProtocol.GEMINI,
        model="selected-model",
        base_url="https://native.invalid/",
        timeout_seconds=17,
        max_retries=0,
        default_temperature=0.25,
        default_max_output_tokens=12345,
        thinking_enabled=False,
        reasoning_effort=None,
    ).model_copy(update=updates)


def _event(call_id="actual-search", status=NativeToolStatus.COMPLETED):
    return NativeToolEvent(NativeToolType.WEB_SEARCH, call_id, status)


def _response(**updates):
    return replace(
        ChatResponse(
            content="Untrusted provider synthesis https://invented.example/answer",
            latency_seconds=0.4,
            provider_request_id="actual-response",
            citations=(ResponseCitation("https://example.com/original"),),
            native_tool_events=(_event(),),
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            cached_prompt_tokens=70,
        ),
        **updates,
    )


def _extractor():
    return SimpleNamespace(extract=AsyncMock(), search=AsyncMock(), close=AsyncMock())


def _bridge(tmp_path, response=None, **options):
    provider = FakeLLMProvider(lambda _: response or _response())
    bridge = NativeSearchBridge(
        profile=options.pop("profile", _profile()),
        credential=options.pop("credential", "synthetic"),
        provider=provider,
        state=options.pop("state", BridgeState(tmp_path / "cache.sqlite3")),
        extractor=options.pop("extractor", _extractor()),
        **options,
    )
    return bridge, provider


async def test_selected_profile_and_user_constraints_reach_only_search_request(tmp_path):
    invocations = SimpleNamespace(record=AsyncMock())
    bridge, provider = _bridge(tmp_path, invocations=invocations)
    request = WebSearchRequest("  actual   query ", max_results=19)
    try:
        result = await bridge.search(request)
        sent = provider.requests[0]
        assert sent.model == "selected-model"
        assert sent.max_output_tokens == 12345 and sent.temperature == 0.25
        assert sent.thinking_enabled is False and sent.reasoning_effort is None
        assert sent.tools == () and sent.continuation is None
        assert len(sent.messages) == 1 and sent.messages[0].role == "user"
        constraints = json.loads(sent.messages[0].content.split("\n", 1)[1])
        assert constraints["query"] == "actual query" and constraints["max_results"] == 19
        assert sent.native_tools[0].type is NativeToolType.WEB_SEARCH
        assert [source.url for source in result.sources] == ["https://example.com/original"]
        assert result.sources[0].relevant_content == ""
        assert result.provider_summary.startswith("Untrusted provider synthesis")
        recorded = invocations.record.call_args.kwargs
        assert recorded["profile_id"] == bridge.profile.id and recorded["native_search_requested"]
        assert recorded["prompt_tokens"] == 100 and recorded["cached_prompt_tokens"] == 70
    finally:
        await bridge.close()


@pytest.mark.parametrize(
    "legacy_provider", ["gemini_native_bridge", "deepseek_anthropic", "tavily"]
)
async def test_legacy_search_or_fallback_cache_never_becomes_native_evidence(
    tmp_path, legacy_provider
):
    legacy = WebSearchResponse(
        "same query",
        (WebSearchSource("old", "old", "https://old.example/", "old.example", "", ""),),
        None,
        0,
        provider=legacy_provider,
    )
    bridge, provider = _bridge(tmp_path, state=SimpleNamespace(access=Mock(return_value=legacy)))
    try:
        result = await bridge.search(WebSearchRequest("same query"))
        assert len(provider.requests) == 1
        assert result.sources[0].url == "https://example.com/original"
    finally:
        await bridge.close()


def test_output_limit_is_not_silently_clamped(tmp_path):
    with pytest.raises(ValueError, match="output limit"):
        _bridge(tmp_path, profile=_profile(max_output_tokens_limit=100))


async def test_only_matching_native_receipts_and_annotations_supply_sources(tmp_path):
    original = "original source body " * 1000
    citations = (
        ResponseCitation("https://example.com/annotation"),
        ResponseCitation(
            "https://example.com/native",
            origin=CitationOrigin.NATIVE_RESULT,
            call_id="actual-search",
            snippet=original,
        ),
        ResponseCitation("https://invented.example/body", origin=CitationOrigin.ANSWER_TEXT),
        ResponseCitation("https://example.com/opened", origin=CitationOrigin.OPEN_PAGE_ACTION),
        ResponseCitation(
            "https://example.com/unknown", origin=CitationOrigin.NATIVE_RESULT, call_id="unknown"
        ),
        ResponseCitation(
            "https://example.com/failed",
            origin=CitationOrigin.NATIVE_RESULT,
            call_id="failed-search",
        ),
        ResponseCitation("https://example.com/unbound", origin=CitationOrigin.NATIVE_RESULT),
        ResponseCitation("http://127.0.0.1/private"),
    )
    bridge, _ = _bridge(
        tmp_path,
        _response(
            citations=citations,
            native_tool_events=(_event(), _event("failed-search", NativeToolStatus.FAILED)),
        ),
    )
    try:
        result = await bridge.search(WebSearchRequest("actual query", max_results=1))
        assert [source.url for source in result.sources] == [
            "https://example.com/annotation",
            "https://example.com/native",
            "https://example.com/opened",
        ]
        assert result.sources[1].relevant_content == original
        assert result.partial_failure
    finally:
        await bridge.close()


@pytest.mark.parametrize("failure", ["body_only", "transport"])
async def test_failure_never_searches_extraction_backend_or_caches_failure(tmp_path, failure):
    extractor = _extractor()
    bridge, provider = _bridge(tmp_path, _response(citations=()), extractor=extractor)
    if failure == "transport":

        def fail(_):
            raise LLMTimeoutError("provider may already have charged this request")

        provider._responder = fail
    try:
        for _ in range(2):
            with pytest.raises(WebSearchError):
                await bridge.search(WebSearchRequest("same query"))
        assert len(provider.requests) == 2
        extractor.search.assert_not_called()
        extractor.extract.assert_not_called()
    finally:
        await bridge.close()


@pytest.mark.parametrize("change", ["protocol", "credential", "output", "headers", "thinking"])
async def test_cache_isolates_actual_connection_and_request_profile(tmp_path, change):
    state = BridgeState(tmp_path / "shared.sqlite3")
    first, first_provider = _bridge(tmp_path, state=state)
    changes = {
        "protocol": {"protocol": ModelProtocol.ANTHROPIC_MESSAGES},
        "output": {"default_max_output_tokens": 19000},
        "headers": {"headers": {"x-route": "second"}},
        "thinking": {"thinking_enabled": True},
    }
    second, second_provider = _bridge(
        tmp_path,
        state=state,
        profile=_profile(**changes.get(change, {})),
        credential="other-credential" if change == "credential" else "synthetic",
    )
    try:
        query = WebSearchRequest("same query")
        assert await first.search(query) == await first.search(query)
        await second.search(query)
        assert len(first_provider.requests) == 1 and len(second_provider.requests) == 1
    finally:
        await first.close()
        await second.close()


async def test_partial_search_does_not_pin_later_complete_receipt(tmp_path):
    bridge, provider = _bridge(tmp_path, _response(status=ModelResponseStatus.INCOMPLETE))
    try:
        query = WebSearchRequest("same query")
        assert (await bridge.search(query)).partial_failure
        provider._responder = lambda _: _response()
        assert not (await bridge.search(query)).partial_failure
        await bridge.search(query)
        assert len(provider.requests) == 2
    finally:
        await bridge.close()


@pytest.mark.parametrize("status", [NativeToolStatus.IN_PROGRESS, NativeToolStatus.SEARCHING])
async def test_unfinished_native_search_keeps_confirmed_sources_without_caching(tmp_path, status):
    bridge, provider = _bridge(
        tmp_path,
        _response(
            citations=(
                ResponseCitation(
                    "https://example.com/confirmed",
                    origin=CitationOrigin.NATIVE_RESULT,
                    call_id="actual-search",
                ),
                ResponseCitation(
                    "https://example.com/unfinished",
                    origin=CitationOrigin.NATIVE_RESULT,
                    call_id="unfinished-search",
                ),
            ),
            native_tool_events=(_event(), _event("unfinished-search", status)),
        ),
    )
    try:
        query = WebSearchRequest("same query")
        for _ in range(2):
            result = await bridge.search(query)
            assert result.partial_failure
            assert [source.url for source in result.sources] == ["https://example.com/confirmed"]
        assert len(provider.requests) == 2
    finally:
        await bridge.close()


async def test_search_returns_native_evidence_without_fetching_and_read_is_explicit(tmp_path):
    extractor = _extractor()
    extractor.extract.return_value = WebSearchSource(
        "page",
        "Extracted page",
        "https://other.example/page",
        "other.example",
        "page snippet",
        "page body",
    )
    bridge, _ = _bridge(tmp_path, extractor=extractor)
    try:
        result = await bridge.search(WebSearchRequest("query"))
        assert result.sources[0].url == "https://example.com/original"
        assert result.sources[0].relevant_content == ""
        extractor.extract.assert_not_awaited()
        await bridge.extract("https://example.com/read#fragment", "read query")
        assert extractor.extract.call_args.args == ("https://example.com/read", "read query")
        extractor.search.assert_not_called()
    finally:
        await bridge.close()


async def test_direct_extraction_preserves_webpage_reading_without_model(tmp_path):
    media = SimpleNamespace(
        resolve=AsyncMock(
            return_value=SimpleNamespace(
                content_type="text/html; charset=utf-8",
                content=b"<h1>Original page</h1><script>secret()</script><p>Complete body</p>",
            )
        ),
        close=AsyncMock(),
    )
    extractor = DirectPageExtractor(
        state=BridgeState(tmp_path / "pages.sqlite3"), timeout_seconds=17, media=media
    )
    try:
        first = await extractor.extract("https://example.com/page#anchor", "query")
        second = await extractor.extract("https://example.com/page", "query")
        assert first == second
        assert first.relevant_content == "Original page\nComplete body"
        assert first.provider == "direct_http"
        assert media.resolve.await_count == 1
        with pytest.raises(WebSearchError):
            await extractor.extract("http://127.0.0.1/private", "query")
        assert media.resolve.await_count == 1
    finally:
        await extractor.close()
    media.close.assert_awaited_once()


async def test_close_attempts_extractor_even_when_provider_close_is_cancelled(tmp_path):
    extractor = _extractor()
    bridge, provider = _bridge(tmp_path, extractor=extractor)
    provider.close = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await bridge.close()
    extractor.close.assert_awaited_once()


async def test_cancelled_model_request_records_one_unknown_paid_attempt(tmp_path):
    import httpx

    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def transport(_request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    invocations = SimpleNamespace(record=AsyncMock())
    async with httpx.AsyncClient(
        base_url="https://native.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        bridge = NativeSearchBridge(
            profile=_profile(),
            credential="synthetic",
            state=BridgeState(tmp_path / "cache.sqlite3"),
            extractor=_extractor(),
            invocations=invocations,
            provider=GeminiProvider(
                base_url="https://native.invalid/",
                api_key="synthetic",
                timeout_seconds=17,
                max_retries=0,
                client=client,
            ),
        )
        task = asyncio.create_task(bridge.search(WebSearchRequest("query")))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cleaned.is_set()
            recorded = invocations.record.call_args.kwargs
            assert not recorded["success"] and recorded["error_category"] == "CancelledError"
            assert (
                recorded["physical_request_count"] == recorded["unknown_usage_request_count"] == 1
            )
            assert recorded["prompt_tokens"] is None and recorded["cached_prompt_tokens"] is None
        finally:
            await asyncio.gather(task, return_exceptions=True)
            await bridge.close()
