"""Background collection follows original media facts, not the context floor."""

import asyncio
import hashlib
import json
from dataclasses import replace

import pytest
from tests.support.native_media_cases import (
    add_sticker,
    drain_native_turns,
    source_record,
    telegram_update,
)
from tests.support.native_media_cases import native_media_env as native_media_env
from tests.support.sticker_delivery import DirectoryBridge

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.media_service import ConversationMediaError
from yuki.emoji.library import EmojiLibrary
from yuki.gateway.providers.telegram_ingress import normalize_message
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.workspace.service import WorkspaceService
from yuki.workspace.store import WorkspaceStore


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "reset",
        "edit",
        "forget",
    ],
)
async def test_native_sticker_collection_keeps_only_the_actual_original_source(
    database, native_media_env, tmp_path, change
):
    env = native_media_env
    raw = telegram_update()
    add_sticker(raw["message"])
    await env.gateway.receive(raw)
    await drain_native_turns(env)
    source = await source_record(database)
    inbound = env.media.prepare_message(
        replace(
            normalize_message(raw["message"], env.gateway.client),
            source_event_id=source.id,
            conversation_id=source.canonical_conversation_id,
            person_id=source.author_person_id,
        ),
        env.media_gateway,
        source_segments=source.segments,
    )
    assert inbound.attachments[0].is_sticker
    workspace = WorkspaceService(WorkspaceStore(tmp_path / "published"))
    workspace.conversation_media = env.media
    bridge = DirectoryBridge(
        tmp_path / "shared-workspace", workspace.store, cache_root=env.media.root.parent
    )
    workspace.sandbox = bridge
    library = EmojiLibrary(workspace)
    entered, release = asyncio.Event(), asyncio.Event()
    resolve = env.media_gateway.resolve_media_file

    async def held_download(file, kind, *, max_download_bytes):
        if file == "old-sticker":
            entered.set()
            await release.wait()
        return await resolve(file, kind, max_download_bytes=max_download_bytes)

    env.media_gateway.resolve_media_file = held_download
    prefetch = asyncio.create_task(env.media._cache_event(source.id, env.media_gateway, inbound))
    collection = asyncio.create_task(library.collect_message(inbound, source_event_id=source.id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if change == "reset":
            async with database.read_session() as session:
                conversation = await session.get(
                    CanonicalConversationModel, source.canonical_conversation_id
                )
                original_generation = conversation.generation
            await env.gateway.receive(telegram_update(2, text="/ai new"))
            await drain_native_turns(env)
            async with database.read_session() as session:
                conversation = await session.get(
                    CanonicalConversationModel, source.canonical_conversation_id
                )
                assert conversation.generation == original_generation + 1
                assert conversation.starts_after_event_id >= source.id
        elif change == "edit":
            await env.gateway.receive(telegram_update(2, edit=True, text="sticker removed"))
        elif change == "forget":
            assert await PeopleRepository(database).delete_person_id(source.author_person_id)

        release.set()
        await prefetch
        files_root = bridge.root / "stickers"
        if change in {"edit", "forget"}:
            try:
                outcome = await collection
            except ConversationMediaError:
                outcome = ()
            assert outcome == ()
            assert not files_root.exists() or not tuple(files_root.rglob("files/*"))
            return

        # Both an unchanged conversation and /ai new keep this original file collectible.
        item, path = await env.media.authorized_path(event_id=source.id, attachment_index=0)
        content = env.contents["old-sticker"]
        assert path.read_bytes() == content
        async with database.read_session() as session:
            event = await session.get(ChatEventModel, source.id)
            cached = await session.get(ConversationMediaItemModel, (source.id, 0))
            assert event.author_person_id == source.author_person_id
            assert event.canonical_conversation_id == source.canonical_conversation_id
            assert json.loads(event.segments_json) == list(source.segments)
            assert cached.content_sha256 == hashlib.sha256(content).hexdigest()
        assert item.source_event_id == source.id
        outcome = await collection
        files = tuple(files_root.rglob("files/*")) if files_root.exists() else ()
        assert len(outcome) == len(files) == 1
        assert files[0].read_bytes() == content
        assert outcome[0].sha256 == hashlib.sha256(content).hexdigest()
        assert outcome[0].is_sticker is True
    finally:
        release.set()
        await asyncio.gather(prefetch, collection, return_exceptions=True)
        await library.close()
