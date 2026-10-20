"""Transient unlink failure retains a route to finish deleting plugin temporaries."""

import asyncio
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select
from tests.support.social_identity_cases import social_env

from yuki.plugin_host.db_models import PluginMediaArtifactModel, PluginNotificationOutboxModel
from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
from yuki.plugin_host.repository import PluginInstallationRepository
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.errors import PluginPermissionError


@pytest.fixture
async def plugin_media(database, tmp_path):
    plugin = "test.cleanup-recovery"
    await PluginInstallationRepository(database).upsert_discovered(
        plugin_id=plugin,
        name="Cleanup",
        version="1",
        plugin_api=PLUGIN_API_VERSION,
        yuki_requires=">=3",
        manifest_hash="c" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=(),
    )
    content = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(content, "PNG")
    store = PluginMediaArtifactStore(database, tmp_path / "plugin-media")
    handle = await store.create(
        plugin_id=plugin,
        data=content.getvalue(),
        content_type="image/png",
        filename="original.png",
        ttl_seconds=60,
        storage_mb=1,
    )
    artifact = await store.resolve(plugin_id=plugin, handle_id=handle.handle_id)
    return SimpleNamespace(
        plugin=plugin,
        store=store,
        handle=handle,
        path=artifact.local_path,
        content=content.getvalue(),
    )


async def expire(database, handle_id):
    async with database.immediate_session() as session:
        row = await session.get(PluginMediaArtifactModel, handle_id)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)


async def test_next_cleanup_finishes_original_file_after_unlink_failure(
    database, plugin_media, monkeypatch
):
    env = plugin_media
    await expire(database, env.handle.handle_id)
    unlink = Path.unlink
    failed = False

    def fail_once(path, *args, **kwargs):
        nonlocal failed
        if path == env.path and not failed:
            failed = True
            raise OSError("temporary file is busy")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    assert await env.store.cleanup() == 0
    assert failed
    await env.store.cleanup()
    assert not env.path.exists()
    async with database.sessions() as session:
        assert await session.get(PluginMediaArtifactModel, env.handle.handle_id) is None


async def test_expired_unreferenced_media_is_removed(database, plugin_media):
    env = plugin_media
    await expire(database, env.handle.handle_id)
    assert await env.store.cleanup() == 1
    assert not env.path.exists()
    async with database.sessions() as session:
        assert await session.get(PluginMediaArtifactModel, env.handle.handle_id) is None


async def test_cancelled_cleanup_keeps_the_original_path_for_the_next_pass(
    database, plugin_media, monkeypatch
):
    env = plugin_media
    await expire(database, env.handle.handle_id)
    unlink = Path.unlink
    started, finished = asyncio.Event(), asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()

    def held_unlink(path, *args, **kwargs):
        if path != env.path:
            return unlink(path, *args, **kwargs)
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5), "test must release its filesystem barrier"
            return unlink(path, *args, **kwargs)
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(Path, "unlink", held_unlink)
    cleaning = asyncio.create_task(env.store.cleanup())
    try:
        async with asyncio.timeout(5):
            await started.wait()
        cleaning.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleaning
        async with database.sessions() as reader:
            row = await reader.get(PluginMediaArtifactModel, env.handle.handle_id)
            assert row is not None and Path(row.storage_path) == env.path
    finally:
        release.set()
        async with asyncio.timeout(5):
            await finished.wait()
        await asyncio.gather(cleaning, return_exceptions=True)
    assert await env.store.cleanup() == 1
    assert not env.path.exists()
    async with database.sessions() as reader:
        assert await reader.get(PluginMediaArtifactModel, env.handle.handle_id) is None


@pytest.mark.parametrize("status", ["pending", "processing"])
async def test_pending_original_outbox_preserves_expired_media(
    database, tmp_path, plugin_media, status
):
    env = plugin_media
    scene = await social_env(database, tmp_path)
    from yuki.persistence.models import ChatEventModel

    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
        session.add(
            PluginNotificationOutboxModel(
                notification_id="original-notification",
                part_key="original-media",
                source_event_id=event.id,
                plugin_id=env.plugin,
                source_person_id=scene.person,
                target_type="group",
                target_id=scene.space,
                bot_user_id=scene.bot.self_id,
                part_type="media",
                media_handle_id=env.handle.handle_id,
                status=status,
                canonical_target_space_id=scene.space,
                canonical_presence_id=scene.presence,
                canonical_conversation_id=scene.context.conversation_id,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    await expire(database, env.handle.handle_id)
    assert await env.store.cleanup() == 0
    assert env.path.read_bytes() == env.content
    async with database.sessions() as session:
        assert await session.get(PluginMediaArtifactModel, env.handle.handle_id) is not None


async def test_foreign_plugin_cannot_read_original_media(plugin_media):
    env = plugin_media
    with pytest.raises(PluginPermissionError, match="foreign"):
        await env.store.read_owned(plugin_id="another.plugin", handle_id=env.handle.handle_id)
    assert env.path.read_bytes() == env.content
