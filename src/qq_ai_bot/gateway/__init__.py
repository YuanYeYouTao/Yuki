"""In-memory NapCat connection registry. Handles never persist."""

from qq_ai_bot.gateway.registry import (
    ConnectionResolution,
    GatewayConnectionRegistry,
    RegistryClosed,
)

__all__ = [
    "ConnectionResolution",
    "GatewayConnectionRegistry",
    "RegistryClosed",
]
