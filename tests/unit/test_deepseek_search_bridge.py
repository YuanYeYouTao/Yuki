"""Real search evidence, restart cache, unrestricted fallback and wiring contracts."""

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.fakes import FakeWebSearchProvider

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.application.modules.web import HotWebSearchProvider, WebModule
from qq_ai_bot.config import Settings
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
)
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
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
    timeouts = []

    def respond(request):
        requests.append(json.loads(request.content))
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json=evidence())

    control = SimpleNamespace(validate=AsyncMock(), reserve_request=AsyncMock())
    token = current_work_control.set(control)
    request = WebSearchRequest(
        "today's news",
        topic="news",
        time_range="day",
        start_date=date(2026, 9, 13),
        end_date=date(2026, 9, 13),
        extract_max_results=0,
    )
    # Old fallback cache entries must not bypass a recovered primary backend.
    key = hashlib.sha256(
        json.dumps(asdict(request), sort_keys=True, default=str).encode()
    ).hexdigest()
    BridgeState(tmp_path / "cache.db").access(key, RESPONSE)
    try:
        for _ in range(2):
            bridge = DeepSeekSearchBridge(
                api_key="test",
                state_path=tmp_path / "cache.db",
                timeout_seconds=240,
                max_output_tokens=16384,
                client=httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=240),
            )
            try:
                result = await bridge.search(request)
                assert [s.url for s in result.sources] == [URL]
                assert result.provider == result.sources[0].provider == "deepseek_anthropic"
            finally:
                await bridge.close()
        assert len(requests) == 1
        assert requests[0]["model"] == "deepseek-flash"
        assert requests[0]["max_tokens"] == 16384
        assert timeouts == [{"connect": 240, "read": 240, "write": 240, "pool": 240}]
        assert len(requests[0]["messages"]) == 1
        assert json.loads(requests[0]["messages"][0]["content"])["query"] == request.query
        constraints = json.loads(requests[0]["messages"][0]["content"])
        assert constraints["time_range"] == "day" and constraints["topic"] == "news"
        assert constraints["start_date"] == constraints["end_date"] == "2026-09-13"
        control.reserve_request.assert_awaited_once_with(auxiliary=True)
    finally:
        current_work_control.reset(token)


async def test_bridge_does_not_reuse_search_cache_across_api_keys(tmp_path):
    calls: list[str] = []

    def respond(request):
        calls.append(request.headers["x-api-key"])
        return httpx.Response(200, json=evidence())

    for api_key in ("first", "second"):
        bridge = DeepSeekSearchBridge(
            api_key=api_key,
            state_path=tmp_path / "cache.db",
            client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        try:
            await bridge.search(WebSearchRequest("same query", extract_max_results=0))
        finally:
            await bridge.close()
    assert calls == ["first", "second"]


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
        assert len(fallback.search_requests) == 17
        responses.append(evidence())
        recovered = await bridge.search(WebSearchRequest("unique query 15", extract_max_results=0))
        assert recovered.provider == "deepseek_anthropic"
        assert len(fallback.search_requests) == 17
        responses.append(evidence())
        dated = await bridge.search(
            WebSearchRequest("dated", time_range="week", extract_max_results=0)
        )
        assert dated.provider == "deepseek_anthropic"
        assert len(fallback.search_requests) == 17
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
        timeout_seconds=240,
        max_output_tokens=16384,
        client=httpx.AsyncClient(transport=httpx.MockTransport(timeout), timeout=240),
    )
    try:
        with pytest.raises(WebSearchError, match="DeepSeek 搜索请求失败"):
            await bridge.search(WebSearchRequest("docs"))
        assert len(calls) == 1
        assert json.loads(calls[0].content)["max_tokens"] == 16384
        assert calls[0].extensions["timeout"]["read"] == 240
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
        web_timeout_seconds=25,
    )
    assert settings.web_configured
    for profile in (
        SimpleNamespace(provider="other", base_url="https://example.com"),
        SimpleNamespace(provider="deepseek", base_url="https://proxy.example.com"),
    ):
        catalog = SimpleNamespace(
            search_connection="search",
            profiles={"search": profile},
            routes={ModelTask.CHAT_AGENT: SimpleNamespace(profile_id="search")},
        )
        with pytest.raises(ValueError, match="official DeepSeek"):
            WebModule(
                settings.web,
                lifecycle=LifecycleRegistry(),
                catalog=catalog,
                clients=SimpleNamespace(api_key_for=lambda _profile: "test"),
            ).build()
    profile = SimpleNamespace(
        provider="deepseek",
        base_url="https://api.deepseek.com",
        timeout_seconds=240,
        default_max_output_tokens=16384,
        max_output_tokens_limit=None,
    )
    catalog = SimpleNamespace(
        search_connection="search",
        profiles={"search": profile},
        routes={ModelTask.CHAT_AGENT: SimpleNamespace(profile_id="chat")},
    )
    lifecycle = LifecycleRegistry()
    bundle = WebModule(
        settings.web,
        lifecycle=lifecycle,
        catalog=catalog,
        clients=SimpleNamespace(api_key_for=lambda _profile: "test"),
    ).build()
    assert isinstance(bundle.provider._active, DeepSeekSearchBridge)
    assert bundle.provider._active.fallback is None
    assert bundle.provider._active.client.timeout.read == 240
    assert bundle.provider._active.max_output_tokens == 16384
    await lifecycle.start()
    await lifecycle.close()
    legacy = Settings(_env_file=None, web_mode="tavily", tavily_api_key="test")
    lifecycle = LifecycleRegistry()
    provider = WebModule(legacy.web, lifecycle=lifecycle).build().provider
    assert provider._active.__class__.__name__ == "TavilyWebSearchProvider"
    await lifecycle.start()
    await lifecycle.close()


async def test_bridge_search_connection_survives_chat_switch_and_hot_key_change(tmp_path):
    settings = Settings(
        _env_file=None,
        web_mode="tavily",
        web_search_backend="deepseek_anthropic",
        web_search_bridge_state_path=tmp_path / "cache.db",
    )
    search = SimpleNamespace(
        provider="deepseek",
        base_url="https://api.deepseek.com",
        timeout_seconds=240,
        default_max_output_tokens=16384,
        max_output_tokens_limit=None,
    )
    gemini = SimpleNamespace(
        provider="gemini", base_url="https://generativelanguage.googleapis.com/v1beta"
    )
    catalog = SimpleNamespace(
        search_connection="search",
        profiles={"search": search, "chat": gemini},
        routes={ModelTask.CHAT_AGENT: SimpleNamespace(profile_id="chat")},
    )
    lifecycle = LifecycleRegistry()
    module = WebModule(
        settings.web,
        lifecycle=lifecycle,
        catalog=catalog,
        clients=SimpleNamespace(api_key_for=lambda _profile: "old-key"),
    )
    provider = module.build().provider
    assert provider is not None
    old = provider._active
    assert old.headers["x-api-key"] == "old-key"
    replacement = module.prepare(
        catalog,
        SimpleNamespace(api_key_for=lambda _profile: "new-key"),
        require_explicit=True,
    )
    assert replacement is not None
    module.activate(replacement)
    assert provider._active.headers["x-api-key"] == "new-key"
    assert old.headers["x-api-key"] == "old-key"
    await asyncio.sleep(0)
    assert old.client.is_closed
    missing = SimpleNamespace(
        search_connection=None, profiles=catalog.profiles, routes=catalog.routes
    )
    with pytest.raises(ValueError, match="Select a DeepSeek search connection"):
        module.prepare(
            missing, SimpleNamespace(api_key_for=lambda _: "test"), require_explicit=True
        )
    with pytest.raises(ValueError, match="requires restart before disabling"):
        module.activate(None)
    await lifecycle.start()
    await lifecycle.close()


@pytest.mark.parametrize("entry", ["startup", "hot_prepare"])
async def test_module_rejects_explicit_search_output_ceiling_before_http(
    tmp_path, monkeypatch, entry
):
    selected = ModelProfile(
        id="search-capped",
        provider="deepseek",
        protocol=ModelProtocol.RESPONSES,
        base_url="https://api.deepseek.com",
        api_key_env="TEST_KEY",
        model="deepseek-flash",
        timeout_seconds=240,
        max_retries=0,
        default_temperature=0.7,
        default_max_output_tokens=16384,
        max_output_tokens_limit=8192,
        capabilities=frozenset({ModelCapability.REASONING, ModelCapability.TOOLS}),
    )
    catalog = ModelProfileCatalog(
        profiles={selected.id: selected},
        routes={task: ModelRoute(task=task, profile_id=selected.id) for task in ModelTask},
        search_connection=selected.id,
    )
    settings = Settings(
        _env_file=None,
        web_mode="tavily",
        web_search_backend="deepseek_anthropic",
        web_search_bridge_state_path=tmp_path / "cache.db",
    )
    calls = []

    def create_bridge(**arguments):
        calls.append(arguments)
        pytest.fail("an invalid search budget must not create an HTTP client")

    monkeypatch.setattr("qq_ai_bot.application.modules.web.DeepSeekSearchBridge", create_bridge)
    clients = SimpleNamespace(api_key_for=lambda _: "test")
    module = WebModule(
        settings.web, lifecycle=LifecycleRegistry(), catalog=catalog, clients=clients
    )
    with pytest.raises(ValueError, match="configured provider output limit"):
        if entry == "startup":
            module.build()
        else:
            module.prepare(catalog, clients, require_explicit=True)
    assert calls == []
    assert not (tmp_path / "cache.db").exists()


async def test_hot_web_search_retires_only_after_inflight_calls_finish():
    class GatedProvider:
        def __init__(self, name: str) -> None:
            self.name = name
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = asyncio.Event()
            self.running = 0

        async def search(self, request):
            self.running += 1
            self.started.set()
            try:
                await self.release.wait()
                assert not self.closed.is_set()
                return RESPONSE
            finally:
                self.running -= 1

        async def extract(self, url, query):
            self.running += 1
            self.started.set()
            try:
                await self.release.wait()
                assert not self.closed.is_set()
                return SOURCE
            finally:
                self.running -= 1

        async def close(self):
            assert self.running == 0
            self.closed.set()

    first, second, third = (GatedProvider(name) for name in ("first", "second", "third"))
    hot = HotWebSearchProvider(first)
    first_call = asyncio.create_task(hot.search(WebSearchRequest("first query")))
    await first.started.wait()
    hot.activate(second)
    second_call = asyncio.create_task(hot.extract(URL, "second query"))
    await second.started.wait()
    hot.activate(third)
    assert not first.closed.is_set() and not second.closed.is_set()
    assert len(hot._retired) == 2
    third.release.set()
    assert await hot.search(WebSearchRequest("third query")) == RESPONSE
    second.release.set()
    assert await second_call == SOURCE
    await asyncio.wait_for(second.closed.wait(), 1)
    assert not first.closed.is_set()
    first.release.set()
    assert await first_call == RESPONSE
    await asyncio.wait_for(first.closed.wait(), 1)
    assert not hot._retired
    previous = third
    for index in range(12):
        replacement = GatedProvider(f"replacement-{index}")
        replacement.release.set()
        hot.activate(replacement)
        await asyncio.wait_for(previous.closed.wait(), 1)
        assert not hot._retired
        previous = replacement
    await hot.close()
    assert previous.closed.is_set()
    with pytest.raises(WebSearchError, match="联网搜索已停止"):
        await hot.search(WebSearchRequest("too late"))


async def test_hot_web_search_shutdown_waits_for_active_extract():
    started = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    class Provider:
        async def search(self, request):
            return RESPONSE

        async def extract(self, url, query):
            started.set()
            await release.wait()
            assert not closed.is_set()
            return SOURCE

        async def close(self):
            closed.set()

    hot = HotWebSearchProvider(Provider())
    read = asyncio.create_task(hot.extract(URL, "query"))
    await started.wait()
    stopping = asyncio.create_task(hot.close())
    await asyncio.sleep(0)
    assert not stopping.done() and not closed.is_set()
    release.set()
    assert await read == SOURCE
    await stopping
    assert closed.is_set()


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
