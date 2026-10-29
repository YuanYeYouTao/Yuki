"""Control read views project live registered owners and consume their cursors."""

from datetime import UTC, datetime

import pytest
from tests.unit.test_group_route_recovery import stack

from yuki.application.modules.admin_api import runtime_health
from yuki.domain.conversations import ConversationScope
from yuki.gateway.provider import GatewayProviderCatalog
from yuki.gateway.providers.app import AppHandle, AppProvider
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.canonical_repository import create_presence
from yuki.model_runtime.models import ModelTask


async def test_status_reports_current_platform_and_selected_profile(database):
    app, *_ = await stack(database)
    try:
        catalog = GatewayProviderCatalog((AppProvider("matrix"),))
        app.gateway_providers = catalog
        app.gateway_registry = GatewayConnectionRegistry(providers=catalog)
        async with database.immediate_session() as session:
            presence = await create_presence(
                session, external_account_id="yuki-matrix", now=datetime.now(UTC), platform="matrix"
            )
        app.gateway_registry.connect(
            AppHandle("matrix"), provider_id="matrix", presence_id=presence.id
        )
        app.settings.llm_model = "retired-main-model"
        scope = ConversationScope.private(
            "yuki-matrix", "reader", platform="matrix", presence_id=presence.id
        )
        text = await app.command_service._conversation.status(scope, scope.key)
        assert "matrix 连接：已连接" in text
        assert f"模型：{app.models.model_name(ModelTask.CHAT_AGENT)}" in text
        assert "retired-main-model" not in text and "OneBot" not in text
        other = ConversationScope.private("other-matrix-account", "reader", platform="matrix")
        assert "matrix 连接：未连接" in await app.command_service._conversation.status(
            other, other.key
        )
    finally:
        await app.close()


async def test_health_includes_new_registered_platform_without_platform_table(database):
    app, *_ = await stack(database)
    try:
        catalog = GatewayProviderCatalog((AppProvider("matrix"),))
        app.gateway_providers = catalog
        app.gateway_registry = GatewayConnectionRegistry(providers=catalog)
        async with database.immediate_session() as session:
            presence = await create_presence(
                session, external_account_id="yuki-matrix", now=datetime.now(UTC), platform="matrix"
            )
        app.gateway_registry.connect(
            AppHandle("matrix"), provider_id="matrix", presence_id=presence.id
        )
        rows = {row["name"]: row for row in await runtime_health(app)}
        assert rows["matrix"]["enabled"] is True
        assert rows["matrix"]["running"] is True
        assert rows["matrix"]["healthy"] is True
        app.gateway_registry.disconnect(app.gateway_registry.resolve_active(presence.id).bot)
        offline = {row["name"]: row for row in await runtime_health(app)}
        assert offline["matrix"]["enabled"] is True
        assert offline["matrix"]["running"] is False
    finally:
        await app.close()


@pytest.mark.parametrize("platform", ["app", "acp"])
async def test_paused_local_entry_registers_physical_connection_before_enable(database, platform):
    from yuki.identity.canonical_repository import find_presence

    app, *_ = await stack(database)
    gateway = app.app_gateway if platform == "app" else app.acp_gateway
    try:
        async with database.immediate_session() as writer:
            presence = await find_presence(writer, gateway.handle.self_id, platform=platform)
            presence.status = "disabled"
            identity = presence.id
        await gateway.start()
        resolution = app.gateway_registry.resolve_active(identity)
        assert resolution.bot is gateway.handle
        async with database.immediate_session() as writer:
            presence = await writer.get(type(presence), identity)
            presence.status = "active"
        assert (
            app.gateway_registry.resolve_active(identity).snapshot.connection_id
            == resolution.snapshot.connection_id
        )
    finally:
        await gateway.close()
        await app.close()
