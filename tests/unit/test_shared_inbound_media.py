"""One admitted original feeds cache, Main pixels and emoji without coupling outcomes."""

import asyncio
import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from sqlalchemy import delete, select, text
from sqlalchemy import event as sql_event
from tests.support.event_seed import seed_scoped_event
from tests.support.social_identity_cases import social_env

from yuki.admin.models import VisionRuntimeConfig
from yuki.conversation.hydrate import bump_canonical_generation
from yuki.conversation.media_service import ConversationMediaService
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import AttachmentKind, InboundMessage, MessageAttachment, SenderIdentity
from yuki.emoji.library import EmojiLibrary
from yuki.emoji.sticker_index import StickerIndex
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_resolver import MediaResolutionError, MediaResolver, PreparedAttachment
from yuki.vision.models import MediaReference
from yuki.workspace.service import WorkspaceService
from yuki.workspace.store import WorkspaceError


@pytest.fixture
async def admitted_media(database, tmp_path):
    env = await social_env(database, tmp_path)
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, "PNG")
    data = output.getvalue()
    calls = []
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def respond(request):
        calls.append(request)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return httpx.Response(200, content=data, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resolver = MediaResolver(client=client, host_resolver=lambda *_args: ("8.8.8.8",))
        preprocessor = ImagePreprocessor()
        media = ConversationMediaService(database, tmp_path / "cache", resolver, preprocessor)
        segment = {"type": "image", "data": {"url": "https://example.test/original.png"}}
        appended = await seed_scoped_event(
            env.service.writer,
            scope=ConversationScope.group("80001", "20001"),
            platform_message_id="shared-media",
            sender_user_id="10001",
            direction="inbound",
            content="look",
            segments=(segment,),
        )
        event = appended.event
        message = InboundMessage(
            "shared-media",
            "message",
            ScopeType.GROUP,
            SenderIdentity("10001"),
            "look",
            bot_user_id="80001",
            group_id="20001",
            person_id=env.person,
            space_id=env.space,
            source_event_id=event.id,
            conversation_id=event.canonical_conversation_id,
            segments=(segment,),
            attachments=(
                MessageAttachment(
                    AttachmentKind.IMAGE,
                    "emoji",
                    segment_index=0,
                    url="https://example.test/original.png",
                    is_sticker=True,
                ),
            ),
        )
        message = media.prepare_message(message, None, source_segments=event.segments)
        workspace = WorkspaceService(env.store)
        workspace.conversation_media = media
        files = tmp_path / "manager-workspace"
        copies = {}

        async def manager_execute(name, arguments, *, request_id):
            if name == "workspace_copy_result":
                previous = copies.get(request_id)
                if previous is None:
                    return {"found": False}
                bound, result = previous
                if any(arguments[key] != bound[key] for key in ("source_key", "destination")):
                    return {"error": "idempotency_conflict"}
                return {"found": True, "result": result}
            if name == "workspace_copy":
                source = media.root.parent / arguments["path"].removeprefix("/inputs/media-cache/")
                content = await asyncio.to_thread(source.read_bytes)
                digest = hashlib.sha256(content).hexdigest()
                assert arguments["expected_version"] == digest
                path = files / arguments["destination"]
                await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(path.write_bytes, content)
                result = {
                    "path": "/workspace/" + arguments["destination"],
                    "version": digest,
                    "size": len(content),
                }
                copies[request_id] = arguments, result
                return result
            assert name == "sticker_index"
            return await asyncio.to_thread(StickerIndex(files).execute, arguments)

        workspace.sandbox = SimpleNamespace(execute=manager_execute)
        emoji = EmojiLibrary(workspace)
        try:
            yield SimpleNamespace(
                media=media,
                message=message,
                emoji=emoji,
                files=files,
                workspace=workspace,
                store=env.store,
                data=data,
                event=event,
                calls=calls,
                entered=entered,
                release=release,
                cancelled=cancelled,
                runtime=VisionRuntimeConfig(4, 4, 4, False, 0, 100, 100, 7),
            )
        finally:
            release.set()
            await emoji.close()
            await media.close()


async def test_plain_input_does_not_encode_media_source_or_clone_message(
    admitted_media, monkeypatch
):
    env = admitted_media
    message = replace(env.message, attachments=(), segments=())

    def encode(*_args, **_kwargs):
        raise AssertionError("no media source needs encoding")

    monkeypatch.setattr("yuki.conversation.media_service.json", SimpleNamespace(dumps=encode))
    assert env.media.prepare_message(message, None, source_segments=message.segments) is message


async def test_prefetch_keeps_every_admitted_original_past_old_pending_limit(
    admitted_media, database
):
    env = admitted_media
    events = ScopedEventLedgerUnitOfWork(database)
    identities = []
    for index in range(33):
        appended = await seed_scoped_event(
            events,
            scope=env.event.scope,
            platform_message_id=f"queued-media-{index}",
            sender_user_id=env.event.sender_user_id,
            direction="inbound",
            content="original",
            segments=env.event.segments,
        )
        identities.append(appended.event.id)
        env.media.submit(appended.event.id, None)
    await asyncio.wait_for(env.entered.wait(), 5)
    env.release.set()
    await asyncio.gather(*tuple(env.media._pending))
    async with database.sessions() as reader:
        items = tuple(
            await reader.scalars(
                select(ConversationMediaItemModel).where(
                    ConversationMediaItemModel.source_event_id.in_(identities)
                )
            )
        )
    assert len(items) == 33 and all(item.cache_status == "cached" for item in items)
    assert len(env.calls) == 33


async def test_cache_emoji_and_explicit_inspection_share_original(admitted_media, database):
    env = admitted_media
    tasks = [
        asyncio.create_task(env.media._cache_event(env.event.id, None, env.message)),
        asyncio.create_task(env.emoji.collect_message(env.message, source_event_id=env.event.id)),
    ]
    await asyncio.wait_for(env.entered.wait(), 2)
    env.release.set()
    _, (asset,) = await asyncio.gather(*tasks)
    assert len(env.calls) == 1
    assert asset.byte_size == len(env.data)
    assert asset.relative_path.startswith("stickers/qq/files/")
    async with database.sessions() as reader:
        item = await reader.scalar(
            select(ConversationMediaItemModel).where(
                ConversationMediaItemModel.source_event_id == env.event.id,
            )
        )
    assert item.cache_status == "cached"
    live, path = await env.media.authorized_path(event_id=env.event.id, attachment_index=0)
    result = await env.media.inspect(live, path, "查看选定的附件", runtime=env.runtime)
    assert result.images and len(env.calls) == 1
    # Each consumer still applies its own byte budget; a rejection does not poison the original.
    with pytest.raises(MediaResolutionError, match="预算"):
        await env.message.attachments[0].prepared_media.resolve(1)
    again = await env.message.attachments[0].prepared_media.resolve(len(env.data))
    assert again.content == env.data and len(env.calls) == 1


async def test_consumer_cancel_does_not_cancel_other_original_reader(admitted_media):
    env = admitted_media
    original = env.message.attachments[0].prepared_media
    first = asyncio.create_task(original.resolve(100_000))
    await env.entered.wait()
    second = asyncio.create_task(original.resolve(100_000))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not env.cancelled.is_set()
    env.release.set()
    assert (await second).content == env.data and len(env.calls) == 1


async def test_last_reader_cancel_closes_actual_download(admitted_media):
    env = admitted_media
    task = asyncio.create_task(env.message.attachments[0].prepared_media.resolve(100_000))
    await env.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.cancelled.is_set()


async def test_old_prepared_original_cannot_publish_as_edited_attachment(admitted_media, database):
    env = admitted_media
    async with database.immediate_session() as writer:
        event = await writer.get(ChatEventModel, env.event.id)
        event.segments_json = '[{"type":"image","data":{"url":"https://example.test/new.png"}}]'
    await env.media._cache_event(env.event.id, None, env.message)
    assert not env.calls
    async with database.sessions() as reader:
        item = await reader.scalar(
            select(ConversationMediaItemModel).where(
                ConversationMediaItemModel.source_event_id == env.event.id,
            )
        )
    assert item.cache_status == "uncached"


async def test_stale_collection_does_not_download_an_edited_source(admitted_media, database):
    env = admitted_media
    async with database.immediate_session() as writer:
        event = await writer.get(ChatEventModel, env.event.id)
        event.segments_json = '[{"type":"image","data":{"url":"https://example.test/new.png"}}]'
    env.release.set()
    assert await env.emoji.collect_message(env.message, source_event_id=env.event.id) == ()
    assert not env.calls and not env.files.exists()


async def test_same_locator_in_new_message_does_not_share_original(admitted_media):
    env = admitted_media
    other = env.media.prepare_message(
        replace(env.message, message_id="new-intent", source_event_id=env.event.id + 1),
        None,
        source_segments=env.event.segments,
    )
    env.release.set()
    first, second = await asyncio.gather(
        env.message.attachments[0].prepared_media.resolve(100_000),
        other.attachments[0].prepared_media.resolve(100_000),
    )
    assert first.content == second.content == env.data and len(env.calls) == 2


async def test_explicit_original_read_does_not_wait_for_cache_publication(admitted_media):
    env = admitted_media
    async with env.media._lock:
        cached = asyncio.create_task(env.media._cache_event(env.event.id, None, env.message))
        selected = asyncio.create_task(env.message.attachments[0].prepared_media.resolve(100_000))
        await env.entered.wait()
        env.release.set()
        async with asyncio.timeout(2):
            assert (await selected).content == env.data
        assert not cached.done()
    await cached
    assert len(env.calls) == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_file_original_has_bounded_copies_and_cleans_own_directory(
    admitted_media, tmp_path, monkeypatch, cancel
):
    env = admitted_media
    from tempfile import TemporaryDirectory

    directories = []

    def directory(**kwargs):
        result = TemporaryDirectory(dir=tmp_path, **kwargs)
        directories.append(Path(result.name))
        return result

    monkeypatch.setattr("yuki.services.media_resolver.TemporaryDirectory", directory)
    original = PreparedAttachment(
        env.media.resolver,
        MediaReference(url="https://example.test/file.bin"),
        None,
        kind="file",
        max_download_bytes=100_000,
    )
    task = asyncio.create_task(original.resolve(100_000))
    await env.entered.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert env.cancelled.is_set()
    else:
        env.release.set()
        assert (await task).content == env.data
        with pytest.raises(MediaResolutionError, match="预算"):
            await original.download(tmp_path / "too-small", 1)
        await original.download(tmp_path / "copy", 100_000)
        assert (tmp_path / "copy").read_bytes() == env.data and len(env.calls) == 1
    await original.close()
    assert directories and all(not path.exists() for path in directories)


@pytest.mark.parametrize("change", ["delete", "edit", "unindex"])
async def test_collection_rechecks_original_source_after_download_wait(
    admitted_media, database, change
):
    env = admitted_media
    collecting = asyncio.create_task(
        env.emoji.collect_message(env.message, source_event_id=env.event.id)
    )
    await asyncio.wait_for(env.entered.wait(), 2)
    try:
        async with database.immediate_session() as writer:
            event = await writer.get(ChatEventModel, env.event.id)
            if change == "delete":
                await writer.delete(event)
            elif change == "unindex":
                await writer.execute(
                    delete(ConversationMediaItemModel).where(
                        ConversationMediaItemModel.source_event_id == env.event.id
                    )
                )
            else:
                event.segments_json = json.dumps(
                    [{"type": "image", "data": {"url": "https://example.test/replacement.png"}}]
                )
    finally:
        env.release.set()
    assert await asyncio.wait_for(collecting, 5) == ()
    assert not env.files.exists()


async def test_collection_publishes_shared_cache_without_delivery_artifact(
    admitted_media, database
):
    env = admitted_media
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    sql_event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    env.release.set()
    try:
        (saved,) = await env.emoji.collect_message(env.message, source_event_id=env.event.id)
    finally:
        sql_event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
    assert (env.files / saved.relative_path).read_bytes() == env.data
    assert env.store.list()["items"] == []
    assert not any("sticker_" in statement.lower() for statement in statements)
    async with database.sessions() as reader:
        item = await reader.get(ConversationMediaItemModel, (env.event.id, 0))
        assert item.cache_status == "cached" and item.content_sha256 == saved.sha256


async def test_completed_save_replays_before_source_and_preserves_later_user_edit(admitted_media):
    env = admitted_media
    env.release.set()
    args = {"event_id": env.event.id, "attachment_index": 0, "destination": "selected.png"}
    first = await env.workspace.execute(
        "save_conversation_attachment_to_workspace", args, request_id="original-save"
    )
    _item, original = await env.media.authorized_path(event_id=env.event.id, attachment_index=0)
    original.unlink()
    selected = env.files / "selected.png"
    selected.write_bytes(b"edited later")
    second = await env.workspace.execute(
        "save_conversation_attachment_to_workspace", args, request_id="original-save"
    )
    assert second == first and selected.read_bytes() == b"edited later"
    assert env.store.list()["items"] == [] and len(env.calls) == 1


@pytest.mark.parametrize("field", ["event_id", "destination"])
async def test_original_save_request_cannot_be_reused_for_a_different_copy(admitted_media, field):
    env = admitted_media
    env.release.set()
    args = {"event_id": env.event.id, "attachment_index": 0, "destination": "selected.png"}
    await env.workspace.execute(
        "save_conversation_attachment_to_workspace", args, request_id="original-save"
    )
    changed = {**args, field: env.event.id + 1 if field == "event_id" else "another.png"}
    with pytest.raises(WorkspaceError, match="idempotency_conflict"):
        await env.workspace.execute(
            "save_conversation_attachment_to_workspace", changed, request_id="original-save"
        )
    assert (env.files / "selected.png").read_bytes() == env.data
    assert not (env.files / "another.png").exists() and len(env.calls) == 1


@pytest.mark.parametrize("change", ["forget", "reset"])
async def test_admitted_shared_copy_survives_source_erasure_without_restoring_attribution(
    admitted_media, database, monkeypatch, change
):
    env = admitted_media
    entered, release = asyncio.Event(), asyncio.Event()
    original_copy = env.emoji.workspace.copy_from_cache

    async def paused_copy(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_copy(*args, **kwargs)

    monkeypatch.setattr(env.emoji.workspace, "copy_from_cache", paused_copy)
    env.release.set()
    collecting = asyncio.create_task(
        env.emoji.collect_message(env.message, source_event_id=env.event.id)
    )
    await asyncio.wait_for(entered.wait(), 5)
    try:
        # Read-only acceptance has finished; no table or pending-copy state was written.
        assert not list(env.files.rglob("*.json"))
        async with database.immediate_session() as writer:
            if change == "forget":
                await writer.execute(
                    delete(ChatEventModel).where(ChatEventModel.id == env.event.id)
                )
            else:
                await bump_canonical_generation(
                    writer, env.event.canonical_conversation_id, event_id=env.event.id
                )
    finally:
        release.set()
    (saved,) = await asyncio.wait_for(collecting, 5)
    assert (env.files / saved.relative_path).read_bytes() == env.data
    metadata = env.files / "stickers" / "qq" / "index" / f"{saved.id}.json"
    assert json.loads(metadata.read_text(encoding="utf-8"))["relative_path"] == saved.relative_path
    async with database.sessions() as reader:
        assert (await reader.get(ChatEventModel, env.event.id) is None) == (change == "forget")
        assert not any(
            name.startswith("sticker_")
            for name in await reader.scalars(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        )
