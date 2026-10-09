"""Built-in gateway Provider implementations."""

from qq_ai_bot.gateway.provider import GatewayProviderCatalog
from qq_ai_bot.gateway.providers.snowluma import (
    SNOWLUMA_PROVIDER_ID,
    SnowLumaProvider,
)


def builtin_provider_catalog() -> GatewayProviderCatalog:
    """Return the built-in QQ gateway providers."""

    return GatewayProviderCatalog((SnowLumaProvider(),))


__all__ = [
    "SNOWLUMA_PROVIDER_ID",
    "SnowLumaProvider",
    "builtin_provider_catalog",
]
