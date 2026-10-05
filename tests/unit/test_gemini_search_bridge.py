"""Gemini search is a separate grounded request behind stable function tools."""

import asyncio
import hashlib
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.fakes import FakeWebSearchProvider

# P10: explicit Invocation fixture contract; existing assertions are retained.
from tests.support.agent_backend import StubAgentBackend

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.application.modules.web import HotWebSearchProvider, WebModule
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import ReasoningEffort
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.model_runtime.executor import LegacyTaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelSearchMode,
    ModelTask,
)
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.services.agent_runner import AgentRunner, AgentRunResult
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.native_tool_binder import NativeToolBinder
from qq_ai_bot.web.base import WebSearchError
from qq_ai_bot.web.bridge_state import BridgeState
from qq_ai_bot.web.gemini_bridge import GeminiSearchBridge
from qq_ai_bot.web.models import WebMode, WebSearchRequest, WebSearchResponse, WebSearchSource
from qq_ai_bot.web.route_context import web_model_task

URL = "https://example.com/source"
SOURCE = WebSearchSource("fallback", "Fallback", URL, "example.com", "", "")
FALLBACK = WebSearchResponse("query", (SOURCE,), None, 0)


def profile(
    mode=ModelSearchMode.BRIDGE,
    *,
    id="gemini-connection",
    max_output_tokens=2048,
    timeout_seconds=20,
):
    return SimpleNamespace(
        id=id,
        provider="gemini",
        protocol=ModelProtocol.GEMINI,
        search_mode=mode,
        model="gemini-3.8-flash",
        base_url="https://example.com/v1beta/",
        default_max_output_tokens=max_output_tokens,
        max_output_tokens_limit=None,
        thinking_enabled=True,
        reasoning_effort=ReasoningEffort.LOW,
        timeout_seconds=timeout_seconds,
        wire_options=None,
        headers={},
    )


def test_bridge_mode_is_per_gemini_connection_and_needs_no_inline_native_declaration():
    fields = {
        "id": "chat",
        "provider": "gemini",
        "protocol": "gemini",
        "base_url": "https://example.com/v1beta",
        "api_key_env": "TEST_KEY",
        "model": "gemini-3.8-flash",
        "timeout_seconds": 20,
        "max_retries": 0,
        "default_temperature": 0.7,
        "default_max_output_tokens": 2048,
        "capabilities": frozenset({ModelCapability.REASONING, ModelCapability.TOOLS}),
        "search_mode": "bridge",
    }
    result = ModelProfile.model_validate(fields)
    assert result.search_mode is ModelSearchMode.BRIDGE
    with pytest.raises(ValueError, match="Gemini protocol"):
        ModelProfile.model_validate(
            {**fields, "provider": "anthropic", "protocol": "anthropic_messages"}
        )


def grounded_response(*, with_source=True):
    return {
        "responseId": "search-request-1",
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "role": "model",
                    "parts": [{"text": "See https://invented.example/unsupported"}],
                },
                "groundingMetadata": {
                    "webSearchQueries": ["public source"],
                    "groundingChunks": (
                        [{"web": {"uri": URL, "title": "Verified source"}}] if with_source else []
                    ),
                },
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 120,
            "candidatesTokenCount": 20,
            "cachedContentTokenCount": 40,
            "totalTokenCount": 140,
        },
    }


@pytest.mark.asyncio
async def test_bridge_rejects_explicit_provider_output_ceiling_before_http(tmp_path):
    selected = ModelProfile(
        id="gemini-capped",
        provider="gemini",
        protocol=ModelProtocol.GEMINI,
        search_mode=ModelSearchMode.BRIDGE,
        base_url="https://example.com/v1beta/",
        api_key_env="TEST_KEY",
        model="gemini-3.8-flash",
        timeout_seconds=240,
        max_retries=0,
        default_temperature=0.7,
        default_max_output_tokens=16384,
        max_output_tokens_limit=8192,
        capabilities=frozenset({ModelCapability.TOOLS, ModelCapability.REASONING}),
    )
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=grounded_response())

    async with httpx.AsyncClient(
        base_url=selected.base_url, transport=httpx.MockTransport(respond)
    ) as client:
        provider = GeminiProvider(
            base_url=selected.base_url,
            api_key="test",
            timeout_seconds=selected.timeout_seconds,
            max_retries=0,
            client=client,
        )
        with pytest.raises(ValueError, match="configured provider output limit"):
            GeminiSearchBridge(
                profile=selected,
                credential="test",
                provider=provider,
                state=BridgeState(tmp_path / "cache.db"),
            )
    assert calls == []


@pytest.mark.asyncio
async def test_bridge_request_is_search_only_and_accepts_only_grounding(tmp_path):
    wires = []
    timeouts = []

    def respond(request):
        wires.append(json.loads(request.content))
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json=grounded_response())

    client = httpx.AsyncClient(
        base_url="https://example.com/v1beta/", transport=httpx.MockTransport(respond)
    )
    gemini = GeminiProvider(
        base_url="https://example.com/v1beta/",
        api_key="secret",
        timeout_seconds=240,
        max_retries=0,
        client=client,
    )
    fallback = FakeWebSearchProvider(
        response=FALLBACK,
        extracted={URL: WebSearchSource("page", "Page", URL, "example.com", "snippet", "body")},
    )
    invocations = SimpleNamespace(record=AsyncMock())
    bridge = GeminiSearchBridge(
        profile=profile(max_output_tokens=16384, timeout_seconds=240),
        credential="secret",
        provider=gemini,
        state=BridgeState(tmp_path / "cache.db"),
        fallback=fallback,
        invocations=invocations,
    )
    control = SimpleNamespace(validate=AsyncMock(), reserve_request=AsyncMock())
    token = current_work_control.set(control)
    try:
        result = await bridge.search(WebSearchRequest("public source", extract_max_results=1))
        repeat = await bridge.search(WebSearchRequest("public source", extract_max_results=1))
    finally:
        current_work_control.reset(token)
        await bridge.close()
        await client.aclose()
    assert result == repeat
    assert result.provider == "gemini_native_bridge"
    assert [source.url for source in result.sources] == [URL]
    assert result.sources[0].relevant_content == "body"
    assert (result.prompt_tokens, result.completion_tokens, result.cached_prompt_tokens) == (
        120,
        20,
        40,
    )
    assert len(wires) == 1
    assert wires[0]["generationConfig"]["maxOutputTokens"] == 16384
    assert timeouts == [{"connect": 240, "read": 240, "write": 240, "pool": 240}]
    assert wires[0]["tools"] == [{"googleSearch": {}}]
    assert "functionDeclarations" not in json.dumps(wires[0])
    assert wires[0]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    control.reserve_request.assert_awaited_once_with(auxiliary=True)
    assert not fallback.search_requests
    assert len(fallback.extract_requests) == 1
    invocations.record.assert_awaited_once()
    assert invocations.record.await_args.kwargs["task"] == "web_search"
    assert invocations.record.await_args.kwargs["cached_prompt_tokens"] == 40
    assert invocations.record.await_args.kwargs["native_search_requested"] is True


@pytest.mark.asyncio
async def test_bridge_cache_reuses_normalized_query_across_whitespace_variants(tmp_path):
    wires = []

    def respond(request):
        wires.append(json.loads(request.content))
        return httpx.Response(200, json=grounded_response())

    client = httpx.AsyncClient(
        base_url="https://example.com/v1beta/", transport=httpx.MockTransport(respond)
    )
    gemini = GeminiProvider(
        base_url="https://example.com/v1beta/",
        api_key="secret",
        timeout_seconds=20,
        max_retries=0,
        client=client,
    )
    bridge = GeminiSearchBridge(
        profile=profile(),
        credential="secret",
        provider=gemini,
        state=BridgeState(tmp_path / "cache.db"),
    )
    try:
        first = await bridge.search(WebSearchRequest("public  \n source"))
        second = await bridge.search(WebSearchRequest("public source"))
    finally:
        await bridge.close()
        await client.aclose()

    assert first == second
    assert first.query == "public source"
    assert len(wires) == 1


@pytest.mark.parametrize("has_usage", [True, False])
async def test_bridge_failed_physical_search_keeps_reported_usage_or_unknown(tmp_path, has_usage):
    body = {"error": {"type": "bad_request"}}
    if has_usage:
        body["usageMetadata"] = {
            "promptTokenCount": 120,
            "candidatesTokenCount": 20,
            "cachedContentTokenCount": 40,
            "totalTokenCount": 140,
        }
    client = httpx.AsyncClient(
        base_url="https://example.com/v1beta/",
        transport=httpx.MockTransport(lambda _: httpx.Response(400, json=body)),
    )
    gemini = GeminiProvider(
        base_url="https://example.com/v1beta/",
        api_key="secret",
        timeout_seconds=20,
        max_retries=0,
        client=client,
    )
    invocations = SimpleNamespace(record=AsyncMock())
    bridge = GeminiSearchBridge(
        profile=profile(),
        credential="secret",
        provider=gemini,
        state=BridgeState(tmp_path / "cache.db"),
        invocations=invocations,
    )
    try:
        with pytest.raises(WebSearchError, match="失败"):
            await bridge.search(WebSearchRequest("query"))
    finally:
        await bridge.close()
        await client.aclose()
    record = invocations.record.await_args.kwargs
    assert record["task"] == "web_search"
    assert record["success"] is False
    assert record["physical_request_count"] == 1
    assert record["unknown_usage_request_count"] == (0 if has_usage else 1)
    assert record["prompt_tokens"] == (120 if has_usage else None)
    assert record["completion_tokens"] == (20 if has_usage else None)
    assert record["total_tokens"] == (140 if has_usage else None)
    assert record["cached_prompt_tokens"] == (40 if has_usage else None)
    assert record["native_search_requested"] is True


@pytest.mark.asyncio
async def test_bridge_fallback_is_explicit_and_not_cached(tmp_path):
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=grounded_response(with_source=calls > 1))

    client = httpx.AsyncClient(
        base_url="https://example.com/v1beta/", transport=httpx.MockTransport(respond)
    )
    gemini = GeminiProvider(
        base_url="https://example.com/v1beta/",
        api_key="secret",
        timeout_seconds=20,
        max_retries=0,
        client=client,
    )
    fallback = FakeWebSearchProvider(response=FALLBACK)
    bridge = GeminiSearchBridge(
        profile=profile(),
        credential="secret",
        provider=gemini,
        state=BridgeState(tmp_path / "cache.db"),
        fallback=fallback,
    )
    try:
        first = await bridge.search(WebSearchRequest("query"))
        second = await bridge.search(WebSearchRequest("query"))
    finally:
        await bridge.close()
        await client.aclose()
    assert first.provider == "tavily"
    assert second.provider == "gemini_native_bridge"
    assert calls == 2
    assert len(fallback.search_requests) == 1
    assert second.sources[0].url == URL


@pytest.mark.asyncio
async def test_bridge_retries_partial_extraction_including_legacy_cached_failure(tmp_path):
    calls = 0

    def respond(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=grounded_response())

    client = httpx.AsyncClient(
        base_url="https://example.com/v1beta/", transport=httpx.MockTransport(respond)
    )
    gemini = GeminiProvider(
        base_url="https://example.com/v1beta/",
        api_key="secret",
        timeout_seconds=20,
        max_retries=0,
        client=client,
    )
    fallback = FakeWebSearchProvider(response=FALLBACK)
    bridge = GeminiSearchBridge(
        profile=profile(),
        credential="secret",
        provider=gemini,
        state=BridgeState(tmp_path / "cache.db"),
        fallback=fallback,
    )
    request = WebSearchRequest("query", extract_max_results=1)
    try:
        first = await bridge.search(request)
        assert first.partial_failure
        # Simulate a partial receipt left by an older Bot before this fix.
        key = hashlib.sha256(
            bridge._namespace + json.dumps(asdict(request), sort_keys=True).encode()
        ).hexdigest()
        assert bridge.state.access(key) is None
        bridge.state.access(key, first)
        fallback.extracted[URL] = WebSearchSource(
            "page", "Page", URL, "example.com", "snippet", "body"
        )
        recovered = await bridge.search(request)
        cached = await bridge.search(request)
    finally:
        await bridge.close()
        await client.aclose()

    assert calls == 2
    assert len(fallback.extract_requests) == 2
    assert not recovered.partial_failure
    assert recovered.sources[0].relevant_content == "body"
    assert cached == recovered


@pytest.mark.asyncio
async def test_bridge_hot_switch_follows_chat_connection_without_native_main_tool(tmp_path):
    settings = Settings(
        _env_file=None,
        web_mode="tavily",
        tavily_api_key="test",
        web_timeout_seconds=25,
        web_search_bridge_state_path=tmp_path / "cache.db",
    )
    selected = profile(max_output_tokens=16384, timeout_seconds=240)
    automation = profile(id="automation-connection")
    catalog = SimpleNamespace(
        profiles={"gemini-connection": selected, "automation-connection": automation},
        routes={
            ModelTask.CHAT_AGENT: SimpleNamespace(profile_id="gemini-connection"),
            ModelTask.AUTOMATION_AGENT: SimpleNamespace(profile_id="automation-connection"),
        },
    )
    lifecycle = LifecycleRegistry()
    module = WebModule(
        settings.web,
        lifecycle=lifecycle,
        catalog=catalog,
        clients=SimpleNamespace(api_key_for=lambda _: "secret"),
    )
    provider = module.build().provider
    with web_model_task(ModelTask.CHAT_AGENT):
        assert isinstance(provider._active._selected(), GeminiSearchBridge)
        assert provider._active._selected().profile.id == "gemini-connection"
        assert provider._active._selected().provider._timeout.read == 240
    with web_model_task(ModelTask.AUTOMATION_AGENT):
        assert provider._active._selected().profile.id == "automation-connection"
    assert (
        NativeToolBinder().bind(
            protocol=ModelProtocol.GEMINI,
            capabilities=frozenset(),
            allowed_capabilities=frozenset({"web_search"}),
            web_mode=WebMode.TAVILY,
            web_was_used=False,
            search_mode=ModelSearchMode.BRIDGE,
        )
        == ()
    )
    assert not NativeToolBinder().excluded_function_names(
        protocol=ModelProtocol.GEMINI,
        capabilities=frozenset(),
        allowed_capabilities=frozenset({"web_search"}),
        web_mode=WebMode.TAVILY,
        search_mode=ModelSearchMode.BRIDGE,
    )
    next_catalog = SimpleNamespace(
        profiles={
            "gemini-connection": profile(ModelSearchMode.EXTERNAL),
            "automation-connection": automation,
        },
        routes=catalog.routes,
    )
    module.activate(
        module.prepare(
            next_catalog,
            SimpleNamespace(api_key_for=lambda _: "secret"),
            require_explicit=True,
        )
    )
    with web_model_task(ModelTask.CHAT_AGENT):
        assert provider._active._selected().__class__.__name__ == "TavilyWebSearchProvider"
    with web_model_task(ModelTask.AUTOMATION_AGENT):
        assert isinstance(provider._active._selected(), GeminiSearchBridge)
    await asyncio.sleep(0)
    await lifecycle.start()
    await lifecycle.close()


@pytest.mark.asyncio
async def test_inflight_runner_keeps_old_search_connection_during_hot_switch():
    old = FakeWebSearchProvider(response=FALLBACK)
    new = FakeWebSearchProvider(
        response=WebSearchResponse("query", (SOURCE,), "new", 0, provider="new")
    )
    hot = HotWebSearchProvider(old)
    with web_model_task(ModelTask.CHAT_AGENT), hot.pin():
        hot.activate(new)
        assert not old.closed
        result = await hot.search(WebSearchRequest("query"))
        assert result.provider == "tavily"
        assert len(old.search_requests) == 1
        assert not new.search_requests
    await asyncio.sleep(0)
    assert old.closed
    result = await hot.search(WebSearchRequest("query"))
    assert result.provider == "new"
    assert len(new.search_requests) == 1
    await hot.close()


@pytest.mark.asyncio
async def test_agent_runner_pins_web_backend_through_tool_execution(monkeypatch):
    old = FakeWebSearchProvider(response=FALLBACK)
    new = FakeWebSearchProvider(
        response=WebSearchResponse("query", (SOURCE,), "new", 0, provider="new")
    )
    hot = HotWebSearchProvider(old)
    runner = AgentRunner(LegacyTaskModelExecutor(FakeLLMProvider()), ConcurrencyManager(2))

    class Backend(StubAgentBackend):
        def pin_web_provider(self):
            return hot.pin()

    async def exercise(messages, runtime, tools):
        del messages, runtime, tools
        hot.activate(new)
        assert not old.closed
        result = await hot.search(WebSearchRequest("query"))
        assert result.provider == "tavily"
        return AgentRunResult(text="", tool_calls_used=0, model_requests=0, web_was_used=True)

    monkeypatch.setattr(runner, "_run_with_receipts", exercise)
    runtime = SimpleNamespace(
        canonical_conversation_id=None,
        execution_id=None,
        source_event_id=None,
        origin=SimpleNamespace(value="user_message"),
    )
    await runner.run((), runtime, Backend())
    await asyncio.sleep(0)
    assert old.closed
    assert len(old.search_requests) == 1
    assert not new.search_requests
    await hot.close()


@pytest.mark.asyncio
async def test_bridge_without_grounding_or_fallback_fails_closed(tmp_path):
    client = httpx.AsyncClient(
        base_url="https://example.com/v1beta/",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=grounded_response(with_source=False))
        ),
    )
    gemini = GeminiProvider(
        base_url="https://example.com/v1beta/",
        api_key="secret",
        timeout_seconds=20,
        max_retries=0,
        client=client,
    )
    bridge = GeminiSearchBridge(
        profile=profile(),
        credential="secret",
        provider=gemini,
        state=BridgeState(tmp_path / "cache.db"),
    )
    try:
        with pytest.raises(WebSearchError, match="可信"):
            await bridge.search(WebSearchRequest("query"))
    finally:
        await bridge.close()
        await client.aclose()
