"""Native edits retain event ownership and frozen evidence while replacing current data."""

import asyncio
import hashlib
import io
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import delete, select
from tests.integration.test_telegram_runtime import drain, update
from tests.integration.test_telegram_runtime import telegram_env as telegram_env

from yuki.conversation import media_service
from yuki.conversation.media_service import ConversationMediaError, ConversationMediaService
from yuki.domain.messages import AttachmentKind, ChatMessage
from yuki.gateway.media import PlatformMediaFile
from yuki.gateway.provider import GatewayProviderCatalog
from yuki.gateway.providers.telegram import TelegramProvider
from yuki.gateway.providers.telegram_ingress import normalize_message
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.persistence.repository_helpers import _event_record
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_resolver import MediaResolver


def _edit(identifier, text="", *, message_id=1, edited_at=1792070010, chat=42):
    result = update(identifier, text=text, chat=chat, edit=True)
    result["edited_message"].update(message_id=message_id, edit_date=edited_at)
    return result


def _photo(message, file="old-photo"):
    message["photo"] = [{"file_id": file, "file_unique_id": file, "width": 2, "height": 2}]


async def _source(database, message_id="42:1"):
    async with database.read_session() as reader:
        row = await reader.scalar(
            select(ChatEventModel).where(
                ChatEventModel.origin_key == message_id,
                ChatEventModel.direction == "inbound",
            )
        )
        return _event_record(row)


@pytest.mark.parametrize("replacement", [None, 44])
async def test_edit_replaces_mentions_without_changing_author_or_starting_main(
    telegram_env, database, replacement
):
    env = telegram_env
    for identity in (43, 44):
        person = update(identity, chat=identity)
        person["message"]["from"]["id"] = identity
        await env.gateway.receive(person)
        await drain(env)
    original = update(2, chat=-10042, text="!ai Bob")
    original["message"]["entities"] = [
        {
            "type": "text_mention",
            "offset": 4,
            "length": 3,
            "user": {"id": 43, "is_bot": False, "first_name": "Bob"},
        }
    ]
    await env.gateway.receive(original)
    await drain(env)
    before = await _source(database, "-10042:2")
    assert before.mentioned_user_ids == ("43",) and len(before.mentioned_person_ids) == 1
    requests, deliveries = len(env.model.requests), len(env.wire)
    edit = _edit(3, "!ai new", message_id=2, chat=-10042)
    if replacement:
        edit["edited_message"]["entities"] = [
            {
                "type": "text_mention",
                "offset": 4,
                "length": 3,
                "user": {"id": replacement, "is_bot": False, "first_name": "new"},
            }
        ]
    await env.gateway.receive(edit)
    after = await _source(database, "-10042:2")
    assert after.id == before.id and after.author_person_id == before.author_person_id
    assert after.canonical_conversation_id == before.canonical_conversation_id
    if replacement:
        target = await _source(database, "44:44")
        assert after.mentioned_user_ids == ("44",)
        assert after.mentioned_person_ids == (target.author_person_id,)
    else:
        assert after.mentioned_user_ids == after.mentioned_person_ids == ()
    assert len(env.model.requests) == requests and len(env.wire) == deliveries


async def test_edit_version_uses_edit_time_across_random_update_ids_and_replay(
    telegram_env, database
):
    env = telegram_env
    await env.gateway.receive(update())
    await drain(env)
    requests, deliveries = len(env.model.requests), len(env.wire)
    await env.gateway.receive(_edit(100, "first"))
    later = 1792070010 + 8 * 86400
    await env.gateway.receive(_edit(50, "after idle week", edited_at=later))
    await env.gateway.receive(_edit(500, "late old edit", edited_at=later - 1))
    await env.gateway.receive(_edit(49, "same second older update", edited_at=later))
    await env.gateway.receive(_edit(51, "same second newer update", edited_at=later))
    await env.gateway.receive(_edit(51, "same second newer update", edited_at=later))
    after = await _source(database)
    assert after.content == "same second newer update"
    assert len(env.model.requests) == requests and len(env.wire) == deliveries


@pytest.fixture
async def media_reader(database, tmp_path, telegram_env):
    def png(color):
        stream = io.BytesIO()
        Image.new("RGB", (2, 2), color).save(stream, "PNG")
        return stream.getvalue()

    contents = {"old-photo": png("red"), "new-photo": png("blue")}
    downloads = []

    class Gateway:
        async def resolve_media_file(self, file, kind, *, max_download_bytes):
            downloads.append(file)
            return PlatformMediaFile(content=contents[file], content_type="image/png")

    gateway = Gateway()

    class MediaProvider(TelegramProvider):
        def media(self, handle):
            return gateway

    registry = GatewayConnectionRegistry(providers=GatewayProviderCatalog((MediaProvider(),)))
    registry.connect(
        telegram_env.gateway.client,
        provider_id="telegram",
        presence_id=telegram_env.gateway.presence_id,
    )
    resolver = MediaResolver()
    service = ConversationMediaService(
        database, tmp_path / "cache", resolver, ImagePreprocessor(), gateway_registry=registry
    )
    try:
        yield SimpleNamespace(
            service=service, gateway=gateway, contents=contents, downloads=downloads
        )
    finally:
        await service.close()
        await resolver.close()


async def _path(reader, source):
    return await reader.service.authorized_path(
        event_id=source.id,
        attachment_index=0,
    )


async def test_media_replacement_reads_new_bytes_and_preserves_frozen_old_payload(
    telegram_env, database, media_reader
):
    env, reader = telegram_env, media_reader
    original = update(text="")
    _photo(original["message"])
    await env.gateway.receive(original)
    await drain(env)
    source = await _source(database)
    old_item, old_path = await _path(reader, source)
    prepared = await reader.service.inspect(old_item, old_path, "synthetic image question")
    frozen = ChatMessage("user", "frozen original observation", images=prepared.images)
    frozen_json = json.dumps(asdict(frozen), sort_keys=True)
    edit = _edit(2)
    _photo(edit["edited_message"], "new-photo")
    await env.gateway.receive(edit)
    _, new_path = await _path(reader, source)
    assert new_path.read_bytes() == reader.contents["new-photo"]
    assert old_path.read_bytes() == reader.contents["old-photo"]
    assert reader.downloads == ["old-photo", "new-photo"]
    await reader.service.cleanup()
    assert old_path.read_bytes() == reader.contents["old-photo"]
    assert json.dumps(asdict(frozen), sort_keys=True) == frozen_json
    assert (await _source(database)).id == source.id


async def test_caption_repositions_existing_index_and_reuses_original_cache_lifetime(
    telegram_env, database, media_reader
):
    env, reader = telegram_env, media_reader
    original = update(text="")
    _photo(original["message"])
    await env.gateway.receive(original)
    await drain(env)
    source = await _source(database)
    _, original_path = await _path(reader, source)
    async with database.read_session() as session:
        cached = await session.get(ConversationMediaItemModel, (source.id, 0))
        original_expiry = cached.expires_at
    edit = _edit(2, "caption now exists")
    _photo(edit["edited_message"])
    await env.gateway.receive(edit)
    item, current_path = await _path(reader, source)
    assert item.segment_index == 1 and current_path == original_path
    assert item.expires_at == original_expiry
    assert reader.downloads == ["old-photo"]
    await env.gateway.receive(_edit(3, "media removed", edited_at=1792070011))
    with pytest.raises(ConversationMediaError, match="attachment_not_found"):
        await _path(reader, source)
    await reader.service.cleanup()
    assert original_path.is_file()
    async with database.immediate_session() as writer:
        await writer.execute(delete(ChatEventModel).where(ChatEventModel.id == source.id))
    await reader.service.cleanup()
    assert not original_path.exists()


async def test_caption_before_and_during_download_reuses_one_original(
    telegram_env, database, media_reader
):
    env, reader = telegram_env, media_reader
    original = update(text="")
    _photo(original["message"])
    await env.gateway.receive(original)
    await drain(env)
    source = await _source(database)
    before = _edit(2, "caption before download")
    _photo(before["edited_message"])
    await env.gateway.receive(before)
    entered, release = asyncio.Event(), asyncio.Event()
    resolve = reader.gateway.resolve_media_file

    async def delayed(file, kind, *, max_download_bytes):
        entered.set()
        await release.wait()
        return await resolve(file, kind, max_download_bytes=max_download_bytes)

    reader.gateway.resolve_media_file = delayed
    task = asyncio.create_task(_path(reader, source))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        during = _edit(3, "caption during download", edited_at=1792070011)
        _photo(during["edited_message"])
        await env.gateway.receive(during)
        release.set()
        item, path = await task
        assert item.segment_index == 1
        assert path.read_bytes() == reader.contents["old-photo"]
        assert reader.downloads == ["old-photo"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_inflight_old_download_cannot_publish_over_new_media(
    telegram_env, database, media_reader
):
    env, reader = telegram_env, media_reader
    original = update(text="")
    _photo(original["message"])
    await env.gateway.receive(original)
    await drain(env)
    source = await _source(database)
    entered, release = asyncio.Event(), asyncio.Event()
    resolve = reader.gateway.resolve_media_file

    async def delayed(file, kind, *, max_download_bytes):
        if file == "old-photo":
            entered.set()
            await release.wait()
        return await resolve(file, kind, max_download_bytes=max_download_bytes)

    reader.gateway.resolve_media_file = delayed
    task = asyncio.create_task(_path(reader, source))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        edit = _edit(2)
        _photo(edit["edited_message"], "new-photo")
        await env.gateway.receive(edit)
        release.set()
        with pytest.raises(ConversationMediaError, match="attachment_source_changed"):
            await task
        _, path = await _path(reader, source)
        assert path.read_bytes() == reader.contents["new-photo"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_animation_compatibility_document_is_one_native_attachment():
    raw = update(text="")["message"]
    raw["animation"] = {
        "file_id": "animation",
        "file_unique_id": "same",
        "width": 2,
        "height": 2,
        "duration": 1,
        "file_name": "loop.mp4",
        "mime_type": "video/mp4",
        "file_size": 41,
    }
    raw["document"] = dict(raw["animation"])
    normalized = normalize_message(raw, SimpleNamespace(self_id="80001", username="yuki_test"))
    assert len(normalized.attachments) == 1
    assert normalized.attachments[0].kind is AttachmentKind.VIDEO
    assert normalized.attachments[0].file == "animation"
    assert normalized.segments[0]["data"]["file_size"] == 41


@pytest.mark.parametrize("edit_when", ["before_inspection", "during_parse"])
async def test_document_edit_never_labels_old_content_with_new_digest(
    telegram_env, database, media_reader, monkeypatch, edit_when
):
    env, reader = telegram_env, media_reader
    reader.contents.update(
        {"old-document": b"OLD document evidence", "new-document": b"NEW document evidence"}
    )
    original = update(text="")
    original["message"]["document"] = {"file_id": "old-document", "file_name": "doc.txt"}
    await env.gateway.receive(original)
    await drain(env)
    source = await _source(database)
    old_item, old_path = await _path(reader, source)
    frozen = await reader.service.inspect(old_item, old_path, "read document")
    frozen_json = json.dumps(frozen, sort_keys=True)
    assert frozen["sha256"] == hashlib.sha256(reader.contents["old-document"]).hexdigest()
    assert "OLD document evidence" in frozen_json

    async def change_source():
        edit = _edit(2)
        edit["edited_message"]["document"] = {"file_id": "new-document", "file_name": "doc.txt"}
        await env.gateway.receive(edit)
        return await _path(reader, source)

    if edit_when == "before_inspection":
        await change_source()
    else:
        parse = media_service.run_parser
        changed = False

        async def parse_then_edit(*args, **kwargs):
            nonlocal changed
            result = await parse(*args, **kwargs)
            if not changed:
                changed = True
                await change_source()
            return result

        monkeypatch.setattr(media_service, "run_parser", parse_then_edit)
    with pytest.raises(ConversationMediaError, match="attachment_source_changed"):
        await reader.service.inspect(old_item, old_path, "read document")
    current_item, current_path = await _path(reader, source)
    result = await reader.service.inspect(current_item, current_path, "read document")
    assert "NEW document evidence" in json.dumps(result)
    assert result["sha256"] == hashlib.sha256(reader.contents["new-document"]).hexdigest()
    assert json.dumps(frozen, sort_keys=True) == frozen_json
