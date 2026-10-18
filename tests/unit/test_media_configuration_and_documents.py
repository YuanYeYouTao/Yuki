"""Documents use document budgets, and visual choices retain explicit configuration."""

import hashlib
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.conftest import make_settings
from tests.unit.test_conversation_media_cleanup import _cache
from tests.unit.test_native_media_inputs import _image

from yuki.admin.config_service import RuntimeConfigService
from yuki.conversation.media_service import ConversationMediaError
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.services.attachment_inputs import AttachmentInputService
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_process import MediaProcessError, run_media_process
from yuki.services.video_frames import _run as run_video_process
from yuki.services.vision_service import VisionProcessingError


def message(*files):
    return InboundMessage(
        message_id="original-message",
        event_type="message:test",
        platform="telegram",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("1001"),
        text="read attachments",
        bot_user_id="8000",
        group_id="2001",
        person_id=str(uuid4()),
        space_id=str(uuid4()),
        attachments=tuple(
            MessageAttachment(
                kind=AttachmentKind.FILE, label=name, source="current", file=name, filename=name
            )
            for name in files
        ),
    )


def service(files, *, allowed=True):
    async def download(reference, path, **kwargs):
        path.write_bytes(files[reference.file])

    result = AttachmentInputService(
        SimpleNamespace(download_attachment=download),
        ImagePreprocessor(),
        concurrency=2,
        pending_limit=8,
        timeout=30,
        max_bytes=6_291_456,
    )
    result._limiter = SimpleNamespace(allow=AsyncMock(return_value=allowed))
    return result


async def test_documents_do_not_consume_image_count_or_visual_rate_bucket(database):
    runtime = (
        await RuntimeConfigService(
            settings=make_settings(database.url), database=database
        ).snapshot()
    ).vision
    runtime = replace(runtime, max_images_per_turn=1)
    reader = service(
        {"a.txt": b"first document", "b.txt": b"second document", "c.txt": b"third document"},
        allowed=False,
    )
    result = await reader.prepare(message("a.txt", "b.txt", "c.txt"), runtime, None)
    assert not result.images
    assert all(
        text in result.documents for text in ("first document", "second document", "third document")
    )
    reader._limiter.allow.assert_not_called()


async def test_image_file_count_does_not_hide_later_documents(database):
    runtime = (
        await RuntimeConfigService(
            settings=make_settings(database.url), database=database
        ).snapshot()
    ).vision
    runtime = replace(runtime, max_images_per_turn=1)
    reader = service(
        {
            "a.png": _image(),
            "a.txt": b"first document",
            "b.png": _image(),
            "b.txt": b"last document",
        }
    )
    inbound = message("a.png", "a.txt", "b.png", "b.txt")
    result = await reader.prepare(inbound, runtime, None)
    assert len(result.images) == 1
    assert "first document" in result.documents and "last document" in result.documents
    assert "图片数量上限" in result.documents
    assert reader._limiter.allow.await_args.kwargs["user_id"] == inbound.person_id
    assert reader._limiter.allow.await_args.kwargs["group_id"] == inbound.space_id


async def test_visual_rate_rejection_still_preserves_readable_document(database):
    runtime = (
        await RuntimeConfigService(
            settings=make_settings(database.url), database=database
        ).snapshot()
    ).vision
    reader = service({"a.png": _image(), "a.txt": b"available document"}, allowed=False)
    result = await reader.prepare(message("a.png", "a.txt"), runtime, None)
    assert not result.images and "available document" in result.documents
    assert "rate_limited" in result.documents


async def test_current_document_process_failure_is_not_invalid_video(database, monkeypatch):
    runtime = (
        await RuntimeConfigService(
            settings=make_settings(database.url), database=database
        ).snapshot()
    ).vision
    monkeypatch.setattr(
        "yuki.services.attachment_inputs.run_parser",
        AsyncMock(side_effect=MediaProcessError()),
    )
    with pytest.raises(VisionProcessingError) as caught:
        await service({"a.txt": b"readable document"}).prepare(message("a.txt"), runtime, None)
    assert caught.value.code == "document_reader_failed"


async def test_history_document_process_failure_is_not_invalid_video(
    database, tmp_path, monkeypatch
):
    reader, source, directory = await _cache(database, tmp_path)
    path = directory / "0-cached.png"
    path.write_bytes(b"original document")
    async with database.immediate_session() as writer:
        event = await writer.get(ChatEventModel, source.id)
        event.segments_json = json.dumps(
            [{"type": "file", "data": {"file": "synthetic-document", "name": "document.txt"}}]
        )
        item = await writer.get(ConversationMediaItemModel, (source.id, 0))
        item.expires_at = datetime.now(UTC) + timedelta(hours=24)
        item.kind, item.display_name, item.content_sha256 = (
            "file",
            "document.txt",
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    monkeypatch.setattr(
        "yuki.conversation.media_service.run_parser",
        AsyncMock(side_effect=MediaProcessError()),
    )
    try:
        with pytest.raises(ConversationMediaError) as caught:
            await reader._inspect(item, path, "read this document")
        assert str(caught.value) == "document_reader_failed"
    finally:
        await reader.close()


async def test_failed_process_retains_its_real_caller_error_kind():
    with pytest.raises(MediaProcessError):
        await run_media_process(sys.executable, "-c", "raise SystemExit(1)")
    with pytest.raises(VisionProcessingError) as caught:
        await run_video_process(sys.executable, "-c", "raise SystemExit(1)")
    assert caught.value.code == "invalid_video"
