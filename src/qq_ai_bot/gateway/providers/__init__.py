"""Built-in gateway Provider implementations."""

from qq_ai_bot.gateway.providers.napcat import (
    NAPCAT_PROVIDER_ID,
    NapCatProvider,
    napcat_provider_catalog,
)

__all__ = [
    "NAPCAT_PROVIDER_ID",
    "NapCatProvider",
    "napcat_provider_catalog",
]
