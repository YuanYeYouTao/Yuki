"""Provided sticker previews share the original event's cache owner and lifetime."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update as sql_update
from tests.integration.test_telegram_attachment_tools import attachment_tools as attachment_tools
from tests.integration.test_telegram_event_edits import _edit, _source
from tests.integration.test_telegram_event_edits import media_reader as media_reader
from tests.integration.test_telegram_runtime import drain, update
from tests.integration.test_telegram_runtime import telegram_env as telegram_env
from tests.integration.test_telegram_sticker_inputs import TGS, TGS_DATA, original
from tests.support.service_dependencies import workspace_service
from tests.support.sticker_delivery import DirectoryBridge
from tests.unit.test_sticker_library import attach_caption_provider

from yuki.conversation.media_service import ConversationMediaError
from yuki.emoji.library import EmojiLibrary
from yuki.gateway.providers.telegram_ingress import normalize_message
from yuki.persistence.models import ConversationMediaItemModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.services.media_resolver import MediaResolutionError
from yuki.workspace.store import WorkspaceError, WorkspaceStore

_TGS = {
    "content": TGS,
    "kind": "file",
    "data": {**TGS_DATA, "preview": {"file": "actual-sticker-preview"}},
}


async def inspect(case, identifier):
    return await case.workspace.execute(
        "inspect_conversation_attachment",
        {"event_id": case.source.id, "attachment_index": 0, "question": "查看静态预览"},
        runtime=case.runtime,
        request_id=identifier,
    )


@pytest.mark.parametrize("attachment_tools", [_TGS], indirect=True)
async def test_cache_inspection_and_optional_caption_share_real_preview(attachment_tools, tmp_path):
    case = attachment_tools
    inbound = original(case, preview=True)
    bridge = DirectoryBridge(
        tmp_path / "shared-workspace", case.store, cache_root=case.media.root.parent
    )
    case.workspace.sandbox = bridge
    library = EmojiLibrary(case.workspace)
    provider = attach_caption_provider(library)
    try:
        _, assets = await asyncio.gather(
            case.media._cache_event(case.source.id, case.original_client, inbound),
            library.collect_message(inbound, source_event_id=case.source.id),
        )
        await asyncio.gather(*tuple(library._tasks))
        item, path = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
        expiry = item.expires_at
        assert (bridge.root / assets[0].relative_path).read_bytes() == TGS
        assert len(provider.requests) == 1
        metadata = bridge.root / "stickers/telegram/index" / f"{assets[0].id}.json"
        assert json.loads(metadata.read_text())["description"]
        case.registry.disconnect(case.original_client)
        first, second = await asyncio.gather(inspect(case, "first"), inspect(case, "second"))
        assert first.images[0].data_url == second.images[0].data_url
        await case.media.cleanup()
        assert path.is_file() and len(tuple(path.parent.glob("preview-*"))) == 1
        refreshed, _ = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
        assert refreshed.expires_at == expiry
        assert case.wire.count(("GET", "/file/botfixture-a/photos/original.png")) == 1
        assert case.wire.count(("GET", "/file/botfixture-a/photos/preview.png")) == 1
    finally:
        await library.close()


@pytest.mark.parametrize("attachment_tools", [_TGS], indirect=True)
async def test_preview_uses_original_cached_at_and_expires_without_redownload(
    attachment_tools, database
):
    case = attachment_tools
    await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
    cached_at = datetime.now(UTC) - timedelta(hours=23)
    expires_at = cached_at + timedelta(hours=24)
    async with database.immediate_session() as writer:
        await writer.execute(
            sql_update(ConversationMediaItemModel)
            .where(ConversationMediaItemModel.source_event_id == case.source.id)
            .values(cached_at=cached_at, expires_at=expires_at)
        )
    await inspect(case, "late-first-preview")
    item, path = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
    preview = next(path.parent.glob("preview-*"))
    assert preview.stat().st_mtime == pytest.approx(cached_at.timestamp())
    assert item.expires_at.replace(tzinfo=UTC) == expires_at
    async with database.immediate_session() as writer:
        await writer.execute(
            sql_update(ConversationMediaItemModel)
            .where(ConversationMediaItemModel.source_event_id == case.source.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await case.media.cleanup()
    assert not path.exists() and not preview.exists()
    before = list(case.wire)
    with pytest.raises(WorkspaceError, match="attachment_expired"):
        await inspect(case, "expired-preview")
    assert case.wire == before


@pytest.mark.parametrize("attachment_tools", [_TGS], indirect=True)
async def test_forgetting_source_cleans_original_and_provided_preview(attachment_tools, database):
    case = attachment_tools
    selected = await inspect(case, "before-forget")
    _, path = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
    preview = next(path.parent.glob("preview-*"))
    assert await PeopleRepository(database).delete_person_id(case.source.author_person_id)
    await case.media.cleanup()
    assert not path.exists() and not preview.exists()
    with pytest.raises(ConversationMediaError, match="attachment_not_found"):
        await case.media.validate_images(selected.images)


@pytest.mark.parametrize("attachment_tools", [_TGS], indirect=True)
async def test_failed_preview_does_not_fail_original_or_permanent_collection(
    attachment_tools, tmp_path, monkeypatch
):
    case = attachment_tools
    inbound = original(case, preview=True)
    resolve = case.original_client.resolve_media_file

    async def fail_preview(file, kind, *, max_download_bytes):
        if file == "actual-sticker-preview":
            raise MediaResolutionError("resource_unavailable", "preview unavailable")
        return await resolve(file, kind, max_download_bytes=max_download_bytes)

    monkeypatch.setattr(case.original_client, "resolve_media_file", fail_preview)
    bridge = DirectoryBridge(
        tmp_path / "shared-workspace", case.store, cache_root=case.media.root.parent
    )
    case.workspace.sandbox = bridge
    library = EmojiLibrary(case.workspace)
    provider = attach_caption_provider(library)
    try:
        _, assets = await asyncio.gather(
            case.media._cache_event(case.source.id, case.original_client, inbound),
            library.collect_message(inbound, source_event_id=case.source.id),
        )
        await asyncio.gather(*tuple(library._tasks), return_exceptions=True)
        item, path = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
        assert item.cache_status == "cached" and path.read_bytes() == TGS
        assert (bridge.root / assets[0].relative_path).read_bytes() == TGS
        assert not provider.requests
        with pytest.raises(WorkspaceError, match="resource_unavailable"):
            await inspect(case, "failed-preview")
        assert path.read_bytes() == TGS
    finally:
        await library.close()


def sticker(message, thumbnail="old-photo"):
    message["sticker"] = {
        "file_id": "animated-original",
        "is_animated": True,
        "width": 64,
        "height": 64,
        "thumbnail": {"file_id": thumbnail, "width": 2, "height": 2},
    }


async def test_native_caption_edit_reuses_preview_but_thumbnail_edit_replaces_pixels(
    telegram_env, database, media_reader
):
    env, reader = telegram_env, media_reader
    reader.contents["animated-original"] = TGS
    inbound = update(text="")
    sticker(inbound["message"])
    await env.gateway.receive(inbound)
    await drain(env)
    source = await _source(database)

    async def read():
        item, path = await reader.service.authorized_path(event_id=source.id, attachment_index=0)
        selected = await reader.service.inspect(item, path, "查看静态预览")
        item, path = await reader.service.authorized_path(event_id=source.id, attachment_index=0)
        return item, path, selected

    before, path, selected = await read()
    first_preview = next(path.parent.glob("preview-*"))
    edit = _edit(2, "新增图注")
    sticker(edit["edited_message"])
    await env.gateway.receive(edit)
    after, same_path, caption = await read()
    assert same_path == path and before.expires_at == after.expires_at
    assert selected.images[0].data_url == caption.images[0].data_url
    assert reader.downloads == ["animated-original", "old-photo"]
    edit = _edit(3, "新增图注", edited_at=1792070011)
    sticker(edit["edited_message"], "new-photo")
    await env.gateway.receive(edit)
    _, _, changed = await read()
    assert changed.images[0].data_url != selected.images[0].data_url
    assert reader.downloads == ["animated-original", "old-photo", "animated-original", "new-photo"]
    with pytest.raises(ConversationMediaError, match="attachment_source_changed"):
        await reader.service.validate_images(caption.images)
    await reader.service.cleanup()
    assert first_preview.is_file()


async def test_captioned_sticker_cache_and_collection_share_parent_preview(
    telegram_env, database, media_reader, tmp_path
):
    env, reader = telegram_env, media_reader
    reader.contents["animated-original"] = TGS
    raw = update(text="贴纸图注")
    sticker(raw["message"])
    await env.gateway.receive(raw)
    await drain(env)
    source = await _source(database)
    inbound = reader.service.prepare_message(
        replace(
            normalize_message(raw["message"], env.gateway.client),
            source_event_id=source.id,
            conversation_id=source.canonical_conversation_id,
            person_id=source.author_person_id,
        ),
        reader.gateway,
        source_segments=source.segments,
    )
    assert inbound.attachments[0].segment_index == 1
    workspace = workspace_service(WorkspaceStore(tmp_path / "permanent-artifacts"))
    workspace.conversation_media = reader.service
    bridge = DirectoryBridge(
        tmp_path / "shared-workspace", workspace.store, cache_root=reader.service.root.parent
    )
    workspace.sandbox = bridge
    library = EmojiLibrary(workspace)
    provider = attach_caption_provider(library)
    try:
        _, assets = await asyncio.gather(
            reader.service._cache_event(source.id, reader.gateway, inbound),
            library.collect_message(inbound, source_event_id=source.id),
        )
        await asyncio.gather(*tuple(library._tasks))
        assert (bridge.root / assets[0].relative_path).read_bytes() == TGS
        assert len(provider.requests) == 1
        assert reader.downloads == ["animated-original", "old-photo"]
    finally:
        await library.close()


async def test_thumbnail_replacement_does_not_renew_old_preview_lifetime(
    telegram_env, database, media_reader
):
    env, reader = telegram_env, media_reader
    reader.contents["animated-original"] = TGS
    raw = update(text="")
    sticker(raw["message"])
    await env.gateway.receive(raw)
    await drain(env)
    source = await _source(database)
    item, path = await reader.service.authorized_path(event_id=source.id, attachment_index=0)
    cached_at = datetime.now(UTC) - timedelta(hours=23)
    async with database.immediate_session() as writer:
        await writer.execute(
            sql_update(ConversationMediaItemModel)
            .where(ConversationMediaItemModel.source_event_id == source.id)
            .values(cached_at=cached_at, expires_at=cached_at + timedelta(hours=24))
        )
    await reader.service.inspect(item, path, "旧预览")
    old_preview = next(path.parent.glob("preview-*"))
    edit = _edit(2)
    sticker(edit["edited_message"], "new-photo")
    await env.gateway.receive(edit)
    current, same_path = await reader.service.authorized_path(
        event_id=source.id, attachment_index=0
    )
    await reader.service.inspect(current, same_path, "新预览")
    assert same_path == path
    now = datetime.now(UTC) + timedelta(hours=2)
    await reader.service._expire_page(now)
    await reader.service._gc_cache_page(now)
    while reader.service._gc_iterator is not None or reader.service._gc_pending:
        await reader.service._gc_cache_page(now)
    assert path.is_file()
    assert not old_preview.exists()
    assert len(tuple(path.parent.glob("preview-*"))) == 1
