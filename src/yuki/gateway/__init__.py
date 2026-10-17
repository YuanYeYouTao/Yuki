"""Provider-neutral in-memory gateway connections. Handles never persist."""

from yuki.gateway.provider import (
    GatewayConnectionProfile,
    GatewayProvider,
    GatewayProviderCatalog,
)
from yuki.gateway.registry import (
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
