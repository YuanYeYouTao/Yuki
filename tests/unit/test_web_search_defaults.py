"""Fresh model search defaults preserve explicit configuration and real backend limits."""

import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from tests.conftest import build_harness, make_settings
from tests.unit.test_gemini_search_bridge import grounded_response

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.application.modules.web import TaskRoutedWebSearchProvider, WebModule
from qq_ai_bot.config import Settings
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelSearchMode,
    ModelTask,
)
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.services.agent_tools import AgentToolService
from qq_ai_bot.services.native_tool_binder import NativeToolBinder
from qq_ai_bot.web.base import WebSearchError
from qq_ai_bot.web.bridge_state import BridgeState
from qq_ai_bot.web.deepseek_bridge import DeepSeekSearchBridge
from qq_ai_bot.web.gemini_bridge import GeminiSearchBridge
from qq_ai_bot.web.models import WebMode, WebSearchRequest, WebSearchResponse, WebSearchSource
from qq_ai_bot.web.route_context import web_model_task


@pytest.fixture(autouse=True)
def no_inherited_web_switches(monkeypatch):
    for key in ("WEB_MODE", "WEB_ENABLED", "TAVILY_API_KEY", "WEB_SEARCH_BACKEND"):
        monkeypatch.delenv(key, raising=False)


def selected_profile(provider="gemini", protocol=ModelProtocol.GEMINI, mode=ModelSearchMode.BRIDGE):
    return ModelProfile(
        id="selected",
        provider=provider,
        protocol=protocol,
        base_url="https://api.deepseek.com" if provider == "deepseek" else "https://example.com",
        api_key_env="SELECTED_KEY",
        model="selected-model",
        search_mode=mode,
        timeout_seconds=30,
        max_retries=0,
        default_temperature=0.7,
        default_max_output_tokens=4096,
        capabilities=frozenset({ModelCapability.REASONING, ModelCapability.TOOLS}),
    )


def catalog_for(profile, *, search_connection=None):
    return ModelProfileCatalog(
        profiles={profile.id: profile},
        routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
        search_connection=search_connection,
    )


def test_fresh_settings_enable_native_without_external_credentials():
    settings = Settings(_env_file=None)
    assert settings.web_enabled and settings.web.mode is WebMode.NATIVE
    assert not settings.tavily_api_key
    assert settings.web_configured


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"web_enabled": False}, WebMode.DISABLED),
        ({"web_enabled": True, "tavily_api_key": "explicit-test-key"}, WebMode.TAVILY),
        ({"web_mode": "disabled"}, WebMode.DISABLED),
        ({"web_enabled": False, "web_mode": "native"}, WebMode.NATIVE),
        ({"web_mode": "tavily", "tavily_api_key": "explicit-test-key"}, WebMode.TAVILY),
    ],
)
def test_explicit_search_choices_preserve_prior_precedence(config, expected):
    assert Settings(_env_file=None, **config).web.mode is expected


@pytest.mark.parametrize("value,expected", [("false", WebMode.DISABLED), ("true", WebMode.TAVILY)])
def test_legacy_dotenv_switch_is_not_replaced_by_new_default(tmp_path, value, expected):
    environment = tmp_path / ".env"
    environment.write_text(f"WEB_ENABLED={value}\nTAVILY_API_KEY=explicit-test-key\n")
    assert Settings(_env_file=environment).web.mode is expected


async def test_native_default_backend_is_unavailable_until_an_explicit_connection_is_saved(
    tmp_path,
):
    lifecycle = LifecycleRegistry()
    settings = Settings(_env_file=None, web_search_bridge_state_path=tmp_path / "cache.db")
    module = WebModule(settings.web, lifecycle=lifecycle)
    provider = module.build().provider
    assert provider is not None
    with pytest.raises(WebSearchError) as error:
        await provider.search(WebSearchRequest("query"))
    assert error.value.code == "search_unavailable"
    assert not (tmp_path / "cache.db").exists()
    profile = selected_profile()
    catalog = catalog_for(profile)
    module.activate(
        module.prepare(
            catalog,
            SimpleNamespace(api_key_for=lambda _: "selected-test-key"),
            require_explicit=True,
        )
    )
    with web_model_task(ModelTask.CHAT_AGENT):
        bridge = provider._active._selected()
        assert isinstance(bridge, GeminiSearchBridge)
        assert bridge.profile == profile
        assert bridge.fallback is None
    await lifecycle.start()
    await lifecycle.close()


async def test_native_gemini_bridge_uses_only_selected_model_grounding_without_tavily(
    tmp_path, monkeypatch
):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        body = grounded_response()
        body["candidates"][0]["content"]["parts"][0]["text"] = (
            "Generated summary https://invented.example/unsupported. " * 150
        )
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(
        base_url="https://example.com", transport=httpx.MockTransport(respond)
    )
    monkeypatch.setattr(
        "qq_ai_bot.application.modules.web.GeminiProvider",
        lambda **kwargs: GeminiProvider(**kwargs, client=client),
    )
    profile = selected_profile()
    lifecycle = LifecycleRegistry()
    settings = Settings(
        _env_file=None,
        web_search_bridge_state_path=tmp_path / "cache.db",
        web_tool_result_max_characters=2400,
    )
    module = WebModule(
        settings.web,
        lifecycle=lifecycle,
        catalog=catalog_for(profile),
        clients=SimpleNamespace(api_key_for=lambda _: "selected-test-key"),
    )
    provider = module.build().provider
    try:
        with web_model_task(ModelTask.CHAT_AGENT):
            response = await provider.search(WebSearchRequest("query"))
            assert response.provider == "gemini_native_bridge"
            assert response.sources and not response.sources[0].relevant_content
            assert response.provider_summary and len(response.provider_summary) == 2400
            assert await provider.search(WebSearchRequest("query")) == response
            result = AgentToolService._web_response_json(response)
            assert result["provider_summary"] == response.provider_summary
            assert "不是网页原文" in result["provider_summary_instruction"]
            assert all("invented.example" not in source["url"] for source in result["sources"])
            with pytest.raises(WebSearchError) as error:
                await provider.extract(response.sources[0].url, "query")
            assert error.value.code == "extract_unavailable"
        assert len(requests) == 1
        assert requests[0]["tools"] == [{"googleSearch": {}}]
        assert (
            requests[0]["generationConfig"]["maxOutputTokens"] == profile.default_max_output_tokens
        )
        assert ModelCapability.NATIVE_WEB_SEARCH not in profile.capabilities
        assert not NativeToolBinder().bind(
            protocol=profile.protocol,
            capabilities=profile.capabilities,
            allowed_capabilities=frozenset({"web_search"}),
            web_mode=WebMode.NATIVE,
            web_was_used=False,
            search_mode=profile.search_mode,
        )
    finally:
        await lifecycle.start()
        await lifecycle.close()
        await client.aclose()


@pytest.mark.parametrize("failure", ["missing_key", "globally_disabled"])
def test_bridge_validation_respects_credentials_and_global_off(tmp_path, failure):
    profile = selected_profile()
    settings = Settings(
        _env_file=None, **({"web_enabled": False} if failure == "globally_disabled" else {})
    )
    module = WebModule(
        settings.web,
        lifecycle=LifecycleRegistry(),
        catalog=catalog_for(profile),
        clients=SimpleNamespace(api_key_for=lambda _: ""),
    )
    if failure == "globally_disabled":
        assert module.build().provider is None
    else:
        with pytest.raises(ValueError, match="no API key"):
            module.build()


async def test_native_mode_deepseek_search_requires_explicit_search_connection(tmp_path):
    profile = selected_profile("deepseek", ModelProtocol.RESPONSES, ModelSearchMode.EXTERNAL)
    settings = Settings(
        _env_file=None,
        web_search_backend="deepseek_anthropic",
        web_search_bridge_state_path=tmp_path / "cache.db",
    )
    lifecycle = LifecycleRegistry()
    clients = SimpleNamespace(api_key_for=lambda _: "selected-test-key")
    module = WebModule(
        settings.web, lifecycle=lifecycle, catalog=catalog_for(profile), clients=clients
    )
    with pytest.raises(ValueError, match="Select a DeepSeek search connection"):
        module.build()
    assert not (tmp_path / "cache.db").exists()
    module = WebModule(
        settings.web,
        lifecycle=lifecycle,
        catalog=catalog_for(profile, search_connection=profile.id),
        clients=clients,
    )
    provider = module.build().provider
    assert isinstance(provider._active, TaskRoutedWebSearchProvider)
    assert isinstance(provider._active._selected(), DeepSeekSearchBridge)
    assert ModelCapability.NATIVE_WEB_SEARCH not in profile.capabilities
    await lifecycle.start()
    await lifecycle.close()


@pytest.mark.parametrize("protocol", [ModelProtocol.RESPONSES, ModelProtocol.ANTHROPIC_MESSAGES])
def test_native_default_still_needs_model_capability_and_request_authority(protocol):
    binder = NativeToolBinder()
    fields = dict(
        protocol=protocol,
        web_mode=WebMode.NATIVE,
        web_was_used=False,
        search_mode=ModelSearchMode.NATIVE,
    )
    capabilities = frozenset({ModelCapability.NATIVE_WEB_SEARCH})
    assert binder.bind(
        **fields, capabilities=capabilities, allowed_capabilities=frozenset({"web_search"})
    )
    assert not binder.bind(**fields, capabilities=capabilities, allowed_capabilities=frozenset())
    assert not binder.bind(
        **fields, capabilities=frozenset(), allowed_capabilities=frozenset({"web_search"})
    )
    assert not binder.bind(
        **{**fields, "search_mode": ModelSearchMode.EXTERNAL},
        capabilities=capabilities,
        allowed_capabilities=frozenset({"web_search"}),
    )


async def test_native_bridge_local_functions_enter_original_tool_service(database):
    settings = make_settings(database.url)
    module = WebModule(settings.web, lifecycle=LifecycleRegistry())
    provider = module.build().provider
    harness = build_harness(database, settings, web_provider=provider)
    tools = harness.processor._chat._tools
    assert tools._web_catalog_enabled()
    assert tools._web_dependencies()[0] is provider
    await module._lifecycle.start()
    await module._lifecycle.close()


def test_provider_summary_cache_keeps_old_entries_readable(tmp_path):
    state = BridgeState(tmp_path / "cache.db")
    response = WebSearchResponse("query", (), None, 0)
    state.access("old", response)
    with sqlite3.connect(state.path) as db:
        payload = json.loads(
            db.execute("SELECT payload FROM cache WHERE key=?", ("old",)).fetchone()[0]
        )
        assert "provider_summary" not in payload
        db.execute("UPDATE cache SET payload=? WHERE key=?", (json.dumps(payload), "old"))
    recovered = state.access("old")
    assert recovered == response and recovered.provider_summary is None


@pytest.mark.parametrize("escaped", [False, True])
async def test_provider_summary_result_budget_keeps_grounding_sources(
    database, monkeypatch, escaped
):
    settings = make_settings(database.url, web_tool_result_max_characters=2400)
    harness = build_harness(database, settings)
    tools = harness.processor._chat._tools
    monkeypatch.setattr(
        tools,
        "_runtime",
        lambda: SimpleNamespace(web=SimpleNamespace(tool_result_max_characters=2400)),
    )
    sources = tuple(
        WebSearchSource(str(index), "Source", f"https://example.com/{index}", "example.com", "", "")
        for index in range(5)
    )
    result = tools._web_response_json(
        WebSearchResponse(
            "query",
            sources,
            None,
            0,
            provider_summary=(
                json.dumps({"notice": '\n"\\中文'}) if escaped else "Untrusted generated summary. "
            )
            * 500,
        )
    )
    rendered = tools._web_result(data=result)
    payload = json.loads(rendered)
    assert len(rendered) <= 2400 and payload["ok"]
    assert payload["data"]["external_untrusted"] and payload["data"]["truncated"]
    assert [source["url"] for source in payload["data"]["sources"]] == [
        source.url for source in sources
    ]
    assert len(payload["data"].get("provider_summary", "")) < 1000


async def test_keyless_bridge_never_returns_or_caches_summary_without_grounding(tmp_path):
    response = grounded_response(with_source=False)
    client = httpx.AsyncClient(
        base_url="https://example.com",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
    )
    provider = GeminiProvider(
        base_url="https://example.com",
        api_key="test",
        timeout_seconds=30,
        max_retries=0,
        client=client,
    )
    state = BridgeState(tmp_path / "cache.db")
    bridge = GeminiSearchBridge(
        profile=selected_profile(), credential="test", provider=provider, state=state
    )
    try:
        with pytest.raises(WebSearchError) as error:
            await bridge.search(WebSearchRequest("query"))
        assert error.value.code == "no_search_evidence"
        with sqlite3.connect(state.path) as db:
            assert db.execute("SELECT count(*) FROM cache").fetchone()[0] == 0
    finally:
        await bridge.close()
        await client.aclose()
