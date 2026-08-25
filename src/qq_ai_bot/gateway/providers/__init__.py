"""Built-in gateway Provider implementations."""

from qq_ai_bot.gateway.provider import GatewayProviderCatalog
from qq_ai_bot.gateway.providers.napcat import (
    NAPCAT_PROVIDER_ID,
    NapCatProvider,
    napcat_provider_catalog,
)
from qq_ai_bot.gateway.providers.snowluma import (
    SNOWLUMA_PROVIDER_ID,
    SnowLumaProvider,
)


def builtin_provider_catalog() -> GatewayProviderCatalog:
    """Return every built-in QQ gateway provider with explicit selection required."""

    return GatewayProviderCatalog((NapCatProvider(), SnowLumaProvider()))


__all__ = [
    "NAPCAT_PROVIDER_ID",
    "SNOWLUMA_PROVIDER_ID",
    "NapCatProvider",
    "SnowLumaProvider",
    "builtin_provider_catalog",
    "napcat_provider_catalog",
]
