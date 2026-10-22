"""Transient unlink failure retains a route to finish deleting plugin temporaries."""

import asyncio
import io
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import insert, select, text
from sqlalchemy.exc import OperationalError
from tests.support.social_identity_cases import social_env

from yuki.plugin_host.db_models import PluginMediaArtifactModel, PluginNotificationOutboxModel
from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
from yuki.plugin_host.repository import PluginInstallationRepository
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.errors import PluginPermissionError


@pytest.mark.parametrize("commit_first", [False, True])
async def test_create_reconciles_original_handle_without_unlinking_unknown_commit(
    database, plugin_media, monkeypatch, commit_first
):
    env = plugin_media
    original_writer = database.immediate_session
    failure = OperationalError("lost acknowledgement", {}, RuntimeError("lost acknowledgement"))

    @asynccontextmanager
    async def lose_acknowledgement():
        async with original_writer() as writer:
            commit = writer.commit

            async def commit_with_unknown_ack():
                if commit_first:
                    await commit()
                raise failure

            monkeypatch.setattr(writer, "commit", commit_with_unknown_ack)
            yield writer

    monkeypatch.setattr(database, "immediate_session", lose_acknowledgement)
    if commit_first:
        handle = await env.store.create(
            plugin_id=env.plugin,
            data=env.content,
            content_type="image/png",
            filename="unknown.png",
            ttl_seconds=60,
            storage_mb=1,
        )
        resolved = await env.store.resolve(plugin_id=env.plugin, handle_id=handle.handle_id)
        assert resolved.local_path.read_bytes() == env.content
    else:
        with pytest.raises(OperationalError) as caught:
            await env.store.create(
                plugin_id=env.plugin,
                data=env.content,
                content_type="image/png",
                filename="unknown.png",
                ttl_seconds=60,
                storage_mb=1,
            )
        assert caught.value is failure
    paths = tuple(env.path.parent.glob("*-unknown.png"))
    assert len(paths) == 1 and paths[0].read_bytes() == env.content
    async with database.read_session() as reader:
        registered = await reader.get(PluginMediaArtifactModel, paths[0].name.split("-", 1)[0])
    assert (registered is not None) is commit_first


async def test_create_unlinks_only_confirmed_rollback(database, plugin_media, monkeypatch):
    env = plugin_media
    original_writer = database.immediate_session
    failure = OperationalError("flush failed", {}, RuntimeError("flush failed"))

    @asynccontextmanager
    async def fail_flush():
        async with original_writer() as writer:

            async def flush_failure(*_args, **_kwargs):
                raise failure

            monkeypatch.setattr(writer, "flush", flush_failure)
            yield writer

    monkeypatch.setattr(database, "immediate_session", fail_flush)
    with pytest.raises(OperationalError) as caught:
        await env.store.create(
            plugin_id=env.plugin,
            data=env.content,
            content_type="image/png",
            filename="rollback.png",
            ttl_seconds=60,
            storage_mb=1,
        )
    assert caught.value is failure
    assert not tuple(env.path.parent.glob("*-rollback.png"))


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
    async with database.read_session() as session:
        assert await session.get(PluginMediaArtifactModel, env.handle.handle_id) is None


async def test_expired_unreferenced_media_is_removed(database, plugin_media):
    env = plugin_media
    await expire(database, env.handle.handle_id)
    assert await env.store.cleanup() == 1
    assert not env.path.exists()
    async with database.read_session() as session:
        assert await session.get(PluginMediaArtifactModel, env.handle.handle_id) is None


async def test_unlink_failures_do_not_hide_later_expired_originals(
    database, plugin_media, monkeypatch
):
    env = plugin_media
    table = PluginMediaArtifactModel.__table__
    async with database.read_session() as reader:
        original = dict((await reader.execute(select(table))).mappings().one())
    rows, blocked = [], set()
    for index in range(129):
        path = env.path.parent / f"original-{index}.png"
        path.write_bytes(env.content)
        rows.append(
            {
                **original,
                "handle_id": f"original-{index}",
                "storage_path": str(path),
                "expires_at": datetime.now(UTC) - timedelta(seconds=200 - index),
            }
        )
        if index < 128:
            blocked.add(path)
    async with database.immediate_session() as writer:
        await writer.execute(insert(table), rows)
    unlink = Path.unlink

    def busy(path, *args, **kwargs):
        if path in blocked:
            raise OSError("original file remains busy")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", busy)
    assert await env.store.cleanup() == 1
    assert await env.store.cleanup() == 0
    available = env.path.parent / "original-128.png"
    assert not await asyncio.to_thread(available.exists)
    assert all(path.exists() for path in blocked) and env.path.exists()
    async with database.read_session() as reader:
        remaining = set(await reader.scalars(select(PluginMediaArtifactModel.handle_id)))
    assert remaining == {env.handle.handle_id, *(row["handle_id"] for row in rows[:-1])}


@pytest.mark.parametrize(
    "stored_expiry",
    ["2026-01-01 00:00:00", "2026-01-01T00:00:00", "2026-01-01 00:00:00.12"],
)
async def test_cleanup_settles_the_exact_original_stored_timestamp(
    database, plugin_media, stored_expiry
):
    env = plugin_media
    async with database.immediate_session() as writer:
        await writer.execute(
            text("UPDATE plugin_media_artifacts SET expires_at=:expiry WHERE handle_id=:handle"),
            {"expiry": stored_expiry, "handle": env.handle.handle_id},
        )
    assert await env.store.cleanup() == 1
    assert not env.path.exists()
    async with database.read_session() as reader:
        assert await reader.get(PluginMediaArtifactModel, env.handle.handle_id) is None


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
        async with database.read_session() as reader:
            row = await reader.get(PluginMediaArtifactModel, env.handle.handle_id)
            assert row is not None and Path(row.storage_path) == env.path
    finally:
        release.set()
        async with asyncio.timeout(5):
            await finished.wait()
        await asyncio.gather(cleaning, return_exceptions=True)
    assert await env.store.cleanup() == 1
    assert not env.path.exists()
    async with database.read_session() as reader:
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
    async with database.read_session() as session:
        assert await session.get(PluginMediaArtifactModel, env.handle.handle_id) is not None


async def test_foreign_plugin_cannot_read_original_media(plugin_media):
    env = plugin_media
    with pytest.raises(PluginPermissionError, match="foreign"):
        await env.store.read_owned(plugin_id="another.plugin", handle_id=env.handle.handle_id)
    assert env.path.read_bytes() == env.content


async def test_completed_owned_read_survives_natural_expiry(plugin_media, monkeypatch):
    import yuki.plugin_host.media_artifacts as media_module

    env = plugin_media
    clock = SimpleNamespace(now=datetime.now(UTC))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now if tz is not None else clock.now.replace(tzinfo=None)

    monkeypatch.setattr(media_module, "datetime", Clock)
    read = media_module._read_verified
    complete = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()

    def read_then_wait(artifact):
        data = read(artifact)
        loop.call_soon_threadsafe(complete.set)
        assert release.wait(5), "test must release its completed read"
        return data

    monkeypatch.setattr(media_module, "_read_verified", read_then_wait)
    reading = asyncio.create_task(
        env.store.read_owned(plugin_id=env.plugin, handle_id=env.handle.handle_id)
    )
    try:
        async with asyncio.timeout(5):
            await complete.wait()
        clock.now = env.handle.expires_at + timedelta(seconds=1)
        release.set()
        original, data = await reading
        assert original.handle_id == env.handle.handle_id and data == env.content
        with pytest.raises(PluginPermissionError, match="expired"):
            await env.store.resolve(plugin_id=env.plugin, handle_id=env.handle.handle_id)
    finally:
        release.set()
        await asyncio.gather(reading, return_exceptions=True)


async def test_concurrent_publications_recheck_the_original_storage_quota(
    database, plugin_media, monkeypatch
):
    import yuki.plugin_host.media_artifacts as media_module

    env = plugin_media
    publish = media_module._atomic_write
    ready = Barrier(2)

    def publish_together(*args):
        publish(*args)
        ready.wait(5)

    monkeypatch.setattr(media_module, "_atomic_write", publish_together)
    results = await asyncio.gather(
        *(
            env.store.create(
                plugin_id=env.plugin,
                data=env.content + b"x" * 600_000,
                content_type="image/png",
                filename=f"concurrent-{index}.png",
                ttl_seconds=60,
                storage_mb=1,
            )
            for index in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, PluginPermissionError) for item in results) == 1
    assert len(tuple(env.path.parent.glob("*-concurrent-*.png"))) == 1
    async with database.read_session() as reader:
        rows = tuple(await reader.scalars(select(PluginMediaArtifactModel)))
    assert len(rows) == 2 and sum(row.byte_size for row in rows) <= 1024 * 1024


async def test_publication_expiring_in_writer_queue_is_not_registered(
    database, plugin_media, monkeypatch
):
    import yuki.plugin_host.media_artifacts as media_module

    env = plugin_media
    clock = SimpleNamespace(now=datetime.now(UTC))
    published = asyncio.Event()
    publish = media_module._write_artifact

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now if tz is not None else clock.now.replace(tzinfo=None)

    async def notify_publication(*args):
        await publish(*args)
        published.set()

    monkeypatch.setattr(media_module, "datetime", Clock)
    monkeypatch.setattr(media_module, "_write_artifact", notify_publication)
    async with database.immediate_session():
        creating = asyncio.create_task(
            env.store.create(
                plugin_id=env.plugin,
                data=env.content,
                content_type="image/png",
                filename="expired-in-queue.png",
                ttl_seconds=60,
                storage_mb=1,
            )
        )
        async with asyncio.timeout(5):
            await published.wait()
        clock.now += timedelta(seconds=61)
    with pytest.raises(PluginPermissionError, match="already expired"):
        await creating
    assert not tuple(env.path.parent.glob("*-expired-in-queue.png"))
    async with database.read_session() as reader:
        assert len(tuple(await reader.scalars(select(PluginMediaArtifactModel)))) == 1


async def test_cancelled_publication_joins_original_file_worker_before_cleanup(
    database, plugin_media, monkeypatch
):
    import yuki.plugin_host.media_artifacts as media_module

    env = plugin_media
    publish = media_module._atomic_write
    started = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()

    def held_publication(*args):
        publish(*args)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "test must release the original file worker"

    monkeypatch.setattr(media_module, "_atomic_write", held_publication)
    creating = asyncio.create_task(
        env.store.create(
            plugin_id=env.plugin,
            data=env.content,
            content_type="image/png",
            filename="cancelled-original.png",
            ttl_seconds=60,
            storage_mb=1,
        )
    )
    try:
        async with asyncio.timeout(5):
            await started.wait()
        creating.cancel()
        await asyncio.sleep(0)
        creating.cancel()
        await asyncio.sleep(0)
        assert not creating.done()
        assert len(tuple(env.path.parent.glob("*-cancelled-original.png"))) == 1
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await creating
    assert not tuple(env.path.parent.glob("*-cancelled-original.png"))
    async with database.read_session() as reader:
        assert len(tuple(await reader.scalars(select(PluginMediaArtifactModel)))) == 1
