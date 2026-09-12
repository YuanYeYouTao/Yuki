"""Controlled web-search application module."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.model_runtime.models import ModelProfile
from qq_ai_bot.settings_domains import WebSettings
from qq_ai_bot.web.base import WebSearchProvider
from qq_ai_bot.web.deepseek_bridge import DeepSeekSearchBridge
from qq_ai_bot.web.models import WebMode
from qq_ai_bot.web.tavily import TavilyWebSearchProvider


@dataclass(frozen=True, slots=True)
class WebBundle:
    provider: WebSearchProvider | None


class WebModule:
    def __init__(
        self,
        settings: WebSettings,
        *,
        lifecycle: LifecycleRegistry,
        search_profile: ModelProfile | None = None,
        search_api_key: str = "",
    ) -> None:
        self._settings = settings
        self._lifecycle = lifecycle
        self._search_profile = search_profile
        self._search_api_key = search_api_key

    def build(self) -> WebBundle:
        settings = self._settings
        if settings.mode not in {WebMode.TAVILY, WebMode.BOTH}:
            return WebBundle(None)
        if settings.web_search_backend == "deepseek_anthropic":
            profile = self._search_profile
            if (
                profile is None
                or profile.provider.casefold() != "deepseek"
                or urlsplit(profile.base_url).hostname != "api.deepseek.com"
                or not self._search_api_key
            ):
                raise ValueError(
                    "DeepSeek search requires an official DeepSeek main profile and key"
                )
        fallback = (
            TavilyWebSearchProvider(
                api_key=settings.tavily_api_key,
                search_depth=settings.web_search_depth,
                extract_max_results=settings.web_extract_max_results,
                timeout_seconds=settings.web_timeout_seconds,
                max_retries=settings.web_max_retries,
                global_concurrency=settings.web_global_concurrency,
            )
            if settings.tavily_api_key
            else None
        )
        provider: WebSearchProvider | None = fallback
        if settings.web_search_backend == "deepseek_anthropic":
            provider = DeepSeekSearchBridge(
                api_key=self._search_api_key,
                state_path=settings.web_search_bridge_state_path,
                fallback=fallback,
                timeout_seconds=settings.web_timeout_seconds,
                extract_max_results=settings.web_extract_max_results,
            )
        if provider is None:
            raise ValueError("Tavily search credentials are required")
        self._lifecycle.register("web", close=provider.close)
        return WebBundle(provider)
