"""Restart original illustration receipts after their actual plugin dependency starts."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

from tests.conftest import make_settings
from tests.support.model_profiles import write_fake_profiles
from tests.support.social_identity_cases import social_env
from tests.support.sticker_delivery import StickerLibrary, settle
from tests.unit.test_sticker_library import LocalDirectoryBridge

from yuki.container import ApplicationContainer
from yuki.plugin_host.notification_repository import PluginNotificationRepository
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest


async def test_pending_plugin_illustration_resumes_after_real_plugin_start(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path / "social")
    plugin_id = "test.sticker-restart"
    plugin_root = tmp_path / "plugins" / plugin_id
    plugin_root.mkdir(parents=True)
    entered, release = tmp_path / "starting", tmp_path / "release"
    release.touch()
    plugin_root.joinpath("plugin.toml").write_text(
        f'''id = "{plugin_id}"
name = "Restart fixture"
version = "1.0.0"
description = "An inert local dependency"
entrypoint = "fixture:Fixture"
plugin_api = "{PLUGIN_API_VERSION}"
yuki_requires = ">=3.8"
permissions = []
''',
        encoding="utf-8",
    )
    plugin_root.joinpath("fixture.py").write_text(
        f"""import asyncio
from pathlib import Path

class Fixture:
    async def register(self, registrar):
        pass

    async def start(self, context):
        Path({str(entered)!r}).touch()
        while not Path({str(release)!r}).exists():
            await asyncio.sleep(0.01)

    async def stop(self):
        pass
""",
        encoding="utf-8",
    )
    app = ApplicationContainer(
        make_settings(
            database.url,
            model_profiles_file=write_fake_profiles(tmp_path / "models.toml"),
            plugin_system_enabled=True,
            plugin_directory=tmp_path / "plugins",
            workspace_directory=tmp_path / "artifacts",
            conversation_media_cache_directory=tmp_path / "conversation-media",
            social_transfer_directory=tmp_path / "transfer",
            semantic_participation_state_path=tmp_path / "participation.sqlite3",
            code_mode_enabled=False,
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
    bridge = LocalDirectoryBridge(tmp_path / "manager-workspace", app.workspace_service.store)

    async def manager_read_or_file(method, arguments, *, request_id, **_kwargs):
        if method == "list_code_completions":
            return {"events": [], "next_cursor": 0, "has_more": False}
        return await bridge.execute(method, arguments, request_id=request_id)

    monkeypatch.setattr(app.sandbox_client, "execute", manager_read_or_file)
    app.gateway_registry.connect(env.bot, provider_id="snowluma", presence_id=env.presence)
    app.emoji_delivery.library = StickerLibrary(app.workspace_service.store)
    await app.plugin_manager.discover()
    await app.plugin_manager.approve(plugin_id, actor_user_id=env.person)
    await app.plugin_manager.enable(plugin_id, actor_user_id=env.person)
    await app.plugin_manager.start()
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(
        target_type="group", target_id=env.space, conversation_id=env.context.conversation_id
    )
    await notifications.grant_target(
        plugin_id=plugin_id,
        target=target,
        bot_user_id=env.bot.self_id,
        created_by_user_id=env.person,
        presence_id=env.presence,
    )
    source = await notifications.publish(
        plugin_id=plugin_id,
        request=PublishNotificationRequest(
            event_key="original",
            event_type="fixture",
            external_source="fixture",
            target=target,
            occurred_at=datetime.now(UTC),
            summary="original plugin event",
        ),
    )
    context = replace(
        env.context, origin="plugin_background", caused_by_event_id=source.source_event_id
    )
    sent = await app.social_service.execute(
        "send_message", {"text": "正文已经送达", "emoji": {"goal": "开心"}}, context
    )
    assert sent["status"] == "succeeded" and sent["emoji"]["status"] == "accepted"
    await app.plugin_manager.stop()
    assert (await app.plugin_manager.show(plugin_id)).status == "approved"
    entered.unlink()
    release.unlink()
    starting = asyncio.create_task(app.start())
    try:
        async with asyncio.timeout(5):
            while not entered.exists():
                if starting.done():
                    await starting
                await asyncio.sleep(0.01)
        # A real plugin may await I/O while its installation is still starting.
        # Exercise a wrongly started consumer to completion before permitting
        # that dependency to finish, instead of depending on scheduler timing.
        if app.emoji_delivery.running:
            await settle(app.emoji_delivery, context)
        release.touch()
        await starting
        assert (await settle(app.emoji_delivery, context))["status"] == "succeeded"
        assert len([method for method, _ in env.bot.calls if method == "send_group_msg"]) == 2
    finally:
        release.touch()
        await asyncio.gather(starting, return_exceptions=True)
        await app.close()
