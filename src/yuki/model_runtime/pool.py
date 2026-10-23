"""Share provider clients only when provider endpoint and credential source match."""

from __future__ import annotations

import os
from collections.abc import Mapping

import httpx

from yuki.llm.anthropic_messages import AnthropicMessagesProvider
from yuki.llm.base import LLMConfigurationError, LLMProvider
from yuki.llm.deepseek_responses import DeepSeekResponsesProvider
from yuki.llm.fake import FakeLLMProvider
from yuki.llm.gemini import GeminiProvider
from yuki.llm.openai_compatible import OpenAICompatibleProvider
from yuki.llm.openai_responses import (
    OpenAICompatibleResponsesProvider,
    OpenAIResponsesProvider,
)
from yuki.model_runtime.models import ModelProfile, ModelProtocol


def create_model_provider(
    profile: ModelProfile, api_key: str, *, client: httpx.AsyncClient | None = None
) -> LLMProvider:
    """Construct an adapter; the caller owns its client lifetime."""
    if profile.provider.casefold() == "fake":
        return FakeLLMProvider()
    provider: LLMProvider
    if profile.protocol is ModelProtocol.CHAT_COMPLETIONS:
        provider = OpenAICompatibleProvider(
            base_url=profile.base_url,
            api_key=api_key,
            api_key_env=profile.api_key_env,
            timeout_seconds=profile.timeout_seconds,
            max_retries=profile.max_retries,
            client=client,
            options=profile.wire_options,
            headers=profile.headers,
            provider_name=profile.provider.casefold(),
        )
    elif profile.protocol in {ModelProtocol.ANTHROPIC_MESSAGES, ModelProtocol.GEMINI}:
        native_provider = (
            AnthropicMessagesProvider
            if profile.protocol is ModelProtocol.ANTHROPIC_MESSAGES
            else GeminiProvider
        )
        provider = native_provider(
            base_url=profile.base_url,
            api_key=api_key,
            api_key_env=profile.api_key_env,
            timeout_seconds=profile.timeout_seconds,
            max_retries=profile.max_retries,
            client=client,
            options=profile.wire_options,
            headers=profile.headers,
            provider_name=profile.provider.casefold(),
        )
    elif profile.provider.casefold() == "deepseek":
        provider = DeepSeekResponsesProvider(
            base_url=profile.base_url,
            api_key=api_key,
            api_key_env=profile.api_key_env,
            timeout_seconds=profile.timeout_seconds,
            max_retries=profile.max_retries,
            client=client,
            headers=profile.headers,
            provider_name=profile.provider.casefold(),
        )
    else:
        response_provider = (
            OpenAIResponsesProvider
            if profile.provider.casefold() == "openai"
            else OpenAICompatibleResponsesProvider
        )
        provider = response_provider(
            base_url=profile.base_url,
            api_key=api_key,
            api_key_env=profile.api_key_env,
            timeout_seconds=profile.timeout_seconds,
            max_retries=profile.max_retries,
            client=client,
            headers=profile.headers,
            provider_name=profile.provider.casefold(),
        )
    return provider


class ModelClientPool:
    """Own provider clients independently from business services."""

    def __init__(
        self,
        *,
        secret_overrides: Mapping[str, str] | None = None,
        injected_profiles: Mapping[str, LLMProvider] | None = None,
    ) -> None:
        self._secret_overrides = dict(secret_overrides or {})
        self._injected_profiles = dict(injected_profiles or {})
        self._clients: dict[tuple[str, float], LLMProvider] = {}
        self._connection_pools: dict[tuple[str, str, str], httpx.AsyncClient] = {}

    def api_key_for(self, profile: ModelProfile) -> str:
        secret = self._secret_overrides.get(profile.api_key_env)
        return os.getenv(profile.api_key_env, "") if secret is None else secret

    def configured(self, profile: ModelProfile) -> bool:
        """Project this pool's selected injection or credential without model I/O."""
        return (
            profile.id in self._injected_profiles
            or profile.provider.casefold() == "fake"
            or not profile.api_key_env
            or bool(self.api_key_for(profile))
        )

    def get(self, profile: ModelProfile, *, timeout_seconds: float | None = None) -> LLMProvider:
        injected = self._injected_profiles.get(profile.id)
        if injected is not None:
            return injected
        if timeout_seconds is not None:
            profile = profile.model_copy(update={"timeout_seconds": timeout_seconds})
        key = (profile.id, profile.timeout_seconds)
        existing = self._clients.get(key)
        if existing is not None:
            return existing
        if profile.provider.casefold() == "fake":
            provider: LLMProvider = FakeLLMProvider()
        else:
            api_key = self.api_key_for(profile)
            if profile.api_key_env and not api_key:
                raise LLMConfigurationError(
                    f"model profile {profile.id} is missing secret environment variable "
                    f"{profile.api_key_env}"
                )
            connection_key = (
                profile.provider.casefold(),
                profile.base_url.rstrip("/"),
                profile.api_key_env,
            )
            connection_pool = self._connection_pools.get(connection_key)
            if connection_pool is None:
                connection_pool = httpx.AsyncClient(
                    base_url=profile.base_url.rstrip("/"),
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )
                self._connection_pools[connection_key] = connection_pool
            provider = create_model_provider(profile, api_key, client=connection_pool)
        self._clients[key] = provider
        return provider

    async def close(self) -> None:
        closed: set[int] = set()
        errors: list[BaseException] = []
        resources: tuple[LLMProvider | httpx.AsyncClient, ...] = (
            *self._clients.values(),
            *self._injected_profiles.values(),
            *self._connection_pools.values(),
        )
        for resource in resources:
            if id(resource) in closed:
                continue
            closed.add(id(resource))
            try:
                if isinstance(resource, httpx.AsyncClient):
                    await resource.aclose()
                else:
                    await resource.close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            cancellation = next(
                (error for error in errors if not isinstance(error, Exception)), None
            )
            if cancellation is not None:
                for error in errors:
                    if error is not cancellation:
                        cancellation.add_note(f"close failed: {type(error).__name__}")
                raise cancellation
            raise BaseExceptionGroup("model pool close failed", errors)
