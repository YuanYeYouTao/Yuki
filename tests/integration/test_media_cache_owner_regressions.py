"""Real admitted media owners survive previews, edits and physical cache cleanup."""

import asyncio
import base64
import gzip
import hashlib
import os
import time
from dataclasses import replace

import pytest
from tests.integration.test_streamed_media_cache import OriginalBody, serve_original
from tests.integration.test_telegram_attachment_tools import attachment_tools as attachment_tools
from tests.integration.test_telegram_event_edits import (
    _edit,
    _path,
    _photo,
    _source,
)
from tests.integration.test_telegram_event_edits import media_reader as media_reader
from tests.integration.test_telegram_runtime import drain, update
from tests.integration.test_telegram_runtime import telegram_env as telegram_env
from tests.integration.test_telegram_sticker_inputs import TGS, TGS_DATA, original
from tests.support.event_seed import seed_event

from yuki.gateway.providers.telegram_ingress import normalize_message


@pytest.mark.parametrize(
    "attachment_tools",
    [
        {
            "content": TGS,
            "kind": "file",
            "data": {**TGS_DATA, "preview": {"file": "actual-sticker-preview"}},
        }
    ],
    indirect=True,
)
async def test_explicit_first_cache_keeps_original_instead_of_registered_preview(
    attachment_tools,
):
    case = attachment_tools
    inbound = original(case, preview=True)
    assert inbound.attachments[0].preview is not None
    _item, path = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
    assert path.read_bytes() == TGS
    assert case.wire == [
        ("POST", "/botfixture-a/getFile"),
        ("GET", "/file/botfixture-a/photos/original.png"),
    ]
    selected = await case.media.inspect(_item, path, "按需读取贴纸静态预览")
    assert selected["format"] == "TGS" and selected.images
    assert selected["sha256"] == hashlib.sha256(TGS).hexdigest()
    assert path.read_bytes() == TGS


async def test_actual_edit_ignores_old_shared_reuse_candidate(telegram_env, database, media_reader):
    env, reader = telegram_env, media_reader
    payload = update(text="")
    _photo(payload["message"])
    await env.gateway.receive(payload)
    await drain(env)
    source = await _source(database)
    inbound = normalize_message(payload["message"], env.gateway.client)
    prepared = reader.service.prepare_message(
        replace(
            inbound,
            source_event_id=source.id,
            conversation_id=source.canonical_conversation_id,
        ),
        reader.gateway,
        source_segments=source.segments,
    )
    # A frozen Main input can still own the old attachment after an edit.
    assert prepared.attachments[0].prepared_media is not None
    edited = _edit(2)
    _photo(edited["edited_message"], "new-photo")
    await env.gateway.receive(edited)
    _, path = await _path(reader, source)
    assert path.read_bytes() == reader.contents["new-photo"]
    assert reader.downloads == ["new-photo"]


async def test_cleanup_keeps_actual_stream_owner_even_when_staging_mtime_is_old(
    attachment_tools, monkeypatch
):
    case = attachment_tools
    body = OriginalBody(case.original_bytes, 100000, pause=True)
    requests = serve_original(case, monkeypatch, body)
    task = asyncio.create_task(case.media._cache_event(case.source.id, None))
    try:
        await asyncio.wait_for(body.entered.wait(), 5)
        parts = list(case.media.root.rglob("*.part"))
        assert len(parts) == 1
        # File age does not prove that its actual writer has exited.
        old = time.time() - 3700
        os.utime(parts[0], (old, old))
        await case.media.cleanup()
        assert parts[0].exists()
    finally:
        body.release.set()
        await task
    _, path = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
    assert path.stat().st_size == body.size
    assert requests == ["getFile", "original"] and body.closed


@pytest.mark.parametrize(
    "attachment_tools",
    [
        {"kind": "file", "content": gzip.compress(b"actual original")[:12]},
        {"kind": "file", "content": b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03\x07" + b"x" * 8},
    ],
    indirect=True,
)
async def test_incomplete_or_bad_gzip_does_not_gate_raw_original_storage(attachment_tools):
    from yuki.emoji.media import inspect_content

    case = attachment_tools
    _item, path = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
    assert path.read_bytes() == case.original_bytes
    assert inspect_content(case.original_bytes) == {
        "image_format": "BIN",
        "mime_type": "application/octet-stream",
        "sha256": hashlib.sha256(case.original_bytes).hexdigest(),
        "byte_size": len(case.original_bytes),
    }


@pytest.mark.parametrize(
    "attachment_tools",
    [{"kind": "file", "content": gzip.compress(b"plain original log\n" * 65536)}],
    indirect=True,
)
async def test_compressed_original_cache_does_not_require_semantic_decompression(
    attachment_tools, monkeypatch
):
    from yuki.conversation import media_service

    case = attachment_tools

    def forbidden_decode(*_args):
        raise AssertionError("raw original storage must not wait for gzip semantics")

    monkeypatch.setattr(media_service, "native_sticker_facts", forbidden_decode)
    _, path = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
    current, _ = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
    assert path.read_bytes() == case.original_bytes
    assert current.content_sha256 == hashlib.sha256(case.original_bytes).hexdigest()


@pytest.mark.parametrize("locator", ["file", "url"])
async def test_onebot_inline_audio_remains_shared_original_media(database, tmp_path, locator):
    from nonebot.adapters.onebot.v11 import Message, MessageSegment
    from tests.unit.test_normalizer import private_event

    from yuki.adapters.onebot.normalizer import normalize_event
    from yuki.conversation.media_service import ConversationMediaService
    from yuki.persistence.repositories import EventLedgerRepository
    from yuki.services.image_preprocessor import ImagePreprocessor
    from yuki.services.media_resolver import MediaResolver
    from yuki.vision.models import MediaReference

    content = b"RIFF admitted inline audio original"
    data_url = "data:audio/wav;base64," + base64.b64encode(content).decode()
    incoming = normalize_event(
        private_event(Message([MessageSegment("record", {locator: data_url})]))
    )
    event, _ = await seed_event(
        EventLedgerRepository(database),
        bot_user_id=incoming.bot_user_id,
        origin_key=incoming.ingress_key,
        scope_type=incoming.scope_type,
        sender_user_id=incoming.sender.user_id,
        private_peer_user_id=incoming.sender.user_id,
        direction="inbound",
        content=incoming.text,
        segments=incoming.segments,
    )
    resolver = MediaResolver()
    media = ConversationMediaService(database, tmp_path / "cache", resolver, ImagePreprocessor())
    try:
        original = await resolver.resolve_audio(MediaReference(**{locator: data_url}))
        assert original.content == content
        incoming = media.prepare_message(
            replace(incoming, source_event_id=event.id), None, source_segments=event.segments
        )
        _, path = await media.authorized_path(event_id=event.id, attachment_index=0)
        assert path.read_bytes() == content
        shared = incoming.attachments[0].prepared_media
        assert shared is not None
        assert (await shared.resolve(len(content))).content == content
    finally:
        await media.close()
        await resolver.close()
