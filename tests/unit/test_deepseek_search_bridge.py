"""Real search evidence, restart cache, unrestricted fallback and wiring contracts."""

import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.fakes import FakeWebSearchProvider

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.application.modules.web import WebModule
from qq_ai_bot.config import Settings
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.web.base import WebSearchError
from qq_ai_bot.web.bridge_state import BridgeState
from qq_ai_bot.web.deepseek_bridge import DeepSeekSearchBridge
from qq_ai_bot.web.models import WebSearchRequest, WebSearchResponse, WebSearchSource

URL = "https://example.com/docs"
SOURCE = WebSearchSource("fallback", "Docs", URL, "example.com", "text", "body")
RESPONSE = WebSearchResponse("query", (SOURCE,), "fallback-id", 0)


def evidence():
    return {
        "id": "message-id",
        "stop_reason": "end_turn",
        "content": [
            {"type": "text", "text": "https://invented.example/fake"},
            {"type": "server_tool_use", "name": "web_search", "id": "server-1"},
            {
                "type": "web_search_tool_result",
                "tool_use_id": "server-1",
                "content": [
                    {"type": "web_search_result", "url": URL, "title": "Docs"},
                    {"type": "web_search_result", "url": "http://127.0.0.1/private"},
                ],
            },
        ],
    }


async def test_bridge_real_evidence_restart_cache_and_request_budget(tmp_path):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=evidence())

    control = SimpleNamespace(validate=AsyncMock(), reserve_request=AsyncMock())
    token = current_work_control.set(control)
    request = WebSearchRequest("official docs", extract_max_results=0)
    try:
        for _ in range(2):
            bridge = DeepSeekSearchBridge(
                api_key="test",
                state_path=tmp_path / "cache.db",
                client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
            )
            try:
                result = await bridge.search(request)
                assert [s.url for s in result.sources] == [URL]
                assert result.provider == result.sources[0].provider == "deepseek_anthropic"
            finally:
                await bridge.close()
        assert len(requests) == 1
        assert requests[0]["model"] == "deepseek-flash"
        assert len(requests[0]["messages"]) == 1
        assert json.loads(requests[0]["messages"][0]["content"])["query"] == request.query
        control.reserve_request.assert_awaited_once_with(auxiliary=True)
    finally:
        current_work_control.reset(token)


async def test_bridge_fallback_has_no_daily_cap_and_rejects_fake_evidence(tmp_path):
    fallback = FakeWebSearchProvider(response=RESPONSE)
    responses = [
        {"content": [{"type": "text", "text": URL}]},
        {
            "content": [
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "unknown",
                    "content": [{"type": "web_search_result", "url": URL}],
                }
            ]
        },
        {
            "content": [
                {"type": "server_tool_use", "name": "web_search"},
                {"type": "web_search_tool_result", "tool_use_id": [], "content": []},
            ]
        },
    ]
    bridge = DeepSeekSearchBridge(
        api_key="test",
        state_path=tmp_path / "cache.db",
        fallback=fallback,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=responses.pop(0) if responses else {})
            )
        ),
    )
    try:
        for index in range(16):
            assert (
                await bridge.search(WebSearchRequest(f"unique query {index}"))
            ).provider == "tavily"
        assert len(fallback.search_requests) == 16
        await bridge.search(WebSearchRequest("unique query 15"))
        assert len(fallback.search_requests) == 16
        await bridge.search(WebSearchRequest("dated", time_range="week"))
        assert fallback.search_requests[-1].time_range == "week"
    finally:
        await bridge.close()
    assert fallback.closed


async def test_bridge_direct_page_shared_ssrf_checks_and_extract_fallback(tmp_path):
    downloaded = []

    def page(request):
        downloaded.append(request)
        if request.url.path == "/docs/blocked":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<style>hidden</style><p>real page evidence</p>",
        )

    page_client = httpx.AsyncClient(transport=httpx.MockTransport(page))
    media = MediaResolver(client=page_client, host_resolver=lambda host, port: ["93.184.216.34"])
    fallback = FakeWebSearchProvider(extracted={URL + "/blocked": SOURCE})
    bridge = DeepSeekSearchBridge(
        api_key="test", state_path=tmp_path / "cache.db", media=media, fallback=fallback
    )
    try:
        result = await bridge.extract(URL, "evidence")
        assert result.relevant_content == "real page evidence"
        assert result.provider == "direct_http"
        assert await bridge.extract(URL, "evidence") == result
        assert len(downloaded) == 1 and not fallback.extract_requests
        assert (await bridge.extract(URL + "/blocked", "evidence")).provider == "tavily"
        assert len(downloaded) == 2  # Private redirect was never requested.
        with pytest.raises(WebSearchError):
            await bridge.extract("http://127.0.0.1/private", "test")
        assert len(fallback.extract_requests) == 1
    finally:
        await bridge.close()
        await page_client.aclose()


async def test_bridge_timeout_no_retry_or_fabricated_success(tmp_path):
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive response or URL")

    bridge = DeepSeekSearchBridge(
        api_key="test",
        state_path=tmp_path / "cache.db",
        client=httpx.AsyncClient(transport=httpx.MockTransport(timeout)),
    )
    try:
        with pytest.raises(WebSearchError, match="DeepSeek 搜索请求失败"):
            await bridge.search(WebSearchRequest("docs"))
        assert len(calls) == 1
    finally:
        await bridge.close()


def test_bridge_cache_expiry_size_and_capacity(tmp_path):
    state = BridgeState(tmp_path / "cache.db")
    for index in range(130):
        state.access(str(index), RESPONSE)
    with sqlite3.connect(state.path) as db:
        assert db.execute("select count(*) from cache").fetchone()[0] == 128
        db.execute("update cache set expires=0 where key='129'")
    assert state.access("129") is None
    state.access(
        "large", replace(RESPONSE, sources=(replace(SOURCE, relevant_content="x" * 40000),))
    )
    assert state.access("large") is None


async def test_bridge_module_opt_in_profile_validation_and_tavily_compatibility(tmp_path):
    settings = Settings(
        _env_file=None,
        web_mode="tavily",
        tavily_api_key="",
        web_search_backend="deepseek_anthropic",
        web_search_bridge_state_path=tmp_path / "cache.db",
    )
    assert settings.web_configured
    for profile in (
        None,
        SimpleNamespace(provider="other", base_url="https://example.com"),
        SimpleNamespace(provider="deepseek", base_url="https://proxy.example.com"),
    ):
        with pytest.raises(ValueError, match="official DeepSeek"):
            WebModule(
                settings.web,
                lifecycle=LifecycleRegistry(),
                search_profile=profile,
                search_api_key="test",
            ).build()
    profile = SimpleNamespace(provider="deepseek", base_url="https://api.deepseek.com")
    lifecycle = LifecycleRegistry()
    bundle = WebModule(
        settings.web, lifecycle=lifecycle, search_profile=profile, search_api_key="test"
    ).build()
    assert isinstance(bundle.provider, DeepSeekSearchBridge)
    assert bundle.provider.fallback is None
    await lifecycle.start()
    await lifecycle.close()
    legacy = Settings(_env_file=None, web_mode="tavily", tavily_api_key="test")
    lifecycle = LifecycleRegistry()
    provider = WebModule(legacy.web, lifecycle=lifecycle).build().provider
    assert provider.__class__.__name__ == "TavilyWebSearchProvider"
    await lifecycle.start()
    await lifecycle.close()


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_bridge_switch_preserves_main_and_worker_wire(database, tmp_path, protocol):
    from tests.conftest import build_harness, make_settings

    from qq_ai_bot.domain.messages import ChatMessage, ChatRequest
    from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
    from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
    from qq_ai_bot.runtime.subagent_tools import WORKER_NAMES
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState
    from qq_ai_bot.workspace.store import WorkspaceStore

    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "response-id",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
                "choices": [
                    {"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}
                ],
            },
        )

    client = httpx.AsyncClient(
        base_url="https://example.com", transport=httpx.MockTransport(respond)
    )
    provider_type = (
        DeepSeekResponsesProvider if protocol == "responses" else OpenAICompatibleProvider
    )
    provider = provider_type(
        base_url="https://example.com",
        api_key="test",
        timeout_seconds=2,
        max_retries=0,
        client=client,
    )
    try:
        for backend in ("tavily", "deepseek_anthropic"):
            settings = make_settings(
                database.url, web_mode="tavily", tavily_api_key="test", web_search_backend=backend
            )
            chat = build_harness(
                database, settings, web_provider=FakeWebSearchProvider(response=RESPONSE)
            ).processor._chat
            contract = MainAgentContract(
                chat,
                SimpleNamespace(_registry=None),
                ShortState(WorkspaceStore(tmp_path / backend)),
            )
            main = await contract.definitions()
            assert {"web_search", "read_webpage"} <= {t.name for t in main}
            for tools in (main, tuple(t for t in main if t.name in WORKER_NAMES)):
                messages = (
                    ChatMessage(role="system", content="fixed contract"),
                    ChatMessage(role="user", content="task"),
                )
                request = ChatRequest(model="deepseek-flash", messages=messages, tools=tools)
                await provider.complete(request)
                await provider.complete(
                    replace(
                        request,
                        messages=(
                            *messages,
                            ChatMessage(role="assistant", content="checked"),
                            ChatMessage(role="user", content="continue"),
                        ),
                    )
                )
        assert captured[:4] == captured[4:]  # Actual JSON, not hashes.
        field = "input" if protocol == "responses" else "messages"
        for before, after in ((captured[0], captured[1]), (captured[2], captured[3])):
            assert after[field][: len(before[field])] == before[field]
            assert before["tools"] == after["tools"]
            assert not any(t["type"] == "web_search" for t in before["tools"])
    finally:
        await client.aclose()
