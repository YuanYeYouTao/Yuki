"""Resolve explicit search connections independently from main model routes."""

from urllib.parse import urlsplit

from yuki.llm.vendor_policy import supports_native_search
from yuki.model_runtime.models import ModelProfile, ModelProtocol
from yuki.model_runtime.profiles import ModelProfileCatalog


def effective_search_profile(
    catalog: ModelProfileCatalog,
    backend: str,
    own_profile: ModelProfile | None = None,
) -> ModelProfile:
    """Keep the retired DeepSeek key-borrowing setting readable on upgrade."""
    connection = (
        own_profile.search_connection
        if own_profile is not None and own_profile.search_connection is not None
        else catalog.search_connection
    )
    profile = catalog.profiles[connection] if connection is not None else own_profile
    if profile is None:
        raise ValueError("Select a native search connection in model profiles via Admin or CLI")
    if catalog.search_backend is None and backend == "deepseek_anthropic":
        endpoint = urlsplit(profile.base_url)
        if (
            profile.provider.casefold() != "deepseek"
            or endpoint.scheme != "https"
            or endpoint.hostname != "api.deepseek.com"
        ):
            raise ValueError("Legacy DeepSeek search requires an official DeepSeek connection")
        profile = profile.model_copy(
            update={
                "protocol": ModelProtocol.ANTHROPIC_MESSAGES,
                "base_url": "https://api.deepseek.com/anthropic/v1",
                "model": "deepseek-flash",
                "wire_options": None,
                "thinking_enabled": False,
                "reasoning_effort": None,
            }
        )
    if not supports_native_search(
        profile.provider.casefold(),
        profile.protocol.value,
        profile.wire_options,
        has_functions=False,
    ):
        raise ValueError("Selected search connection does not support native web search")
    if (
        profile.max_output_tokens_limit is not None
        and profile.default_max_output_tokens > profile.max_output_tokens_limit
    ):
        raise ValueError("search default exceeds configured provider output limit")
    return profile
