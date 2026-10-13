"""Bounded metadata expiration and filesystem GC preserve live cache owners."""

import asyncio
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, insert, select, update

from qq_ai_bot.conversation import media_service
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.media_service import ConversationMediaError, ConversationMediaService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatImage
from qq_ai_bot.persistence.models import ChatEventModel, ConversationMediaItemModel
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor


async def _cache(database, tmp_path, count=1):
    source, _ = await EventLedgerRepository(database).append(
        bot_user_id="8000",
        platform_message_id="cleanup-media",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        private_peer_user_id="1001",
        direction="inbound",
        content="attachment",
    )
    service = ConversationMediaService(
        database, tmp_path / "media", AsyncMock(), ImagePreprocessor()
    )
    directory = service.root / source.canonical_conversation_id / str(source.id)
    directory.mkdir(parents=True)
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(ConversationMediaItemModel),
            [
                dict(
                    source_event_id=source.id,
                    attachment_index=index,
                    conversation_id=source.canonical_conversation_id,
                    generation=1,
                    segment_index=index,
                    kind="image",
                    created_at=now,
                    cached_at=now - timedelta(days=2),
                    expires_at=now - timedelta(days=1),
                    cache_status="cached",
                    cache_name=f"{index}-cached.png",
                )
                for index in range(count)
            ],
        )
    for index in range(count):
        (directory / f"{index}-cached.png").write_bytes(b"tiny cache fixture")
    return service, source, directory


@pytest.mark.asyncio
async def test_cleanup_retains_later_pages_and_eventually_removes_expired_files(database, tmp_path):
    service, source, directory = await _cache(database, tmp_path, 260)
    try:
        await service.cleanup()
        async with database.sessions() as reader:
            rows = tuple(await reader.scalars(select(ConversationMediaItemModel)))
        assert 0 < sum(row.cache_status == "expired" for row in rows) < len(rows)
        assert all(
            (directory / row.cache_name).exists() for row in rows if row.cache_status != "expired"
        )
        # Drain actual pages under a deadline; batch size and loop count are
        # implementation choices, while source preservation is a real contract.
        async with asyncio.timeout(10):
            while any(row.cache_status != "expired" for row in rows) or tuple(
                service.root.rglob("*.png")
            ):
                await service.cleanup()
                async with database.sessions() as reader:
                    rows = tuple(await reader.scalars(select(ConversationMediaItemModel)))
        assert await EventLedgerRepository(database).get_event(source.id) is not None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_expiry_discovery_rechecks_updated_lifetime_before_metadata_and_gc(
    database, tmp_path, monkeypatch
):
    service, source, directory = await _cache(database, tmp_path)
    original = media_service.update
    injected = False

    def update_and_mark(model):
        nonlocal injected
        # Hook just before the conditional expiration statement is built.
        # Actual concurrent publish below changes the row after readonly discovery.
        injected = True
        return original(model)

    original_immediate = database.immediate_session
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def publish_before_writer():
        async with original_immediate() as writer:
            await writer.execute(
                original(ConversationMediaItemModel)
                .where(ConversationMediaItemModel.source_event_id == source.id)
                .values(expires_at=datetime.now(UTC) + timedelta(hours=24))
            )
        async with original_immediate() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", publish_before_writer)
    monkeypatch.setattr(media_service, "update", update_and_mark)
    await service.cleanup()
    assert injected and (directory / "0-cached.png").is_file()
    async with database.sessions() as reader:
        item = await reader.get(ConversationMediaItemModel, (source.id, 0))
        assert item.cache_status == "cached" and item.cache_name == "0-cached.png"
    await service.close()


@pytest.mark.asyncio
async def test_blocked_gc_filesystem_keeps_event_loop_responsive_and_publish_lock_owned(
    database, tmp_path, monkeypatch
):
    service, _source, _directory = await _cache(database, tmp_path)
    started, release = threading.Event(), threading.Event()
    original = Path.unlink

    def blocked_unlink(path, *args, **kwargs):
        if path.suffix == ".png":
            started.set()
            assert release.wait(5)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked_unlink)
    cleanup = asyncio.create_task(service.cleanup())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        # No filesystem call owns this loop: a real SQLite reader still runs.
        async with asyncio.timeout(0.5), database.sessions() as reader:
            assert await reader.scalar(select(ConversationMediaItemModel.cache_status)) == "expired"
        assert service._lock.locked()
        cleanup.cancel()
        await asyncio.sleep(0)
        assert not cleanup.done() and service._lock.locked()
    finally:
        release.set()
        await asyncio.gather(cleanup, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["generation", "hash", "expiry", "keeper"])
async def test_selected_media_batches_frames_and_rechecks_real_sources(
    database, tmp_path, monkeypatch, change
):
    service, source, _directory = await _cache(database, tmp_path, 3)
    expires = datetime.now(UTC) + timedelta(hours=24)
    digest = "a" * 64
    segments = json.dumps([{"type": "image", "data": {"file": str(i)}} for i in range(3)])
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == source.id)
            .values(segments_json=segments, content="unused body" * 10000)
        )
        await writer.execute(
            update(ConversationMediaItemModel)
            .where(ConversationMediaItemModel.source_event_id == source.id)
            .values(expires_at=expires, content_sha256=digest)
        )
    images = tuple(
        ChatImage(
            "data:image/png;base64,eA==",
            source="history",
            conversation_id=source.canonical_conversation_id,
            generation=1,
            source_event_id=source.id,
            attachment_index=index,
            content_hash=digest,
            expires_at=expires.isoformat(),
        )
        for index in range(3)
    )
    parse = json.loads
    parses = 0
    sql = []

    def count_parse(value, *args, **kwargs):
        nonlocal parses
        parses += int(value == segments)
        return parse(value, *args, **kwargs)

    def capture(_connection, _cursor, statement, *_args):
        sql.append(statement)

    monkeypatch.setattr(media_service.json, "loads", count_parse)
    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await service.validate_images((*images, replace(images[0], video_timestamp_seconds=1.0)))
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert parses == 1
    assert not any("chat_events.content" in statement for statement in sql)
    with pytest.raises(ConversationMediaError, match="attachment_scope_denied"):
        await service.validate_images((replace(images[0], conversation_id="other"),))
    async with database.immediate_session() as writer:
        if change == "generation":
            await writer.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == source.canonical_conversation_id)
                .values(generation=2)
            )
        elif change == "keeper":
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == source.id)
                .values(suppression_status="duplicate", utterance_fingerprint="d" * 64)
            )
        else:
            await writer.execute(
                update(ConversationMediaItemModel)
                .where(ConversationMediaItemModel.source_event_id == source.id)
                .values(
                    **(
                        {"content_sha256": "b" * 64}
                        if change == "hash"
                        else {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
                    )
                )
            )
    with pytest.raises(ConversationMediaError):
        await service.validate_images(images)
    service.resolver.resolve.assert_not_awaited()
    await service.close()
