"""The configured native gateway belongs to the existing application lifecycle."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from tests.conftest import make_settings
from tests.support.model_profiles import write_fake_profiles
from tests.unit.test_sticker_library import LocalDirectoryBridge

from yuki.container import ApplicationContainer


@pytest.mark.parametrize("domain", [None, "https://open.feishu.cn", "https://open.larksuite.com"])
async def test_optional_feishu_gateway_is_started_and_closed_by_container(
    database, tmp_path, monkeypatch, domain
):
    from yuki.gateway.providers.feishu_ingress import FeishuGateway

    start = AsyncMock()
    health = AsyncMock(return_value={"enabled": True, "running": True})
    monkeypatch.setattr(FeishuGateway, "start", start)
    monkeypatch.setattr(FeishuGateway, "health", health)
    close = AsyncMock()
    monkeypatch.setattr(FeishuGateway, "close", close)
    app = ApplicationContainer(
        make_settings(
            database.url,
            feishu_app_id="cli_offline" if domain else "",
            feishu_app_secret="offline-only-secret" if domain else "",
            feishu_domain=domain or "https://open.feishu.cn",
            telegram_bot_token="",
            model_profiles_file=write_fake_profiles(tmp_path / "models.toml"),
            plugin_system_enabled=False,
            plugin_directory=tmp_path / "plugins",
            workspace_directory=tmp_path / "artifacts",
            conversation_media_cache_directory=tmp_path / "conversation-media",
            social_transfer_directory=tmp_path / "transfer",
            semantic_participation_state_path=tmp_path / "participation.sqlite3",
            memory_embedding_enabled=False,
            memory_embedding_worker_enabled=False,
            memory_self_reflection_enabled=False,
            memory_dream_enabled=False,
            memory_maintenance_enabled=False,
            conversation_proactive_wakeup_enabled=False,
            conversation_autonomous_enabled=False,
        ),
        database=database,
    )
    await app.character.initialize("Feishu fixture")
    bridge = LocalDirectoryBridge(tmp_path / "manager-workspace", app.workspace_service.store)

    async def manager_read_or_file(method, arguments, *, request_id, **_kwargs):
        if method == "list_code_completions":
            return {"events": [], "next_cursor": 0, "has_more": False}
        return await bridge.execute(method, arguments, request_id=request_id)

    monkeypatch.setattr(app.sandbox_client, "execute", manager_read_or_file)
    try:
        await app.start()
        if domain is None:
            assert app.feishu_gateway is None
            assert "feishu_gateway" not in app.lifecycle.names
            start.assert_not_awaited()
        else:
            assert app.feishu_gateway is not None
            assert app.feishu_gateway.client.domain == domain
            assert app.feishu_gateway.client.platform == (
                "lark" if domain == "https://open.larksuite.com" else "feishu"
            )
            start.assert_awaited_once()
            assert (await app.lifecycle.health())["feishu_gateway"]["running"]
    finally:
        await asyncio.wait_for(app.close(), 5)
        if app.feishu_gateway is not None:
            await app.feishu_gateway.client.close()
    if domain is None:
        close.assert_not_awaited()
    else:
        close.assert_awaited_once()
