"""Old IMAGE sticker records use real WebM bytes without rewriting their source."""

import hashlib
import shutil
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from tests.conftest import make_settings
from tests.integration.test_telegram_attachment_tools import attachment_tools as attachment_tools
from tests.integration.test_telegram_sticker_inputs import make_webm

from yuki.admin.config_service import RuntimeConfigService
from yuki.domain.messages import AttachmentKind, MessageAttachment
from yuki.persistence.models import ChatEventModel
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.workspace.inspect import WorkspaceInspector

pytestmark = [
    pytest.mark.skipif(
        not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
        reason="actual WebM frames require ffmpeg/ffprobe",
    ),
    pytest.mark.parametrize(
        "attachment_tools", [{"kind": "image", "data": {"name": "sticker"}}], indirect=True
    ),
]


async def prepare_old_message(case, database, tmp_path, *, reply):
    content = await make_webm(tmp_path)
    case.media_content["original"] = content
    attachment = MessageAttachment(
        AttachmentKind.IMAGE,
        "sticker",
        segment_index=0,
        source="reply" if reply else "current",
        file="original-bot-a-file",
        filename="sticker",
        mime_type=None,
    )
    inbound = replace(
        case.runtime.inbound,
        bot_user_id=case.original_client.self_id,
        presence_id=case.source.ingress_presence_id,
        source_event_id=case.source.id,
        person_id=case.source.author_person_id,
        text="查看贴纸",
        segments=() if reply else case.source.segments,
        attachments=() if reply else (attachment,),
        reply_segments=case.source.segments if reply else (),
        reply_attachments=(attachment,) if reply else (),
    )
    inbound = case.media.prepare_message(
        inbound, case.original_client, source_segments=inbound.segments
    )
    runtime = (
        await RuntimeConfigService(
            settings=make_settings(database.url), database=database
        ).snapshot()
    ).vision
    return inbound, runtime, content


async def test_legacy_image_webm_current_cache_history_share_real_original(
    attachment_tools, database, tmp_path, monkeypatch
):
    case = attachment_tools
    async with database.sessions() as session:
        original_segments = (await session.get(ChatEventModel, case.source.id)).segments_json
    inbound, runtime, content = await prepare_old_message(case, database, tmp_path, reply=False)
    analyze = AsyncMock(side_effect=AssertionError("ordinary media never buys auxiliary Vision"))
    monkeypatch.setattr("yuki.vision.qwen.QwenVisionProvider.analyze", analyze)
    await case.media._cache_event(case.source.id, case.original_client, inbound)
    item, path = await case.media.authorized_path(
        event_id=case.source.id,
        attachment_index=0,
    )
    assert path.suffix == ".webm" and path.read_bytes() == content
    history = await case.workspace.execute(
        "inspect_conversation_attachment",
        {"event_id": case.source.id, "attachment_index": 0, "question": "看之前的贴纸"},
        runtime=case.runtime,
        request_id="legacy-webm-history",
    )
    assert history["mode"] == "video_frames" and history.images
    assert history.images[0].video_timestamp_seconds == 0
    assert history["sha256"] == item.content_sha256 == hashlib.sha256(content).hexdigest()
    workspace = await WorkspaceInspector(case.store, ImagePreprocessor())(
        None,
        "看保存的旧贴纸",
        runtime=runtime,
        file_metadata={
            "path": "/workspace/sticker",
            "version": hashlib.sha256(content).hexdigest(),
        },
        data=content,
    )
    assert workspace["mode"] == "video_frames" and workspace.images
    assert case.wire.count(("GET", "/file/botfixture-a/photos/original.png")) == 1
    assert all("fixture-b" not in path for _, path in case.wire)
    async with database.sessions() as session:
        event = await session.get(ChatEventModel, case.source.id)
        assert event.segments_json == original_segments
    analyze.assert_not_called()


async def test_legacy_image_webm_reply_uses_real_frames_and_one_original_download(
    attachment_tools, database, tmp_path, monkeypatch
):
    case = attachment_tools
    async with database.sessions() as session:
        original_segments = (await session.get(ChatEventModel, case.source.id)).segments_json
    inbound, _runtime, _content = await prepare_old_message(case, database, tmp_path, reply=True)
    analyze = AsyncMock(side_effect=AssertionError("reply media never buys auxiliary Vision"))
    monkeypatch.setattr("yuki.vision.qwen.QwenVisionProvider.analyze", analyze)
    assert not case.wire
    await case.media._cache(
        case.source.id,
        0,
        case.original_client,
        prepared_media=inbound.reply_attachments[0].prepared_media,
    )
    result = await case.workspace.execute(
        "inspect_conversation_attachment",
        {"event_id": case.source.id, "attachment_index": 0, "question": "看引用原视频"},
        runtime=case.runtime,
        request_id="explicit-legacy-webm-reply",
    )
    assert result.images and result.images[0].video_timestamp_seconds == 0
    assert all(frame.source_event_id == case.source.id for frame in result.images)
    assert case.wire.count(("GET", "/file/botfixture-a/photos/original.png")) == 1
    assert all("fixture-b" not in path for _, path in case.wire)
    async with database.sessions() as session:
        event = await session.get(ChatEventModel, case.source.id)
        assert event.segments_json == original_segments
    analyze.assert_not_called()
