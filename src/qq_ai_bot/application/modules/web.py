"""Controlled web-search application module."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.settings_domains import WebSettings
from qq_ai_bot.web.base import WebSearchProvider
from qq_ai_bot.web.models import WebMode
from qq_ai_bot.web.tavily import TavilyWebSearchProvider


@dataclass(frozen=True, slots=True)
class WebBundle:
    provider: WebSearchProvider | None


class WebModule:
    def __init__(self, settings: WebSettings, *, lifecycle: LifecycleRegistry) -> None:
        self._settings = settings
        self._lifecycle = lifecycle

    def build(self) -> WebBundle:
        settings = self._settings
        if settings.mode not in {WebMode.TAVILY, WebMode.BOTH}:
            return WebBundle(None)
        provider = TavilyWebSearchProvider(
            api_key=settings.tavily_api_key,
            search_depth=settings.web_search_depth,
            extract_max_results=settings.web_extract_max_results,
            timeout_seconds=settings.web_timeout_seconds,
            max_retries=settings.web_max_retries,
            global_concurrency=settings.web_global_concurrency,
        )
        self._lifecycle.register("web", close=provider.close)
        return WebBundle(provider)
