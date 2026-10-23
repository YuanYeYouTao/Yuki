"""A live personal entry does not claim that a social platform is connected."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.unit.test_gateway_providers import _Bot, _Provider
from tests.unit.test_group_route_recovery import stack
from tests.unit.test_telegram_health import health_container

from yuki.application.modules.admin_api import runtime_health
from yuki.gateway.provider import GatewayProviderCatalog
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.health import build_health_payload
from yuki.identity.canonical_repository import create_presence


async def test_personal_gateway_health_keeps_qq_telegram_and_environment_facts_separate(database):
    app, bot, *_ = await stack(database)
    try:
        await app.app_gateway.start()
        app.gateway_registry.disconnect(bot)
        rows = {row["name"]: row for row in await runtime_health(app)}
        assert rows["app"]["running"]
        assert not rows["qq"]["running"]
        assert rows["qq"]["healthy"] is False
        assert not rows["telegram"]["enabled"]
        assert not rows["telegram"]["running"]
        assert rows["persistent_environment"]["healthy"] is None
        assert rows["persistent_environment"]["running"] is None
        assert "completion_receiver_running" in rows["persistent_environment"]
        assert all(row["checked_at"] is not None for row in rows.values())
    finally:
        await app.close()


async def test_platform_extension_and_writer_state_have_both_projection_shapes(database):
    app = health_container(database, None)
    app.work_scheduler.running = False
    app.plugin_manager = SimpleNamespace(system_enabled=False, running_count=0)
    app.subagent_scheduler.task = None
    app.sandbox_completions.health.return_value = {
        "running": False,
        "last_error_category": None,
    }
    app.runtime_config = SimpleNamespace(
        snapshot=AsyncMock(
            return_value=SimpleNamespace(memory=SimpleNamespace(maintenance_enabled=False))
        )
    )
    app.semantic_participation = SimpleNamespace(health=AsyncMock(return_value={}))
    app.gateway_providers = GatewayProviderCatalog(
        _Provider(platform, platform=platform)
        for platform in ("qq", "telegram", "feishu", "lark", "matrix", "unknown-owner")
    )
    app.gateway_registry = GatewayConnectionRegistry(providers=app.gateway_providers)
    async with database.immediate_session() as session:
        presence = await create_presence(
            session, external_account_id="matrix-owner", platform="matrix", now=datetime.now(UTC)
        )
    bot = _Bot("matrix-owner")
    app.gateway_registry.connect(bot, provider_id="matrix", presence_id=presence.id)
    owner = {"enabled": True, "running": True, "healthy": None}
    app.gateway_health_sources = {
        "feishu": AsyncMock(return_value={"enabled": True, "running": True, "healthy": True}),
        "lark": AsyncMock(return_value={"enabled": True, "running": False, "healthy": False}),
        "unknown-owner": AsyncMock(return_value=owner),
    }
    public = await build_health_payload(app)
    operator = {row["name"]: row for row in await runtime_health(app)}
    for platform in app.gateway_providers.platforms:
        assert public["gateways"][platform] == {
            key: operator[platform][key] for key in ("enabled", "running", "healthy")
        }
    assert public["gateways"]["matrix"] == {
        "enabled": True,
        "running": True,
        "healthy": True,
    }
    assert public["gateways"]["unknown-owner"]["healthy"] is None
    assert public["telegram_gateway"] == public["gateways"]["telegram"]
    assert not public["onebot_connected"] and public["status"] == "degraded"
    app.gateway_registry.disconnect(bot)
    after = await build_health_payload(app)
    assert not after["gateways"]["matrix"]["running"]
    assert after["gateways"]["matrix"]["healthy"] is False
    await database.close()
    public = await build_health_payload(app)
    operator = {row["name"]: row for row in await runtime_health(app)}
    assert public["status"] == "degraded"
    assert public["database"] == "unavailable"
    assert public["database_read"] == "ok"
    assert public["database_writer"] == "stopped"
    assert operator["database"]["read_available"]
    assert not operator["database"]["writer_accepting"]
    assert operator["database"]["healthy"] is False
