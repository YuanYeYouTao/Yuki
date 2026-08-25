"""Provider-neutral in-memory gateway connections. Handles never persist."""

from qq_ai_bot.gateway.provider import (
    GatewayConnectionProfile,
    GatewayProvider,
    GatewayProviderCatalog,
)
from qq_ai_bot.gateway.registry import (
    ConnectionResolution,
    GatewayConnectionConflict,
    GatewayConnectionRegistry,
    RegistryClosed,
)

__all__ = [
    "ConnectionResolution",
    "GatewayConnectionConflict",
    "GatewayConnectionProfile",
    "GatewayConnectionRegistry",
    "GatewayProvider",
    "GatewayProviderCatalog",
    "RegistryClosed",
]
