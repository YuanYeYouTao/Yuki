"""Search routing, credential ownership and saved settings remain independent of Main."""

import asyncio
import tomllib

import pytest
from tests.conftest import make_settings

from qq_ai_bot.admin.config_files import ConfigFileService
from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.application.modules import web as web_module
from qq_ai_bot.application.modules.web import HotWebSearchProvider, WebModule
from qq_ai_bot.domain.messages import ChatResponse, CitationOrigin, ResponseCitation
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelSearchMode,
    ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog, parse_model_profile_catalog
from qq_ai_bot.web.models import WebSearchRequest
from qq_ai_bot.web.native_bridge import NativeSearchBridge
from qq_ai_bot.web.route_context import current_web_model_task
from qq_ai_bot.web.search_config import effective_search_profile


def profile(name, protocol, *, provider="agm", search_mode=None):
    return ModelProfile(
        id=name,
        provider=provider,
        protocol=protocol,
        base_url="https://model.invalid/v1",
        api_key_env=name.upper() + "_KEY",
        model="test-model",
        timeout_seconds=30,
        max_retries=2,
        default_temperature=0.4,
        default_max_output_tokens=2048,
        search_mode=search_mode,
        capabilities=frozenset({ModelCapability.TOOLS}),
    )


def catalog(main, search=None, *, backend=None):
    profiles = {main.id: main}
    if search:
        profiles[search.id] = search
    return ModelProfileCatalog(
        profiles=profiles,
        routes={
            task: ModelRoute(
                task=task,
                profile_id=main.id,
                required_capabilities=(
                    frozenset({ModelCapability.TOOLS})
                    if task in {ModelTask.CHAT_AGENT, ModelTask.PLUGIN_AGENT_SESSION}
                    else frozenset()
                ),
            )
            for task in ModelTask
        },
        search_backend=backend,
        search_connection=search.id if search else None,
    )


def settings(tmp_path, **overrides):
    return make_settings(
        "sqlite+aiosqlite:///:memory:",
        web_mode="tavily",
        tavily_api_key="",
        web_search_bridge_state_path=tmp_path / "search.sqlite3",
        **overrides,
    )


@pytest.mark.parametrize(
    "protocol",
    [
        ModelProtocol.GEMINI,
        ModelProtocol.ANTHROPIC_MESSAGES,
        ModelProtocol.RESPONSES,
    ],
)
async def test_chat_uses_selected_native_connection_without_main_history(
    tmp_path,
    monkeypatch,
    protocol,
):
    main = profile("main", ModelProtocol.CHAT_COMPLETIONS, search_mode=ModelSearchMode.BRIDGE)
    search = profile("search", protocol)
    original = main.model_dump()
    built = []

    def factory(selected, credential, **_kwargs):
        provider = FakeLLMProvider(
            lambda _request: ChatResponse(
                content="Search synthesis",
                latency_seconds=0,
                citations=(
                    ResponseCitation(
                        url="https://example.com/source",
                        title="Source",
                        origin=CitationOrigin.ANNOTATION,
                    ),
                ),
            )
        )
        built.append((selected, credential, provider))
        return provider

    monkeypatch.setattr(web_module, "create_model_provider", factory)
    clients = ModelClientPool(secret_overrides={"SEARCH_KEY": "search-secret"})
    module = WebModule(settings(tmp_path).web, lifecycle=LifecycleRegistry())
    routed = module.prepare(catalog(main, search), clients)
    token = current_web_model_task.set(ModelTask.CHAT_AGENT)
    try:
        response = await routed.search(WebSearchRequest("a query"))
        assert response.sources[0].url == "https://example.com/source"
        selected, credential, provider = built[0]
        assert selected.id == "search" and selected.protocol is protocol
        assert selected.max_retries == 0 and search.max_retries == 2
        assert credential == "search-secret"
        request = provider.requests[0]
        assert not request.tools and request.native_tools
        assert len(request.messages) == 1 and request.messages[0].role == "user"
        assert main.model_dump() == original
        assert len(built) == 1  # All task routes reuse this search connection.
    finally:
        current_web_model_task.reset(token)
        await routed.close()
        await clients.close()


async def test_explicit_search_backend_works_with_external_chat_and_no_tavily(tmp_path):
    main = profile("main", ModelProtocol.CHAT_COMPLETIONS, search_mode=ModelSearchMode.EXTERNAL)
    search = profile("search", ModelProtocol.GEMINI)
    clients = ModelClientPool(secret_overrides={"SEARCH_KEY": "secret"})
    module = WebModule(settings(tmp_path).web, lifecycle=LifecycleRegistry())
    provider = module.prepare(catalog(main, search, backend="model"), clients)
    assert isinstance(provider, NativeSearchBridge)
    assert provider.profile.id == "search"
    await provider.close()
    await clients.close()


def test_chat_bridge_needs_a_native_search_connection(tmp_path):
    main = profile("main", ModelProtocol.CHAT_COMPLETIONS, search_mode=ModelSearchMode.BRIDGE)
    module = WebModule(settings(tmp_path).web, lifecycle=LifecycleRegistry())
    with pytest.raises(ValueError, match="does not support native"):
        module.prepare(catalog(main), ModelClientPool())


def test_legacy_deepseek_setting_translates_search_only():
    main = profile("main", ModelProtocol.RESPONSES, provider="deepseek")
    main = main.model_copy(update={"base_url": "https://api.deepseek.com"})
    original = main.model_dump()
    old = catalog(main).model_copy(update={"search_connection": "main"})
    selected = effective_search_profile(old, "deepseek_anthropic")
    assert selected.protocol is ModelProtocol.ANTHROPIC_MESSAGES
    assert selected.base_url == "https://api.deepseek.com/anthropic/v1"
    assert selected.model == "deepseek-flash" and selected.thinking_enabled is False
    assert main.model_dump() == original
    explicit = old.model_copy(update={"search_backend": "model"})
    with pytest.raises(ValueError, match="does not support native"):
        effective_search_profile(explicit, "model")


def test_tavily_credentials_are_checked_after_catalog_selection(tmp_path):
    main = profile("main", ModelProtocol.CHAT_COMPLETIONS, search_mode=ModelSearchMode.EXTERNAL)
    configuration = settings(tmp_path)  # No premature check against unused .env defaults.
    module = WebModule(configuration.web, lifecycle=LifecycleRegistry())
    with pytest.raises(ValueError, match="Tavily search credentials"):
        module.prepare(catalog(main), ModelClientPool())


async def test_search_backend_round_trips_through_management(tmp_path):
    main = profile("main", ModelProtocol.CHAT_COMPLETIONS)
    path = tmp_path / "models.toml"
    configuration = settings(tmp_path, model_profiles_file=path)
    service = ConfigFileService(configuration)
    document = {
        "schema_version": 3,
        "profiles": {
            "main": main.model_dump(mode="json", exclude={"id", "headers"}, exclude_none=True)
        },
        "routes": {task.value: "main" for task in ModelTask},
        "search_backend": "model",
        "search_connection": "main",
    }
    await service.save("model_profiles", 0, {"document": document})
    snapshot = await service.read("model_profiles")
    assert snapshot["document"]["search_backend"] == "model"
    assert parse_model_profile_catalog(path.read_text()).search_backend == "model"
    snapshot["document"]["search_backend"] = None
    snapshot["document"]["search_connection"] = None
    await service.save("model_profiles", snapshot["revision"], {"document": snapshot["document"]})
    saved = tomllib.loads(path.read_text())
    assert "search_backend" not in saved and "search_connection" not in saved


async def test_hot_search_change_preserves_pinned_runner():
    class Provider:
        def __init__(self, name):
            self.name = name
            self.closed = asyncio.Event()

        async def search(self, _request):
            return self.name

        async def close(self):
            self.closed.set()

    old, new = Provider("old"), Provider("new")
    hot = HotWebSearchProvider(old)
    request = WebSearchRequest("query")
    with hot.pin():
        hot.activate(new)
        assert await hot.search(request) == "old"
        assert not old.closed.is_set()
    await asyncio.wait_for(old.closed.wait(), 1)
    assert await hot.search(request) == "new"
    await hot.close()
    assert new.closed.is_set()


@pytest.mark.parametrize("background_mode", [ModelSearchMode.EXTERNAL, ModelSearchMode.BRIDGE])
async def test_background_profile_does_not_require_unused_search(tmp_path, background_mode):
    main = profile("main", ModelProtocol.GEMINI, search_mode=ModelSearchMode.BRIDGE)
    background = profile("background", ModelProtocol.CHAT_COMPLETIONS, search_mode=background_mode)
    mixed = catalog(main)
    mixed = mixed.model_copy(
        update={
            "profiles": {main.id: main, background.id: background},
            "routes": {
                **mixed.routes,
                ModelTask.MEMORY_EXTRACTION: ModelRoute(
                    task=ModelTask.MEMORY_EXTRACTION,
                    profile_id=background.id,
                ),
            },
        }
    )
    clients = ModelClientPool(secret_overrides={"MAIN_KEY": "secret"})
    module = WebModule(settings(tmp_path).web, lifecycle=LifecycleRegistry())
    routed = module.prepare(mixed, clients)
    assert routed.default is None
    assert routed.bridges[ModelTask.CHAT_AGENT].profile.id == "main"
    await routed.close()
    await clients.close()
