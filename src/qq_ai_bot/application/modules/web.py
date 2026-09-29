"""Controlled web search with an explicit per-task Gemini bridge option."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.model_runtime.models import ModelProfile, ModelProtocol, ModelSearchMode, ModelTask
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.settings_domains import WebSettings
from qq_ai_bot.web.base import WebSearchError, WebSearchProvider
from qq_ai_bot.web.bridge_state import BridgeState
from qq_ai_bot.web.deepseek_bridge import DeepSeekSearchBridge
from qq_ai_bot.web.gemini_bridge import GeminiSearchBridge
from qq_ai_bot.web.models import WebMode, WebSearchRequest, WebSearchResponse, WebSearchSource
from qq_ai_bot.web.route_context import current_web_model_task
from qq_ai_bot.web.tavily import TavilyWebSearchProvider

logger = logging.getLogger(__name__)


class HotWebSearchProvider:
    """Keep references stable while new searches use an activated backend."""

    def __init__(self, provider: WebSearchProvider) -> None:
        self._active: WebSearchProvider | None = provider
        self._retired: dict[int, WebSearchProvider] = {}
        self._inflight: dict[int, int] = {}
        self._close_tasks: set[asyncio.Task[None]] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._closing = False
        self._pinned: ContextVar[WebSearchProvider | None] = ContextVar(
            f"web_provider_pin_{id(self)}", default=None
        )

    @contextmanager
    def pin(self) -> Iterator[None]:
        """Keep the selected provider alive for the full model/tool Runner."""
        provider = self._borrow()
        token = self._pinned.set(provider)
        try:
            yield
        finally:
            self._pinned.reset(token)
            self._release(provider)

    def activate(self, provider: WebSearchProvider) -> None:
        if self._closing:
            raise RuntimeError("web search provider is closing")
        previous = self._active
        if previous is provider:
            return
        assert previous is not None
        self._active = provider
        self._retired[id(previous)] = previous
        if self._inflight.get(id(previous), 0) == 0:
            self._retire(previous)

    def _retire(self, provider: WebSearchProvider) -> None:
        self._retired.pop(id(provider), None)
        task = asyncio.create_task(provider.close())
        self._close_tasks.add(task)

        def completed(done: asyncio.Task[None]) -> None:
            self._close_tasks.discard(done)
            try:
                done.result()
            except Exception:
                logger.exception("retired web search provider close failed")

        task.add_done_callback(completed)

    def _borrow(self) -> WebSearchProvider:
        provider = self._active
        if provider is None or self._closing:
            raise WebSearchError("search_unavailable", "联网搜索已停止")
        key = id(provider)
        self._inflight[key] = self._inflight.get(key, 0) + 1
        self._idle.clear()
        return provider

    def _release(self, provider: WebSearchProvider) -> None:
        key = id(provider)
        remaining = self._inflight[key] - 1
        if remaining:
            self._inflight[key] = remaining
        else:
            del self._inflight[key]
            if key in self._retired:
                self._retire(provider)
        if not self._inflight:
            self._idle.set()

    async def search(self, request: WebSearchRequest) -> WebSearchResponse:
        pinned = self._pinned.get()
        if pinned is not None:
            return await pinned.search(request)
        provider = self._borrow()
        try:
            return await provider.search(request)
        finally:
            self._release(provider)

    async def extract(self, url: str, query: str) -> WebSearchSource:
        pinned = self._pinned.get()
        if pinned is not None:
            return await pinned.extract(url, query)
        provider = self._borrow()
        try:
            return await provider.extract(url, query)
        finally:
            self._release(provider)

    async def close(self) -> None:
        self._closing = True
        await self._idle.wait()
        providers = [*self._retired.values()]
        if self._active is not None:
            providers.append(self._active)
        self._retired.clear()
        self._active = None
        errors = await asyncio.gather(
            *(provider.close() for provider in providers), return_exceptions=True
        )
        if self._close_tasks:
            await asyncio.gather(*self._close_tasks)
        for error in errors:
            if isinstance(error, BaseException):
                raise error


class TaskRoutedWebSearchProvider:
    """Choose a declared bridge by the invoking model task, never by query text."""

    def __init__(
        self,
        default: WebSearchProvider,
        bridges: Mapping[ModelTask, WebSearchProvider],
    ) -> None:
        self.default = default
        self.bridges = bridges

    def _selected(self) -> WebSearchProvider:
        task = current_web_model_task.get()
        return self.bridges.get(task, self.default) if task is not None else self.default

    async def search(self, request: WebSearchRequest) -> WebSearchResponse:
        return await self._selected().search(request)

    async def extract(self, url: str, query: str) -> WebSearchSource:
        return await self._selected().extract(url, query)

    async def close(self) -> None:
        unique = {id(item): item for item in (self.default, *self.bridges.values())}
        errors = await asyncio.gather(
            *(provider.close() for provider in unique.values()), return_exceptions=True
        )
        for error in errors:
            if isinstance(error, BaseException):
                raise error


@dataclass(frozen=True, slots=True)
class WebBundle:
    provider: HotWebSearchProvider | None


class WebModule:
    def __init__(
        self,
        settings: WebSettings,
        *,
        lifecycle: LifecycleRegistry,
        catalog: ModelProfileCatalog | None = None,
        clients: ModelClientPool | None = None,
        invocations: ModelInvocationRepository | None = None,
    ) -> None:
        self._settings = settings
        self._lifecycle = lifecycle
        self._catalog = catalog
        self._clients = clients
        self._invocations = invocations
        self._provider: HotWebSearchProvider | None = None

    def prepare(
        self,
        catalog: ModelProfileCatalog | None,
        clients: ModelClientPool | None,
        *,
        require_explicit: bool,
    ) -> WebSearchProvider | None:
        settings = self._settings
        bridge_profiles: dict[ModelTask, ModelProfile] = {}
        if catalog is not None:
            for task, route in catalog.routes.items():
                profile = catalog.profiles.get(route.profile_id)
                if (
                    profile is not None
                    and getattr(profile, "search_mode", None) is ModelSearchMode.BRIDGE
                ):
                    bridge_profiles[task] = profile
        if bridge_profiles and settings.mode not in {WebMode.TAVILY, WebMode.BOTH}:
            raise ValueError("Gemini bridge requires an external web mode")
        if settings.mode not in {WebMode.TAVILY, WebMode.BOTH}:
            return None
        if bridge_profiles:
            if clients is None:
                raise ValueError("Gemini bridge requires a configured model connection")
            for profile in bridge_profiles.values():
                if profile.protocol is not ModelProtocol.GEMINI:
                    raise ValueError("the separate native search bridge requires Gemini protocol")
                if not settings.tavily_api_key:
                    raise ValueError("Gemini bridge requires Tavily for page reading and fallback")
                if not clients.api_key_for(profile):
                    raise ValueError("Selected Gemini connection has no API key")
        default = self._default_provider(catalog, clients, require_explicit=require_explicit)
        if default is None or not bridge_profiles:
            return default
        assert clients is not None
        by_profile: dict[str, GeminiSearchBridge] = {}
        bridges: dict[ModelTask, GeminiSearchBridge] = {}
        for task, profile in bridge_profiles.items():
            bridge = by_profile.get(profile.id)
            if bridge is None:
                bridge = self._gemini_bridge(profile, clients)
                by_profile[profile.id] = bridge
            bridges[task] = bridge
        return TaskRoutedWebSearchProvider(default, bridges)

    def _gemini_bridge(
        self, profile: ModelProfile, clients: ModelClientPool
    ) -> GeminiSearchBridge:
        settings = self._settings
        if profile.protocol is not ModelProtocol.GEMINI:
            raise ValueError("the separate native search bridge requires Gemini protocol")
        if not settings.tavily_api_key:
            raise ValueError("Gemini bridge requires Tavily for page reading and fallback")
        key = clients.api_key_for(profile)
        if not key:
            raise ValueError("Selected Gemini connection has no API key")
        return GeminiSearchBridge(
            profile=profile,
            credential=key,
            provider=GeminiProvider(
                base_url=profile.base_url,
                api_key=key,
                timeout_seconds=min(profile.timeout_seconds, settings.web_timeout_seconds),
                max_retries=0,
                options=profile.wire_options,
                headers=profile.headers,
            ),
            state=BridgeState(settings.web_search_bridge_state_path),
            fallback=self._tavily(),
            invocations=self._invocations,
        )

    def _default_provider(
        self,
        catalog: ModelProfileCatalog | None,
        clients: ModelClientPool | None,
        *,
        require_explicit: bool,
    ) -> WebSearchProvider | None:
        settings = self._settings
        if settings.web_search_backend == "tavily":
            if not settings.tavily_api_key:
                raise ValueError("Tavily search credentials are required")
            return self._tavily()
        if catalog is None or clients is None:
            raise ValueError("DeepSeek search requires a configured model connection")
        connection = catalog.search_connection
        if connection is None and not require_explicit:
            # Legacy startup only: prior versions attached search to the chat route.
            connection = catalog.routes[ModelTask.CHAT_AGENT].profile_id
        if connection is None:
            raise ValueError("Select a DeepSeek search connection in the WebUI")
        profile = catalog.profiles.get(connection)
        if (
            profile is None
            or profile.provider.casefold() != "deepseek"
            or urlsplit(profile.base_url).scheme != "https"
            or urlsplit(profile.base_url).hostname != "api.deepseek.com"
        ):
            raise ValueError("DeepSeek search requires an official DeepSeek connection")
        api_key = clients.api_key_for(profile)
        if not api_key:
            raise ValueError("Selected DeepSeek search connection has no API key")
        fallback = self._tavily() if settings.tavily_api_key else None
        return DeepSeekSearchBridge(
            api_key=api_key,
            state_path=settings.web_search_bridge_state_path,
            fallback=fallback,
            timeout_seconds=settings.web_timeout_seconds,
            extract_max_results=settings.web_extract_max_results,
        )

    def _tavily(self) -> TavilyWebSearchProvider:
        settings = self._settings
        return TavilyWebSearchProvider(
            api_key=settings.tavily_api_key,
            search_depth=settings.web_search_depth,
            extract_max_results=settings.web_extract_max_results,
            timeout_seconds=settings.web_timeout_seconds,
            max_retries=settings.web_max_retries,
            global_concurrency=settings.web_global_concurrency,
        )

    def activate(self, provider: WebSearchProvider | None) -> None:
        if self._provider is None:
            if provider is not None:
                raise ValueError("web search mode requires restart before enabling")
            return
        if provider is None:
            raise ValueError("web search mode requires restart before disabling")
        self._provider.activate(provider)

    def build(self) -> WebBundle:
        provider = self.prepare(self._catalog, self._clients, require_explicit=False)
        if provider is None:
            return WebBundle(None)
        self._provider = HotWebSearchProvider(provider)
        self._lifecycle.register("web", close=self._provider.close)
        return WebBundle(self._provider)
