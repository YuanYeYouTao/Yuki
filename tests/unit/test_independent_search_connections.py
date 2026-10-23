"""Search routing, credential ownership and saved settings remain independent of Main."""

import asyncio
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import tomlkit
from tests.conftest import make_settings

from yuki.admin.config_files import ConfigFileService
from yuki.application.lifecycle import LifecycleRegistry
from yuki.application.modules import web as web_module
from yuki.application.modules.web import HotWebSearchProvider, WebModule
from yuki.domain.messages import ChatResponse, CitationOrigin, ResponseCitation
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.executor import TaskModelExecutor
from yuki.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelSearchMode,
    ModelTask,
)
from yuki.model_runtime.pool import ModelClientPool
from yuki.model_runtime.profiles import ModelProfileCatalog, parse_model_profile_catalog
from yuki.model_runtime.routes import ModelRouter
from yuki.web.base import WebSearchError
from yuki.web.models import WebSearchRequest, WebSearchResponse, WebSearchSource
from yuki.web.native_bridge import NativeSearchBridge
from yuki.web.page_extractor import DirectPageExtractor
from yuki.web.route_context import current_web_model_task
from yuki.web.search_config import effective_search_profile


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
    values = {
        "web_mode": "native",
        "tavily_api_key": "",
        "web_search_bridge_state_path": tmp_path / "search.sqlite3",
        **overrides,
    }
    return make_settings("sqlite+aiosqlite:///:memory:", **values)


async def test_bridge_reader_uses_web_timeout_without_a_model_request(tmp_path, monkeypatch):
    main = profile("main", ModelProtocol.CHAT_COMPLETIONS, search_mode=ModelSearchMode.BRIDGE)
    search = profile("search", ModelProtocol.GEMINI)
    model = FakeLLMProvider(lambda _: ChatResponse(content="unused", latency_seconds=0))
    monkeypatch.setattr(web_module, "create_model_provider", lambda *_args: model)
    clients = ModelClientPool(secret_overrides={"SEARCH_KEY": "synthetic"})
    module = WebModule(
        settings(tmp_path, web_timeout_seconds=0.03).web, lifecycle=LifecycleRegistry()
    )
    routed = module.prepare(catalog(main, search), clients)
    token = current_web_model_task.set(ModelTask.CHAT_AGENT)
    try:
        bridge = routed.bridges[ModelTask.CHAT_AGENT]

        async def stalled_download(*_args, **_kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(bridge.extractor.media, "_download_with_redirects", stalled_download)
        with pytest.raises(WebSearchError) as failure:
            await asyncio.wait_for(routed.extract("https://example.com/", "read"), 1)
        assert failure.value.__cause__.code == "timeout"
        assert bridge.profile.timeout_seconds == search.timeout_seconds
        assert not model.requests
    finally:
        current_web_model_task.reset(token)
        await routed.close()
        await clients.close()


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


@pytest.mark.parametrize("search_mode", [None, ModelSearchMode.EXTERNAL])
async def test_explicit_search_backend_works_with_external_chat_and_no_tavily(
    tmp_path, search_mode
):
    main = profile("main", ModelProtocol.CHAT_COMPLETIONS, search_mode=search_mode)
    search = profile("search", ModelProtocol.GEMINI)
    clients = ModelClientPool(secret_overrides={"SEARCH_KEY": "secret"})
    module = WebModule(settings(tmp_path).web, lifecycle=LifecycleRegistry())
    routed = module.prepare(catalog(main, search, backend="model"), clients)
    assert isinstance(routed.default, NativeSearchBridge)
    assert routed.default.profile.id == "search"
    await routed.close()
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


@pytest.mark.parametrize("search_mode", [None, ModelSearchMode.EXTERNAL])
@pytest.mark.parametrize("backend_source", ["catalog", "environment"])
def test_tavily_credentials_are_checked_after_catalog_selection(
    tmp_path, search_mode, backend_source
):
    main = profile("main", ModelProtocol.CHAT_COMPLETIONS, search_mode=search_mode)
    configuration = settings(
        tmp_path, web_search_backend="tavily" if backend_source == "environment" else "model"
    )
    module = WebModule(configuration.web, lifecycle=LifecycleRegistry())
    with pytest.raises(ValueError, match="Tavily search credentials"):
        module.prepare(
            catalog(main, backend="tavily" if backend_source == "catalog" else None),
            ModelClientPool(),
        )


@pytest.mark.parametrize("tavily_key", ["", "unused-tavily-secret"])
async def test_model_search_and_page_reading_never_select_tavily_by_key(
    tmp_path, monkeypatch, tavily_key
):
    main = profile("main", ModelProtocol.CHAT_COMPLETIONS, search_mode=ModelSearchMode.BRIDGE)
    search = profile("search", ModelProtocol.GEMINI)
    source = WebSearchSource(
        "direct-page",
        "Actual page",
        "https://example.com/source",
        "example.com",
        "Page",
        "Page",
        provider="direct_http",
    )
    direct = AsyncMock(return_value=source)
    monkeypatch.setattr(DirectPageExtractor, "_extract", direct)

    def reject_tavily(_self):
        pytest.fail("a model search must not construct an unrelated Tavily service")

    monkeypatch.setattr(WebModule, "_tavily", reject_tavily)
    model = FakeLLMProvider(
        lambda _request: ChatResponse(
            content="Model summary",
            latency_seconds=0,
            provider_request_id="original-search-request",
            prompt_tokens=15,
            completion_tokens=4,
            cached_prompt_tokens=None,
            citations=(ResponseCitation("https://example.com/source"),),
        )
    )
    monkeypatch.setattr(web_module, "create_model_provider", lambda *_args: model)
    clients = ModelClientPool(secret_overrides={"SEARCH_KEY": "search-secret"})
    module = WebModule(
        settings(tmp_path, tavily_api_key=tavily_key).web,
        lifecycle=LifecycleRegistry(),
    )
    routed = module.prepare(catalog(main, search, backend="model"), clients)
    token = current_web_model_task.set(ModelTask.CHAT_AGENT)
    try:
        bridge = routed.bridges[ModelTask.CHAT_AGENT]
        assert isinstance(bridge.extractor, DirectPageExtractor)
        result = await routed.search(WebSearchRequest("query"))
        assert result.provider_request_id == "original-search-request"
        assert (result.prompt_tokens, result.completion_tokens) == (15, 4)
        assert result.cached_prompt_tokens is None
        assert result.sources[0].relevant_content == ""
        direct.assert_not_awaited()
        assert await routed.extract(source.url, "query") == source
        assert len(model.requests) == 1
        direct.assert_awaited_once_with(source.url, "query")
    finally:
        current_web_model_task.reset(token)
        await routed.close()
        await clients.close()


@pytest.mark.parametrize("tavily_key", ["", "unused-tavily-secret"])
@pytest.mark.parametrize("inline_native", [False, True])
async def test_page_reading_remains_direct_without_a_local_search_connection(
    tmp_path, monkeypatch, tavily_key, inline_native
):
    main = profile(
        "main",
        ModelProtocol.ANTHROPIC_MESSAGES if inline_native else ModelProtocol.CHAT_COMPLETIONS,
    )
    if inline_native:
        main = ModelProfile.model_validate(
            {
                **main.model_dump(),
                "search_mode": ModelSearchMode.NATIVE,
                "capabilities": {ModelCapability.TOOLS, ModelCapability.NATIVE_WEB_SEARCH},
            }
        )
    source = WebSearchSource(
        "direct-page",
        "Page",
        "https://example.com/page",
        "example.com",
        "Body",
        "Body",
        provider="direct_http",
    )
    direct = AsyncMock(return_value=source)
    monkeypatch.setattr(DirectPageExtractor, "_extract", direct)

    def reject_tavily(_self):
        pytest.fail("a Tavily key must not supply an unselected local backend")

    monkeypatch.setattr(WebModule, "_tavily", reject_tavily)
    module = WebModule(
        settings(tmp_path, tavily_api_key=tavily_key).web,
        lifecycle=LifecycleRegistry(),
    )
    routed = module.prepare(catalog(main, backend="model"), None)
    token = current_web_model_task.set(ModelTask.CHAT_AGENT)
    try:
        assert await routed.extract(source.url, "query") == source
        with pytest.raises(WebSearchError) as failure:
            await routed.search(WebSearchRequest("query"))
        assert failure.value.code == "search_unavailable"
        direct.assert_awaited_once_with(source.url, "query")
    finally:
        current_web_model_task.reset(token)
        await routed.close()


@pytest.mark.parametrize("search_mode", [None, ModelSearchMode.EXTERNAL])
@pytest.mark.parametrize("backend_source", ["catalog", "environment"])
async def test_explicit_tavily_backend_remains_selected(
    tmp_path, monkeypatch, search_mode, backend_source
):
    main = profile("main", ModelProtocol.CHAT_COMPLETIONS, search_mode=search_mode)
    tavily = SimpleNamespace(search=AsyncMock(), extract=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(WebModule, "_tavily", lambda _self: tavily)
    module = WebModule(
        settings(
            tmp_path,
            tavily_api_key="explicit-tavily-secret",
            web_search_backend="tavily" if backend_source == "environment" else "model",
        ).web,
        lifecycle=LifecycleRegistry(),
    )
    routed = module.prepare(
        catalog(main, backend="tavily" if backend_source == "catalog" else None), None
    )
    try:
        request = WebSearchRequest("query")
        await routed.search(request)
        await routed.extract("https://example.com/page", "query")
        tavily.search.assert_awaited_once_with(request)
        tavily.extract.assert_awaited_once_with("https://example.com/page", "query")
    finally:
        await routed.close()
    tavily.close.assert_awaited_once()


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
    await service.save(0, {"document": document})
    snapshot = await service.read()
    assert snapshot["document"]["search_backend"] == "model"
    assert parse_model_profile_catalog(path.read_text()).search_backend == "model"
    snapshot["document"]["search_backend"] = None
    snapshot["document"]["search_connection"] = None
    await service.save(snapshot["revision"], {"document": snapshot["document"]})
    saved = tomllib.loads(path.read_text())
    assert saved["search_backend"] == "model" and "search_connection" not in saved


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


async def test_mixed_default_search_connections_keep_credentials_receipts_and_pinned_cache(
    tmp_path, monkeypatch
):
    main = profile("main", ModelProtocol.CHAT_COMPLETIONS, provider="deepseek")
    main = main.model_copy(update={"base_url": "https://deepseek-gateway.example/v1"})
    plugin = profile("plugin", ModelProtocol.GEMINI, provider="gemini")
    plugin = plugin.model_copy(update={"base_url": "https://gemini-gateway.example/v1beta"})
    document = {
        "schema_version": 3,
        "profiles": {
            item.id: item.model_dump(mode="json", exclude={"id"}, exclude_none=True)
            for item in (main, plugin)
        },
        "routes": {
            task.value: "plugin" if task is ModelTask.PLUGIN_AGENT_SESSION else "main"
            for task in ModelTask
        },
    }
    original = parse_model_profile_catalog(tomlkit.dumps(document))
    assert original.search_connection is None
    assert original.profiles["main"].protocol is ModelProtocol.CHAT_COMPLETIONS
    built = []

    def factory(selected, credential):
        provider = FakeLLMProvider(
            lambda _request: ChatResponse(
                content="Provider summary",
                latency_seconds=0,
                provider_request_id=f"{selected.id}:{selected.api_key_env}",
                prompt_tokens=12 if selected.protocol is ModelProtocol.ANTHROPIC_MESSAGES else 18,
                completion_tokens=3,
                cached_prompt_tokens=None,
                citations=(ResponseCitation(f"https://example.com/{selected.id}"),),
            )
        )
        built.append((selected, credential, provider))
        return provider

    monkeypatch.setattr(web_module, "create_model_provider", factory)
    invocations = SimpleNamespace(record=AsyncMock())
    module = WebModule(
        settings(tmp_path, tavily_api_key="unused-tavily-secret").web,
        lifecycle=LifecycleRegistry(),
        invocations=invocations,
    )
    old_clients = ModelClientPool(
        secret_overrides={"MAIN_KEY": "main-secret", "PLUGIN_KEY": "plugin-secret"}
    )
    new_clients = ModelClientPool(
        secret_overrides={"MAIN_KEY_V2": "new-main-secret", "PLUGIN_KEY_V2": "new-plugin-secret"}
    )
    hot = HotWebSearchProvider(module.prepare(original, old_clients))

    async def search(task):
        token = current_web_model_task.set(task)
        try:
            return await hot.search(WebSearchRequest("same query"))
        finally:
            current_web_model_task.reset(token)

    try:
        chat_before = await search(ModelTask.CHAT_AGENT)
        plugin_before = await search(ModelTask.PLUGIN_AGENT_SESSION)
        assert chat_before.provider_request_id == "main_search:MAIN_KEY"
        assert plugin_before.provider_request_id == "plugin:PLUGIN_KEY"
        assert (chat_before.prompt_tokens, plugin_before.prompt_tokens) == (12, 18)
        assert chat_before.cached_prompt_tokens is plugin_before.cached_prompt_tokens is None
        document["profiles"]["main"]["api_key_env"] = "MAIN_KEY_V2"
        document["profiles"]["plugin"]["api_key_env"] = "PLUGIN_KEY_V2"
        updated = parse_model_profile_catalog(tomlkit.dumps(document))
        with hot.pin():
            hot.activate(module.prepare(updated, new_clients))
            assert await search(ModelTask.CHAT_AGENT) == chat_before
            assert await search(ModelTask.PLUGIN_AGENT_SESSION) == plugin_before
        assert (await search(ModelTask.CHAT_AGENT)).provider_request_id == "main_search:MAIN_KEY_V2"
        assert (
            await search(ModelTask.PLUGIN_AGENT_SESSION)
        ).provider_request_id == "plugin:PLUGIN_KEY_V2"
        assert [(item.base_url, credential) for item, credential, _ in built] == [
            ("https://deepseek-gateway.example/anthropic/v1", "main-secret"),
            ("https://gemini-gateway.example/v1beta", "plugin-secret"),
            ("https://deepseek-gateway.example/anthropic/v1", "new-main-secret"),
            ("https://gemini-gateway.example/v1beta", "new-plugin-secret"),
        ]
        assert all(len(provider.requests) == 1 for _, _, provider in built)
        assert all(item.max_retries == 0 for item, _, _ in built)
        assert [call.kwargs["profile_id"] for call in invocations.record.call_args_list] == [
            "main_search",
            "plugin",
            "main_search",
            "plugin",
        ]
    finally:
        await hot.close()
        await old_clients.close()
        await new_clients.close()


@pytest.mark.parametrize(
    "mode,backend,has_own_connection,expected",
    [
        (ModelSearchMode.BRIDGE, "model", True, "own"),
        (ModelSearchMode.BRIDGE, "tavily", True, "own"),
        (ModelSearchMode.EXTERNAL, "model", True, "own"),
        (ModelSearchMode.EXTERNAL, "tavily", True, "own"),
        (ModelSearchMode.BOTH, "model", True, "own"),
        (ModelSearchMode.BOTH, "tavily", True, "own"),
        (None, "model", True, "own"),
        (None, "tavily", True, "own"),
        (ModelSearchMode.EXTERNAL, "model", False, "shared"),
        (ModelSearchMode.EXTERNAL, "tavily", False, "tavily"),
        (ModelSearchMode.BOTH, "model", False, "shared"),
        (ModelSearchMode.BOTH, "tavily", False, "tavily"),
        (ModelSearchMode.NATIVE, "model", True, None),
        (ModelSearchMode.NATIVE, "tavily", True, None),
    ],
)
async def test_explicit_bridge_and_external_choices_use_their_declared_connections(
    tmp_path, monkeypatch, mode, backend, has_own_connection, expected
):
    inline = mode in {ModelSearchMode.NATIVE, ModelSearchMode.BOTH}
    main = profile("main", ModelProtocol.GEMINI if inline else ModelProtocol.CHAT_COMPLETIONS)
    main = ModelProfile.model_validate(
        {
            **main.model_dump(),
            "search_mode": mode,
            "capabilities": (
                {ModelCapability.TOOLS, ModelCapability.NATIVE_WEB_SEARCH}
                if inline
                else {ModelCapability.TOOLS}
            ),
        }
    )
    own = profile("own", ModelProtocol.GEMINI)
    shared = profile("shared", ModelProtocol.GEMINI)
    mixed = catalog(main, shared, backend=backend)
    if has_own_connection:
        main = main.model_copy(update={"search_connection": "own"})
    mixed = ModelProfileCatalog(
        profiles={item.id: item for item in (main, own, shared)},
        routes=mixed.routes,
        search_backend=backend,
        search_connection=shared.id,
    )
    built = []

    def factory(selected, credential):
        built.append((selected.id, credential))
        return FakeLLMProvider(
            lambda _request: ChatResponse(
                content="Search summary",
                latency_seconds=0,
                provider_request_id=selected.id,
                citations=(ResponseCitation(f"https://example.com/{selected.id}"),),
            )
        )

    monkeypatch.setattr(web_module, "create_model_provider", factory)
    tavily = SimpleNamespace(
        search=AsyncMock(return_value=WebSearchResponse("query", (), "tavily", 0)),
        close=AsyncMock(),
    )
    monkeypatch.setattr(WebModule, "_tavily", lambda _self: tavily)
    clients = ModelClientPool(secret_overrides={"OWN_KEY": "own-key", "SHARED_KEY": "shared-key"})
    module = WebModule(
        settings(tmp_path, tavily_api_key="explicit-tavily-key").web,
        lifecycle=LifecycleRegistry(),
    )
    routed = module.prepare(mixed, clients)
    token = current_web_model_task.set(ModelTask.CHAT_AGENT)
    try:
        if expected is None:
            with pytest.raises(WebSearchError) as failure:
                await routed.search(WebSearchRequest("query"))
            assert failure.value.code == "search_unavailable"
        else:
            result = await routed.search(WebSearchRequest("query"))
            assert result.provider_request_id == expected
        assert built == ([] if expected in {None, "tavily"} else [(expected, f"{expected}-key")])
        assert tavily.search.await_count == (expected == "tavily")
    finally:
        current_web_model_task.reset(token)
        await routed.close()
        await clients.close()


async def test_absent_search_connection_preserves_existing_profile_revision():
    main = profile("main", ModelProtocol.RESPONSES, provider="openai")
    clients = ModelClientPool()
    executor = TaskModelExecutor(router=ModelRouter(catalog(main)), pool=clients)
    try:
        original_revision = executor.profile_revision(ModelTask.CHAT_AGENT)
        # This is the revision of the same Profile before search_connection existed.
        assert (
            original_revision == "6917ef0c8106a52252fc86ecaabd538f60bf00968e80ebe147c27f6fbba9544e"
        )
        revisions = []
        for selected_id in ("own", "other"):
            selected = profile(selected_id, ModelProtocol.GEMINI)
            configured = catalog(
                main.model_copy(update={"search_connection": selected_id}), selected
            )
            configured_executor = TaskModelExecutor(router=ModelRouter(configured), pool=clients)
            revisions.append(configured_executor.profile_revision(ModelTask.CHAT_AGENT))
        assert len({original_revision, *revisions}) == 3
    finally:
        await clients.close()


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
