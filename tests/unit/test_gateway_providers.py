"""Formal Gateway Provider boundary and built-in implementation."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from qq_ai_bot import cli as administrative_cli
from qq_ai_bot.adapters.onebot.provider_adapter import SnowLumaOneBotAdapter
from qq_ai_bot.gateway.compatibility import CORE_ONEBOT_ACTIONS, provider_doctor_payload
from qq_ai_bot.gateway.provider import GatewayConnectionProfile, GatewayProviderCatalog
from qq_ai_bot.gateway.providers import builtin_provider_catalog
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
def test_snowluma_rejects_invalid_connection_handles(handle: object | None) -> None:
    with pytest.raises((TypeError, ValueError)):
        SnowLumaProvider().describe_connection(handle)  # type: ignore[arg-type]


def test_provider_catalog_is_explicit_and_fail_closed() -> None:
    catalog = builtin_provider_catalog()
    with pytest.raises(ValueError, match="not registered"):
        catalog.describe_connection(_Bot("8000"), provider_id="unknown")
    with pytest.raises(ValueError, match="duplicate"):
        GatewayProviderCatalog((SnowLumaProvider(), SnowLumaProvider()))
    with pytest.raises(ValueError, match="at least one"):
        GatewayProviderCatalog(())
    builtins = builtin_provider_catalog()
    assert builtins.describe_connection(_Bot("8000")) == SnowLumaProvider().describe_connection(
        _Bot("8000")
    )


def test_registry_uses_selected_provider_profile_not_constructor_strings() -> None:
    catalog = GatewayProviderCatalog(
        (SnowLumaProvider(), _Provider("custom")),
    )
    registry = GatewayConnectionRegistry(
        providers=catalog,
        gateway_instance_id="gw-provider",
    )
    builtin = _Bot("8000")
    custom = _Bot("8001")
    with pytest.raises(ValueError, match="provider_id is required"):
        registry.connect(builtin, presence_id="p-builtin")
    builtin_snapshot = registry.connect(
        builtin,
        provider_id="snowluma",
        presence_id="p-builtin",
    )
    custom_snapshot = registry.connect(
        custom,
        provider_id="custom",
        gateway_instance_id="gw-custom",
        presence_id="p-custom",
    )
    assert builtin_snapshot.provider == "snowluma"
    assert builtin_snapshot.platform == "qq"
    assert builtin_snapshot.capabilities == SNOWLUMA_CAPABILITIES
    assert custom_snapshot.provider == "custom"
    assert custom_snapshot.gateway_instance_id == "gw-custom"
    assert custom_snapshot.capabilities == frozenset({"send_private"})


def test_reconnect_cannot_change_the_handle_provider_identity() -> None:
    registry = GatewayConnectionRegistry(
        providers=GatewayProviderCatalog(
            (SnowLumaProvider(), _Provider("custom")),
        ),
        gateway_instance_id="gw-provider",
    )
    handle = _Bot("8000")
    registry.connect(handle, provider_id="snowluma")
    with pytest.raises(ValueError, match="identity changed"):
        registry.connect(handle, provider_id="custom")


def test_same_account_cannot_connect_twice_across_providers() -> None:
    registry = GatewayConnectionRegistry(
        providers=GatewayProviderCatalog((SnowLumaProvider(), _Provider("custom"))),
        gateway_instance_id="gw-provider",
    )
    builtin = _Bot("8000")
    custom = _Bot("8000")
    first = registry.connect(builtin, provider_id="snowluma", presence_id="presence-yuki")
    with pytest.raises(GatewayConnectionConflict) as conflict:
        registry.connect(custom, provider_id="custom", presence_id="presence-yuki")
    assert conflict.value.category == "provider_conflict"
    assert registry.resolve_active("presence-yuki").bot is builtin
    registry.disconnect(builtin)
    replacement = registry.connect(
        custom,
        provider_id="custom",
        presence_id="presence-yuki",
    )
    assert replacement.provider == "custom"
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


def test_snowluma_registers_dedicated_websocket_routes_without_legacy_endpoints() -> None:
    from nonebot.config import Config, Env
    from nonebot.drivers.fastapi import Driver
    from starlette.routing import Route, WebSocketRoute

    driver = Driver(Env(_env_file=None), Config(_env_file=None))
    adapter = object.__new__(SnowLumaOneBotAdapter)
    adapter.driver = driver
    adapter._setup()
    websocket_paths = {
        route.path for route in driver.server_app.routes if isinstance(route, WebSocketRoute)
    }
    http_paths = {route.path for route in driver.server_app.routes if isinstance(route, Route)}
    assert "/onebot/v11/snowluma/ws" in websocket_paths
    assert "/onebot/v11/snowluma/ws/" in websocket_paths
    legacy_paths = {
        "/onebot/v11/",
        "/onebot/v11/http",
        "/onebot/v11/http/",
        "/onebot/v11/ws",
        "/onebot/v11/ws/",
    }
    assert not legacy_paths & websocket_paths
    assert not legacy_paths & http_paths


def test_different_accounts_can_use_different_providers_together() -> None:
    registry = GatewayConnectionRegistry(
        providers=GatewayProviderCatalog((SnowLumaProvider(), _Provider("custom"))),
        gateway_instance_id="gw-provider",
    )
    builtin = _Bot("8000")
    custom = _Bot("8001")
    registry.connect(builtin, provider_id="snowluma", presence_id="presence-a")
    registry.connect(custom, provider_id="custom", presence_id="presence-b")
    assert registry.resolve_active("presence-a").bot is builtin
    assert registry.resolve_active("presence-b").bot is custom


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
    assert required_capabilities <= SNOWLUMA_CAPABILITIES
    snowluma = provider_doctor_payload("snowluma")
    assert {item["action"] for item in snowluma["core_actions"]} == expected
    assert snowluma["reverse_ws_paths"] == ["/onebot/v11/snowluma/ws"]
    assert snowluma["live_probe"] == "not_run"
    assert snowluma["provider_private_actions"] == "not_guaranteed"
    serialized = json.dumps(snowluma).casefold()
    assert all(secret not in serialized for secret in ("access_token", "cookie", "qq_number"))


@pytest.mark.asyncio
async def test_social_operations_use_custom_provider_connection_without_brand_dispatch() -> None:
    from unittest.mock import AsyncMock

    from qq_ai_bot.identity.routing import ResolvedSend
    from qq_ai_bot.social.service import SocialService

    @dataclass
    class ApiBot(_Bot):
        call_api: AsyncMock

    bot = ApiBot("8000", AsyncMock(return_value={"message_id": "accepted"}))
    registry = GatewayConnectionRegistry(providers=GatewayProviderCatalog((_Provider("custom"),)))
    registry.connect(bot, presence_id="presence-yuki")
    route = ResolvedSend(
        presence_id="presence-yuki",
        binding_id="binding-peer",
        platform="qq",
        external_target_id="1001",
        route_generation=1,
        connection=registry.resolve_active("presence-yuki"),
        kind="person",
        sender_account_id="8000",
    )
    result = await SocialService._call(
        route, "send_private_msg", {"user_id": 1001, "message": "hello"}
    )
    assert result == {"message_id": "accepted"}
    bot.call_api.assert_awaited_with("send_private_msg", user_id=1001, message="hello")
    bot.call_api.reset_mock()
    with pytest.raises(ValueError, match="capability_unavailable"):
        await SocialService._call(route, "provider_private_action", {})
    bot.call_api.assert_not_awaited()


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
