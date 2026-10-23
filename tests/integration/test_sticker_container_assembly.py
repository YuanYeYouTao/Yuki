"""The actual application lifecycle freezes one contract and owns both sticker tasks."""

import asyncio

import pytest
from tests.conftest import make_settings
from tests.support.model_profiles import write_fake_profiles
from tests.unit.test_sticker_library import LocalDirectoryBridge

from yuki.container import ApplicationContainer
from yuki.health import build_health_payload


@pytest.mark.parametrize("code_enabled", [False, True])
async def test_offline_container_starts_freezes_tools_and_closes_sticker_resources(
    database, tmp_path, monkeypatch, code_enabled
):
    app = ApplicationContainer(
        make_settings(
            database.url,
            model_profiles_file=write_fake_profiles(tmp_path / "models.toml"),
            plugin_system_enabled=False,
            plugin_directory=tmp_path / "plugins",
            workspace_directory=tmp_path / "artifacts",
            conversation_media_cache_directory=tmp_path / "conversation-media",
            social_transfer_directory=tmp_path / "transfer",
            semantic_participation_state_path=tmp_path / "participation.sqlite3",
            code_mode_enabled=code_enabled,
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
    await app.character.initialize("Offline fixture")
    bridge = LocalDirectoryBridge(
        tmp_path / "manager-workspace",
        app.workspace_service.store,
        cache_root=app.conversation_media.root.parent,
    )

    async def manager_read_or_file(method, arguments, *, request_id, **_kwargs):
        if method == "list_code_completions":
            return {"events": [], "next_cursor": 0, "has_more": False}
        return await bridge.execute(method, arguments, request_id=request_id)

    monkeypatch.setattr(app.sandbox_client, "execute", manager_read_or_file)
    try:
        await app.start()
        assert app.emoji_delivery.running
        delivery_task = app.emoji_delivery._task
        platform = await app.main_agent_contract.definitions()
        names = {tool.name for tool in platform}
        assert {"send", "query_chat_history"} <= names
        assert ("execute_code" in names) == code_enabled
        assert "send" in {tool.name for tool in await app.main_agent_contract.model_definitions()}
        payload = await build_health_payload(app)
        assert payload["main_agent_manifest"]["frozen"]
    finally:
        await asyncio.wait_for(app.close(), 5)
    assert not app.emoji_delivery.running
    assert delivery_task.done() and delivery_task.cancelled()
