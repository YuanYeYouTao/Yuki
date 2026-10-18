"""Documents use document budgets, and visual choices retain explicit configuration."""

import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from tests.unit.test_conversation_media_cleanup import _cache

from yuki.conversation.media_service import ConversationMediaError
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.services.media_process import MediaProcessError, run_media_process
from yuki.services.video_frames import _run as run_video_process
from yuki.services.vision_service import VisionProcessingError


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
