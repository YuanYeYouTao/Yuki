"""Formal Gateway Provider boundary and built-in NapCat implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from qq_ai_bot.gateway.provider import GatewayConnectionProfile, GatewayProviderCatalog
from qq_ai_bot.gateway.providers import builtin_provider_catalog
from qq_ai_bot.gateway.providers.napcat import (
    NAPCAT_CAPABILITIES,
    NAPCAT_PROVIDER_ID,
    NapCatProvider,
    napcat_provider_catalog,
)
from qq_ai_bot.gateway.providers.snowluma import (
    SNOWLUMA_CAPABILITIES,
    SNOWLUMA_PROVIDER_ID,
    SnowLumaProvider,
)
from qq_ai_bot.gateway.registry import GatewayConnectionConflict, GatewayConnectionRegistry


@dataclass
class _Bot:
    self_id: str


@dataclass(frozen=True)
class _Provider:
    provider_id: str
    platform: str = "qq"

    def describe_connection(self, handle: object) -> GatewayConnectionProfile:
        return GatewayConnectionProfile(
            provider_id=self.provider_id,
            platform=self.platform,
            external_account_id=str(getattr(handle, "self_id", "")),
            capabilities=frozenset({"send_private"}),
        )


def test_napcat_is_a_formal_provider_profile() -> None:
    provider = NapCatProvider()
    profile = provider.describe_connection(_Bot("8000"))
    assert provider.provider_id == NAPCAT_PROVIDER_ID
    assert profile == GatewayConnectionProfile(
        provider_id="napcat",
        platform="qq",
        external_account_id="8000",
        capabilities=NAPCAT_CAPABILITIES,
    )


def test_snowluma_is_a_formal_provider_profile() -> None:
    provider = SnowLumaProvider()
    profile = provider.describe_connection(_Bot("8001"))
    assert provider.provider_id == SNOWLUMA_PROVIDER_ID
    assert profile == GatewayConnectionProfile(
        provider_id="snowluma",
        platform="qq",
        external_account_id="8001",
        capabilities=SNOWLUMA_CAPABILITIES,
    )


@pytest.mark.parametrize("handle", [None, _Bot(""), _Bot("   ")])
def test_napcat_rejects_invalid_connection_handles(handle: object | None) -> None:
    with pytest.raises((TypeError, ValueError)):
        NapCatProvider().describe_connection(handle)  # type: ignore[arg-type]


@pytest.mark.parametrize("handle", [None, _Bot(""), _Bot("   ")])
def test_snowluma_rejects_invalid_connection_handles(handle: object | None) -> None:
    with pytest.raises((TypeError, ValueError)):
        SnowLumaProvider().describe_connection(handle)  # type: ignore[arg-type]


def test_provider_catalog_is_explicit_and_fail_closed() -> None:
    catalog = napcat_provider_catalog()
    assert catalog.provider_ids == ("napcat",)
    with pytest.raises(ValueError, match="not registered"):
        catalog.describe_connection(_Bot("8000"), provider_id="snowluma")
    with pytest.raises(ValueError, match="duplicate"):
        GatewayProviderCatalog((NapCatProvider(), NapCatProvider()))
    with pytest.raises(ValueError, match="at least one"):
        GatewayProviderCatalog(())
    builtins = builtin_provider_catalog()
    assert builtins.provider_ids == ("napcat", "snowluma")
    with pytest.raises(ValueError, match="provider_id is required"):
        builtins.describe_connection(_Bot("8000"))


def test_registry_uses_selected_provider_profile_not_constructor_strings() -> None:
    catalog = GatewayProviderCatalog(
        (NapCatProvider(), _Provider("snowluma")),
    )
    registry = GatewayConnectionRegistry(
        providers=catalog,
        gateway_instance_id="gw-provider",
    )
    napcat = _Bot("8000")
    snowluma = _Bot("8001")
    with pytest.raises(ValueError, match="provider_id is required"):
        registry.connect(napcat, presence_id="p-napcat")
    napcat_snapshot = registry.connect(
        napcat,
        provider_id="napcat",
        presence_id="p-napcat",
    )
    snowluma_snapshot = registry.connect(
        snowluma,
        provider_id="snowluma",
        gateway_instance_id="gw-snowluma",
        presence_id="p-snowluma",
    )
    assert napcat_snapshot.provider == "napcat"
    assert napcat_snapshot.platform == "qq"
    assert napcat_snapshot.capabilities == NAPCAT_CAPABILITIES
    assert snowluma_snapshot.provider == "snowluma"
    assert snowluma_snapshot.gateway_instance_id == "gw-snowluma"
    assert snowluma_snapshot.capabilities == frozenset({"send_private"})


def test_reconnect_cannot_change_the_handle_provider_identity() -> None:
    registry = GatewayConnectionRegistry(
        providers=GatewayProviderCatalog(
            (NapCatProvider(), _Provider("snowluma")),
        ),
        gateway_instance_id="gw-provider",
    )
    handle = _Bot("8000")
    registry.connect(handle, provider_id="napcat")
    with pytest.raises(ValueError, match="identity changed"):
        registry.connect(handle, provider_id="snowluma")


def test_same_account_cannot_connect_twice_across_providers() -> None:
    registry = GatewayConnectionRegistry(
        providers=builtin_provider_catalog(),
        gateway_instance_id="gw-provider",
    )
    napcat = _Bot("8000")
    snowluma = _Bot("8000")
    first = registry.connect(napcat, provider_id="napcat", presence_id="presence-yuki")
    with pytest.raises(GatewayConnectionConflict) as conflict:
        registry.connect(snowluma, provider_id="snowluma", presence_id="presence-yuki")
    assert conflict.value.category == "provider_conflict"
    assert registry.resolve_active("presence-yuki").bot is napcat
    registry.disconnect(napcat)
    replacement = registry.connect(
        snowluma,
        provider_id="snowluma",
        presence_id="presence-yuki",
    )
    assert replacement.provider == "snowluma"
    assert replacement.generation == first.generation + 1


def test_different_accounts_can_use_different_providers_together() -> None:
    registry = GatewayConnectionRegistry(
        providers=builtin_provider_catalog(),
        gateway_instance_id="gw-provider",
    )
    napcat = _Bot("8000")
    snowluma = _Bot("8001")
    registry.connect(napcat, provider_id="napcat", presence_id="presence-a")
    registry.connect(snowluma, provider_id="snowluma", presence_id="presence-b")
    assert registry.resolve_active("presence-a").bot is napcat
    assert registry.resolve_active("presence-b").bot is snowluma


def test_provider_neutral_registry_does_not_import_or_default_napcat() -> None:
    root = Path(__file__).resolve().parents[2]
    registry_source = (root / "src" / "qq_ai_bot" / "gateway" / "registry.py").read_text(
        encoding="utf-8"
    )
    models_source = (root / "src" / "qq_ai_bot" / "gateway" / "models.py").read_text(
        encoding="utf-8"
    )
    assert "napcat" not in registry_source.casefold()
    assert "napcat" not in models_source.casefold()
