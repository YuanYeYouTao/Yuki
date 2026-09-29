"""Controlled web search, independent from the active chat-model route."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.settings_domains import WebSettings
from qq_ai_bot.web.base import WebSearchError, WebSearchProvider
from qq_ai_bot.web.deepseek_bridge import DeepSeekSearchBridge
from qq_ai_bot.web.models import WebMode, WebSearchRequest, WebSearchResponse, WebSearchSource
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
        provider = self._borrow()
        try:
            return await provider.search(request)
        finally:
            self._release(provider)

    async def extract(self, url: str, query: str) -> WebSearchSource:
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
    ) -> None:
        self._settings = settings
        self._lifecycle = lifecycle
        self._catalog = catalog
        self._clients = clients
        self._provider: HotWebSearchProvider | None = None

    def prepare(
        self,
        catalog: ModelProfileCatalog | None,
        clients: ModelClientPool | None,
        *,
        require_explicit: bool,
    ) -> WebSearchProvider | None:
        settings = self._settings
        if settings.mode not in {WebMode.TAVILY, WebMode.BOTH}:
            return None
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
