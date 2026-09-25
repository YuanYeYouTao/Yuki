"""Formal Gateway Provider boundary and built-in NapCat implementation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from qq_ai_bot import cli as administrative_cli
from qq_ai_bot.adapters.onebot.provider_adapter import SnowLumaOneBotAdapter
from qq_ai_bot.gateway.compatibility import CORE_ONEBOT_ACTIONS, provider_doctor_payload
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
from qq_ai_bot.gateway.registry import (
    GatewayConnectionConflict,
    GatewayConnectionRegistry,
    RegistryClosed,
    configure_process_registry,
)


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


def test_adapter_registry_follows_socket_lifecycle_before_async_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    adapter = object.__new__(SnowLumaOneBotAdapter)
    first = _Bot("8000")
    replacement = _Bot("8000")
    calls: list[str] = []

    def connected(_adapter: object, bot: object) -> None:
        assert registry.resolve_by_handle(bot).bot is bot
        calls.append("connected")

    def disconnected(_adapter: object, bot: object) -> None:
        with pytest.raises(RegistryClosed, match="no_connection"):
            registry.resolve_by_handle(bot)
        calls.append("disconnected")

    monkeypatch.setattr(OneBotV11Adapter, "bot_connect", connected)
    monkeypatch.setattr(OneBotV11Adapter, "bot_disconnect", disconnected)
    configure_process_registry(registry)
    try:
        adapter.bot_connect(first)  # type: ignore[arg-type]
        registry.bind_presence(platform="qq", external_account_id="8000", presence_id="p")
        assert registry.resolve_active("p").bot is first
        adapter.bot_disconnect(first)  # type: ignore[arg-type]
        assert not registry.has_any_active()
        adapter.bot_connect(replacement)  # type: ignore[arg-type]
        assert registry.resolve_active("p").bot is replacement
        assert calls == ["connected", "disconnected", "connected"]
    finally:
        configure_process_registry(None)


def test_adapter_rolls_back_registry_when_nonebot_rejects_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    adapter = object.__new__(SnowLumaOneBotAdapter)
    bot = _Bot("8000")

    def rejected(_adapter: object, _bot: object) -> None:
        raise RuntimeError("nonebot_rejected")

    monkeypatch.setattr(OneBotV11Adapter, "bot_connect", rejected)
    configure_process_registry(registry)
    try:
        with pytest.raises(RuntimeError, match="nonebot_rejected"):
            adapter.bot_connect(bot)  # type: ignore[arg-type]
        assert not registry.has_any_active()
    finally:
        configure_process_registry(None)


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


def test_builtin_provider_doctor_freezes_the_core_onebot_contract() -> None:
    expected = {
        "send_private_msg",
        "send_group_msg",
        "get_group_info",
        "get_group_member_info",
        "get_stranger_info",
        "get_image",
        "get_group_msg_history",
        "get_friend_msg_history",
    }
    assert {item.action for item in CORE_ONEBOT_ACTIONS} == expected
    required_capabilities = {item.provider_capability for item in CORE_ONEBOT_ACTIONS}
    assert required_capabilities <= NAPCAT_CAPABILITIES
    assert required_capabilities <= SNOWLUMA_CAPABILITIES
    napcat = provider_doctor_payload("napcat")
    snowluma = provider_doctor_payload("snowluma")
    assert {item["action"] for item in napcat["core_actions"]} == expected
    assert napcat["core_actions"] == snowluma["core_actions"]
    assert snowluma["reverse_ws_paths"] == ["/onebot/v11/snowluma/ws"]
    assert snowluma["live_probe"] == "not_run"
    assert snowluma["provider_private_actions"] == "not_guaranteed"
    serialized = json.dumps(snowluma).casefold()
    assert all(secret not in serialized for secret in ("access_token", "cookie", "qq_number"))


def test_gateway_doctor_does_not_require_runtime_settings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _settings_forbidden() -> None:
        raise AssertionError("gateway doctor must not load application settings")

    monkeypatch.setattr(administrative_cli, "Settings", _settings_forbidden)
    monkeypatch.setattr(
        "sys.argv",
        ["qq-ai-bot-cli", "gateway", "doctor", "--provider", "snowluma"],
    )

    administrative_cli.main()

    assert json.loads(capsys.readouterr().out)["provider_id"] == "snowluma"
