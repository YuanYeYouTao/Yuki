"""Built-in gateway Provider implementations."""

from pathlib import Path

from yuki.gateway.provider import GatewayProviderCatalog
from yuki.gateway.providers.app import AppProvider
from yuki.gateway.providers.feishu import FeishuProvider
from yuki.gateway.providers.snowluma import (
    SNOWLUMA_PROVIDER_ID,
    SnowLumaProvider,
)
from yuki.gateway.providers.telegram import TelegramProvider


def builtin_provider_catalog(
    *, qq_transfer_root: Path | None = None, qq_gateway_root: str = ""
) -> GatewayProviderCatalog:
    """Return the platform and personal-entry gateway providers."""

    return GatewayProviderCatalog(
        (
            SnowLumaProvider(qq_transfer_root, qq_gateway_root),
            AppProvider(),
            AppProvider("acp"),
            TelegramProvider(),
            FeishuProvider(),
            FeishuProvider(platform="lark"),
        )
    )


__all__ = [
    "SNOWLUMA_PROVIDER_ID",
    "SnowLumaProvider",
    "builtin_provider_catalog",
]
