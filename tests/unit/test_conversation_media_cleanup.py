"""Bounded metadata expiration and filesystem GC preserve live cache owners."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, insert, select

from qq_ai_bot.conversation import media_service
from qq_ai_bot.conversation.media_service import ConversationMediaService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.persistence.models import ConversationMediaItemModel
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
        database, tmp_path / "media", AsyncMock(), ImagePreprocessor(), None
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
async def test_cleanup_bounds_metadata_and_advances_file_cursor_without_full_live_scan(
    database, tmp_path
):
    service, source, directory = await _cache(database, tmp_path, 260)
    statement_rows = []

    def sql(connection, cursor, statement, parameters, context, many):
        lowered = statement.lower()
        if lowered.startswith("select") and "from conversation_media_items" in lowered:
            statement_rows.append(lowered)

    event.listen(database.engine.sync_engine, "before_cursor_execute", sql)
    try:
        await service.cleanup()
        async with database.sessions() as reader:
            rows = tuple(await reader.scalars(select(ConversationMediaItemModel)))
        assert sum(row.cache_status == "expired" for row in rows) == 128
        assert len(tuple(directory.glob("*.png"))) >= 132
        # Subsequent pages continue from the scanner, and all cached metadata
        # is eventually expired without deleting the permanent ledger event.
        for _ in range(8):
            await service.cleanup()
        async with database.sessions() as reader:
            rows = tuple(await reader.scalars(select(ConversationMediaItemModel)))
        assert all(row.cache_status == "expired" for row in rows)
        assert not tuple(service.root.rglob("*.png"))
        assert await EventLedgerRepository(database).get_event(source.id) is not None
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", sql)
        await service.close()
    cleanup_reads = [statement for statement in statement_rows if "where" in statement]
    assert cleanup_reads
    assert all(
        " limit " in statement or "source_event_id in" in statement for statement in cleanup_reads
    )


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
