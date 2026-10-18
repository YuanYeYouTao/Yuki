"""One admitted original feeds cache, Main pixels and emoji without coupling outcomes."""

import asyncio
import io
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from tests.support.social_identity_cases import social_env

from yuki.admin.models import VisionRuntimeConfig
from yuki.conversation.media_service import ConversationMediaService
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import AttachmentKind, InboundMessage, MessageAttachment, SenderIdentity
from yuki.emoji.collector import EmojiCollector
from yuki.emoji.detector import EmojiCandidateDetector
from yuki.emoji.repository import EmojiRepository
from yuki.emoji.storage import EmojiStorage
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.services.attachment_inputs import AttachmentInputService
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_resolver import MediaResolutionError, MediaResolver, PreparedAttachment
from yuki.vision.models import MediaReference


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
        appended = await env.service.writer.append(
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
                ),
            ),
        )
        message = media.prepare_message(message, None, source_segments=event.segments)
        native = AttachmentInputService(
            resolver,
            preprocessor,
            concurrency=2,
            pending_limit=4,
            timeout=10,
            max_bytes=1_000_000,
        )
        emoji = EmojiCollector(
            detector=EmojiCandidateDetector(),
            resolver=resolver,
            storage=EmojiStorage(tmp_path / "emoji"),
            repository=EmojiRepository(database),
        )
        try:
            yield SimpleNamespace(
                media=media,
                message=message,
                native=native,
                emoji=emoji,
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
            await media.close()
            await emoji.close()


async def test_plain_input_does_not_encode_media_source_or_clone_message(
    admitted_media, monkeypatch
):
    env = admitted_media
    message = replace(env.message, attachments=(), segments=())

    def encode(*_args, **_kwargs):
        raise AssertionError("no media source needs encoding")

    monkeypatch.setattr("yuki.conversation.media_service.json", SimpleNamespace(dumps=encode))
    assert env.media.prepare_message(message, None, source_segments=message.segments) is message


@pytest.mark.parametrize("cache_full", [False, True])
async def test_cache_main_and_emoji_share_original_without_cache_admission_gate(
    admitted_media, database, monkeypatch, cache_full
):
    env = admitted_media
    if cache_full:
        monkeypatch.setattr("yuki.conversation.media_service._GLOBAL_BUDGET", 0)
    tasks = [
        asyncio.create_task(env.media._cache_event(env.event.id, None, env.message)),
        asyncio.create_task(env.native.prepare(env.message, env.runtime, None)),
        asyncio.create_task(
            env.emoji.collect_attachment(
                env.message.attachments[0],
                message=env.message,
                source_event_id=env.event.id,
                runtime=SimpleNamespace(near_duplicate_enabled=False),
                gateway=None,
            )
        ),
    ]
    await asyncio.wait_for(env.entered.wait(), 2)
    env.release.set()
    _, native, (asset, created, _) = await asyncio.gather(*tasks)
    assert len(env.calls) == 1
    assert native.images and asset.byte_size == len(env.data) and created
    async with database.sessions() as reader:
        item = await reader.scalar(
            select(ConversationMediaItemModel).where(
                ConversationMediaItemModel.source_event_id == env.event.id,
            )
        )
    assert item.cache_status == ("uncached" if cache_full else "cached")
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


async def test_main_does_not_wait_for_cache_publication(admitted_media):
    env = admitted_media
    async with env.media._lock:
        cached = asyncio.create_task(env.media._cache_event(env.event.id, None, env.message))
        native = asyncio.create_task(env.native.prepare(env.message, env.runtime, None))
        await env.entered.wait()
        env.release.set()
        async with asyncio.timeout(2):
            assert (await native).images
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
