"""Explicit built-in gateway Provider wiring for tests."""

from __future__ import annotations

from qq_ai_bot.gateway.providers import builtin_provider_catalog
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry


def builtin_registry(*, gateway_instance_id: str | None = None) -> GatewayConnectionRegistry:
    return GatewayConnectionRegistry(
        providers=builtin_provider_catalog(),
        gateway_instance_id=gateway_instance_id,
    )
