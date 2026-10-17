"""Built-in gateway Provider implementations."""

from pathlib import Path

from qq_ai_bot.gateway.provider import GatewayProviderCatalog
from qq_ai_bot.gateway.providers.app import AppProvider
from qq_ai_bot.gateway.providers.snowluma import (
    SNOWLUMA_PROVIDER_ID,
    SnowLumaProvider,
)
from qq_ai_bot.gateway.providers.telegram import TelegramProvider


def builtin_provider_catalog(
    *, qq_transfer_root: Path | None = None, qq_gateway_root: str = ""
) -> GatewayProviderCatalog:
    """Return the built-in QQ gateway providers."""

    return GatewayProviderCatalog(
        (SnowLumaProvider(qq_transfer_root, qq_gateway_root), AppProvider(), TelegramProvider())
    )


__all__ = [
    "SNOWLUMA_PROVIDER_ID",
    "SnowLumaProvider",
    "builtin_provider_catalog",
]
