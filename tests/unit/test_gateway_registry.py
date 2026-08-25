"""C16 GatewayConnectionRegistry: memory-only, fail-closed, generation isolation."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from tests.support.gateway import napcat_registry

from qq_ai_bot.gateway.models import ConnectionHealth
from qq_ai_bot.gateway.registry import (
    GatewayConnectionConflict,
    RegistryClosed,
    configure_process_registry,
    process_registry,
)


@dataclass
class _Bot:
    self_id: str


def test_registry_zero_one_and_coexisting_presences() -> None:
    registry = napcat_registry(gateway_instance_id="gw-1")
    first = _Bot("8000")
    second = _Bot("8001")
    registry.connect(first, presence_id="p-a")
    registry.connect(second, presence_id="p-b")
    resolved_a = registry.resolve_active("p-a")
    resolved_b = registry.resolve_active("p-b")
    assert resolved_a.bot is first
    assert resolved_b.bot is second
    assert resolved_a.snapshot.generation == 1
    with pytest.raises(RegistryClosed) as missing:
        registry.resolve_active("p-missing")
    assert missing.value.category == "disconnected"
    exclusive = napcat_registry(gateway_instance_id="gw-2")
    left = _Bot("8000")
    right = _Bot("8000")
    exclusive.connect(left)
    with pytest.raises(GatewayConnectionConflict) as conflict:
        exclusive.connect(right)
    assert conflict.value.category == "provider_conflict"


def test_reconnect_same_handle_only_increments_connection_generation() -> None:
    registry = napcat_registry(gateway_instance_id="gw-1")
    bot = _Bot("8000")
    first = registry.connect(bot, presence_id="p-a")
    second = registry.connect(bot, presence_id="p-a")
    assert first.connection_id == second.connection_id
    assert second.generation == first.generation + 1
    assert registry.resolve_active("p-a").snapshot.generation == second.generation


def test_disconnect_then_new_handle_is_new_connection() -> None:
    registry = napcat_registry(gateway_instance_id="gw-1")
    bot = _Bot("8000")
    first = registry.connect(bot, presence_id="p-a")
    registry.disconnect(bot)
    replacement = _Bot("8000")
    second = registry.connect(replacement, presence_id="p-a")
    assert second.connection_id != first.connection_id
    assert second.generation == first.generation + 1
    assert registry.resolve_active("p-a").bot is replacement


def test_resolve_by_handle_never_picks_another_presence() -> None:
    registry = napcat_registry(gateway_instance_id="gw-1")
    alpha = _Bot("8000")
    beta = _Bot("8001")
    registry.connect(alpha, presence_id="p-a")
    registry.connect(beta, presence_id="p-b")
    assert registry.resolve_by_handle(alpha).snapshot.presence_id == "p-a"
    assert registry.resolve_by_handle(beta).snapshot.presence_id == "p-b"
    with pytest.raises(RegistryClosed):
        registry.resolve_by_handle(_Bot("8002"))


def test_duplicate_account_keeps_incumbent_and_snapshot_matches() -> None:
    registry = napcat_registry(gateway_instance_id="gw-exclusive")
    first = _Bot("8000")
    extra = _Bot("8000")
    registry.connect(first, presence_id="p-a")
    incumbent = registry.resolve_active("p-a")
    with pytest.raises(GatewayConnectionConflict):
        registry.connect(extra, presence_id="p-a")
    still = registry.resolve_active("p-a")
    assert still.bot is first
    assert still.snapshot.connection_id == incumbent.snapshot.connection_id
    snap = registry.snapshot_presence(presence_id="p-a", platform="qq", external_account_id="8000")
    assert snap.health is ConnectionHealth.CONNECTED
    assert snap.live_count == 1
    assert snap.connection_id == incumbent.snapshot.connection_id


def test_one_presence_cannot_bind_two_accounts() -> None:
    registry = napcat_registry(gateway_instance_id="gw-corrupt-binding")
    left = _Bot("8000")
    right = _Bot("8001")
    registry.connect(left, presence_id="p-a")
    with pytest.raises(GatewayConnectionConflict):
        registry.connect(right, presence_id="p-a")
    assert registry.resolve_active("p-a").bot is left


def test_disconnect_allows_replacement_and_preserves_presence_binding() -> None:
    registry = napcat_registry(gateway_instance_id="gw-replace")
    first = _Bot("8000")
    extra = _Bot("8000")
    registry.connect(first, presence_id="p-a")
    registry.disconnect(first)
    registry.connect(extra)
    rebound = registry.resolve_active("p-a")
    assert rebound.bot is extra
    snap = registry.snapshot_presence(presence_id="p-a", platform="qq", external_account_id="8000")
    assert snap.health is ConnectionHealth.CONNECTED
    assert snap.live_count == 1
    assert snap.connection_id == rebound.snapshot.connection_id


def test_snapshot_health_and_process_registry() -> None:
    registry = napcat_registry(gateway_instance_id="gw-1")
    configure_process_registry(registry)
    try:
        assert process_registry() is registry
        empty = registry.snapshot_presence(
            presence_id="p-a", platform="qq", external_account_id="8000"
        )
        assert empty.health is ConnectionHealth.DISCONNECTED
        registry.connect(_Bot("8000"), presence_id="p-a")
        live = registry.snapshot_presence(
            presence_id="p-a", platform="qq", external_account_id="8000"
        )
        assert live.health is ConnectionHealth.CONNECTED
        assert live.generation == 1
        left = _Bot("8000")
        clash_registry = napcat_registry(gateway_instance_id="gw-clash")
        clash_registry.connect(left, presence_id="p-a")
        with pytest.raises(GatewayConnectionConflict):
            clash_registry.connect(_Bot("8001"), presence_id="p-a")
        clash = clash_registry.snapshot_presence(
            presence_id="p-a", platform="qq", external_account_id="8000"
        )
        assert clash.health is ConnectionHealth.CONNECTED
        assert clash.live_count == 1
    finally:
        configure_process_registry(None)
