"""Provider-neutral in-memory gateway connections. Handles never persist."""

from qq_ai_bot.gateway.provider import (
    GatewayConnectionProfile,
    GatewayProvider,
    GatewayProviderCatalog,
)
from qq_ai_bot.gateway.registry import (
    ConnectionResolution,
    GatewayConnectionRegistry,
    RegistryClosed,
)

__all__ = [
    "ConnectionResolution",
    "GatewayConnectionProfile",
    "GatewayConnectionRegistry",
    "GatewayProvider",
    "GatewayProviderCatalog",
    "RegistryClosed",
]
