"""Native originals cache without eager reads; explicit inspection returns real pixels."""

import asyncio
import gzip
import hashlib
import io
import json
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import update
from tests.conftest import make_settings
from tests.integration.test_telegram_attachment_tools import attachment_tools as attachment_tools

from yuki.admin.config_service import RuntimeConfigService
from yuki.conversation.media_service import ConversationMediaError
from yuki.domain.messages import AttachmentKind, MessageAttachment
from yuki.gateway.providers.telegram_ingress import normalize_message
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.video_frames import sample_video
from yuki.vision.models import DownloadedMedia
from yuki.workspace.inspect import WorkspaceInspector
from yuki.workspace.store import WorkspaceError

TGS = gzip.compress(json.dumps({"v": "5.7", "layers": [], "w": 64, "h": 64}).encode())
TGS_DATA = {
    "name": "sticker.tgs",
    "is_sticker": True,
    "sticker_format": "animated",
    "mime_type": "application/x-tgsticker",
    "summary": "开心",
}


def original(case, *, preview, reply=False):
    sticker = {
        "file_id": "original-bot-a-file",
        "is_animated": True,
        "width": 64,
        "height": 64,
        "emoji": "开心",
    }
    if preview:
        sticker["thumbnail"] = {"file_id": "actual-sticker-preview", "width": 64, "height": 64}
    raw = {
        "message_id": 1,
        "date": 1792368000,
        "chat": {"id": -10042, "type": "supergroup"},
        "from": {"id": 42},
        "sticker": sticker,
    }
    if reply:
        raw = {**raw, "message_id": 2, "text": "看这张贴纸", "reply_to_message": raw}
        del raw["sticker"]
    message = normalize_message(raw, case.original_client)
    return case.media.prepare_message(
        replace(
            message,
            source_event_id=case.source.id,
            conversation_id=case.source.canonical_conversation_id,
            person_id=case.source.author_person_id,
            **({"reply_segments": case.source.segments} if reply else {}),
        ),
        case.original_client,
        source_segments=case.source.segments,
    )


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
async def test_tgs_current_uses_shared_real_preview_and_keeps_original_cache(
    attachment_tools, database, monkeypatch
):
    case = attachment_tools
    inbound = original(case, preview=True)
    analyze = AsyncMock(side_effect=AssertionError("ordinary Main must not call auxiliary Vision"))
    monkeypatch.setattr("yuki.vision.qwen.QwenVisionProvider.analyze", analyze)
    await case.media._cache_event(case.source.id, case.original_client, inbound)
    assert case.wire.count(("GET", "/file/botfixture-a/photos/preview.png")) == 1
    prepared = await case.workspace.execute(
        "inspect_conversation_attachment",
        {"event_id": case.source.id, "attachment_index": 0, "question": "查看贴纸预览"},
        runtime=case.runtime,
        request_id="explicit-current-preview",
    )
    assert len(prepared.images) == 1 and prepared["preview"] == "platform_static_thumbnail"
    assert prepared["summary"] == "开心"
    assert inbound.attachments[0].kind is AttachmentKind.FILE
    assert inbound.attachments[0].preview.prepared_media is not None
    item, path = await case.media.authorized_path(
        event_id=case.source.id,
        attachment_index=0,
    )
    assert path.read_bytes() == TGS and item.content_sha256 == hashlib.sha256(TGS).hexdigest()
    assert case.wire.count(("GET", "/file/botfixture-a/photos/original.png")) == 1
    assert case.wire.count(("GET", "/file/botfixture-a/photos/preview.png")) == 1
    analyze.assert_not_called()


@pytest.mark.parametrize(
    "attachment_tools", [{"content": TGS, "kind": "file", "data": TGS_DATA}], indirect=True
)
async def test_tgs_without_thumbnail_keeps_native_facts_without_document_failure(
    attachment_tools, database
):
    case = attachment_tools
    inbound = original(case, preview=False)
    assert inbound.attachments[0].is_sticker and case.wire == []
    result = await case.workspace.execute(
        "inspect_conversation_attachment",
        {"event_id": case.source.id, "attachment_index": 0, "question": "看一下贴纸"},
        runtime=case.runtime,
        request_id="native-sticker-without-preview",
    )
    assert result["mode"] == "sticker_metadata"
    assert result["status"] == "preview_unavailable" and result["summary"] == "开心"


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
async def test_tgs_history_preview_uses_original_presence_and_original_ttl(
    attachment_tools, database
):
    case = attachment_tools
    args = {"event_id": case.source.id, "attachment_index": 0, "question": "看一下贴纸"}
    result = await case.workspace.execute(
        "inspect_conversation_attachment", args, runtime=case.runtime, request_id="tgs-preview"
    )
    assert result["mode"] == "sticker_preview" and result["preview"] == "platform_static_thumbnail"
    assert len(result.images) == 1 and result.images[0].source_event_id == case.source.id
    assert result["sha256"] == hashlib.sha256(TGS).hexdigest()
    assert all("fixture-b" not in path for _, path in case.wire)
    assert case.wire.count(("GET", "/file/botfixture-a/photos/preview.png")) == 1
    async with database.immediate_session() as session:
        event = await session.get(ChatEventModel, case.source.id)
        segments = json.loads(event.segments_json)
        segments[0]["data"]["preview"]["file"] = "edited-preview"
        event.segments_json = json.dumps(segments)
    with pytest.raises(ConversationMediaError, match="attachment_source_changed"):
        await case.media.validate_images(result.images)
    async with database.immediate_session() as session:
        await session.execute(
            update(ConversationMediaItemModel)
            .where(ConversationMediaItemModel.source_event_id == case.source.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await case.media.cleanup()
    case.wire.clear()
    with pytest.raises(WorkspaceError, match="attachment_expired"):
        await case.workspace.execute(
            "inspect_conversation_attachment",
            args,
            runtime=case.runtime,
            request_id="expired-preview",
        )
    assert case.wire == []


async def make_webm(tmp_path):
    path = tmp_path / "telegram-sticker.webm"
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=32x32:r=10",
            "-t",
            "0.4",
            "-an",
            "-c:v",
            "libvpx-vp9",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path.read_bytes()


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="requires local ffmpeg"
)
async def test_actual_webm_is_sampled_to_pixels_without_auxiliary_model(tmp_path):
    content = await make_webm(tmp_path)
    frames = await sample_video(
        DownloadedMedia(
            content=content,
            content_type="video/webm",
            content_hash=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
        ),
        source="current",
        maximum=2,
    )
    assert 1 <= len(frames) <= 2
    assert all(frame.data_url.startswith("data:image/jpeg;base64,") for frame in frames)
    assert frames[0].video_timestamp_seconds == 0


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="requires local ffmpeg"
)
@pytest.mark.parametrize("attachment_tools", [{"kind": "video"}, {"kind": "file"}], indirect=True)
async def test_actual_webm_current_cache_history_and_workspace_share_decoding(
    attachment_tools, database, tmp_path, monkeypatch
):
    case = attachment_tools
    content = await make_webm(tmp_path)
    case.media_content["original"] = content
    field = "sticker" if case.source.segments[0]["type"] == "video" else "document"
    normalized = normalize_message(
        {
            "message_id": 1,
            "date": 1792368000,
            "chat": {"id": -10042, "type": "supergroup"},
            "from": {"id": 42},
            field: {
                "file_id": "original-bot-a-file",
                "file_name": "real-sticker.webm",
                "is_video": True,
                "width": 32,
                "height": 32,
                "mime_type": "video/webm",
            },
        },
        case.original_client,
    )
    inbound = case.media.prepare_message(
        replace(
            normalized,
            source_event_id=case.source.id,
            conversation_id=case.source.canonical_conversation_id,
            person_id=case.source.author_person_id,
        ),
        case.original_client,
        source_segments=case.source.segments,
    )
    runtime = (
        await RuntimeConfigService(
            settings=make_settings(database.url), database=database
        ).snapshot()
    ).vision
    analyze = AsyncMock(side_effect=AssertionError("Main and explicit tools do not call Vision"))
    monkeypatch.setattr("yuki.vision.qwen.QwenVisionProvider.analyze", analyze)
    await case.media._cache_event(case.source.id, case.original_client, inbound)
    result = await case.workspace.execute(
        "inspect_conversation_attachment",
        {"event_id": case.source.id, "attachment_index": 0, "question": "查看原视频贴纸"},
        runtime=case.runtime,
        request_id="webm-history",
    )
    assert result["mode"] == "video_frames" and result.images
    assert case.wire.count(("GET", "/file/botfixture-a/photos/original.png")) == 1
    selected = await WorkspaceInspector(case.store, ImagePreprocessor())(
        None,
        "查看保存的视频贴纸",
        runtime=runtime,
        file_metadata={
            "path": "/workspace/stickers/telegram/files/real.webm",
            "version": hashlib.sha256(content).hexdigest(),
        },
        data=content,
    )
    assert selected["mode"] == "video_frames" and selected.images
    analyze.assert_not_called()


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
async def test_reply_tgs_preview_retains_reply_source_and_media_owner(attachment_tools, database):
    case = attachment_tools
    inbound = original(case, preview=True, reply=True)
    assert not inbound.attachments and len(inbound.reply_attachments) == 1
    attachment = inbound.reply_attachments[0]
    assert attachment.source == attachment.preview.source == "reply"
    assert attachment.prepared_media is not None and attachment.preview.prepared_media is not None
    assert not case.wire
    await case.media._cache(
        case.source.id, 0, case.original_client, prepared_media=attachment.prepared_media
    )
    result = await case.workspace.execute(
        "inspect_conversation_attachment",
        {"event_id": case.source.id, "attachment_index": 0, "question": "查看引用贴纸"},
        runtime=case.runtime,
        request_id="explicit-reply-preview",
    )
    assert result.images[0].source_event_id == case.source.id
    assert result["preview"] == "platform_static_thumbnail"
    assert case.wire.count(("GET", "/file/botfixture-a/photos/preview.png")) == 1
    assert case.wire.count(("GET", "/file/botfixture-a/photos/original.png")) == 1


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
async def test_preview_download_failure_does_not_misclassify_native_tgs(
    attachment_tools, database, monkeypatch
):
    from yuki.services.media_resolver import MediaResolutionError

    case = attachment_tools
    inbound = original(case, preview=True)
    resolve = case.original_client.resolve_media_file

    async def fail_preview(file, kind, *, max_download_bytes):
        if file == "actual-sticker-preview":
            raise MediaResolutionError("resource_unavailable", "preview unavailable")
        return await resolve(file, kind, max_download_bytes=max_download_bytes)

    monkeypatch.setattr(case.original_client, "resolve_media_file", fail_preview)
    await case.media._cache_event(case.source.id, case.original_client, inbound)
    item, path = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
    with pytest.raises(WorkspaceError, match="resource_unavailable"):
        await case.workspace.execute(
            "inspect_conversation_attachment",
            {"event_id": case.source.id, "attachment_index": 0, "question": "查看贴纸预览"},
            runtime=case.runtime,
            request_id="failed-explicit-preview",
        )
    assert path.read_bytes() == TGS and item.content_sha256 == hashlib.sha256(TGS).hexdigest()


@pytest.mark.parametrize("metadata", [{}, {"mime_type": "video/mp4", "file_name": "wrong.mp4"}])
@pytest.mark.parametrize("attachment_tools", [{"kind": "video"}], indirect=True)
async def test_telegram_gif_animation_reuses_bytes_for_current_cache_history_and_workspace(
    attachment_tools, database, metadata, monkeypatch
):
    case = attachment_tools
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(
        output,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (8, 8), "blue")],
        duration=100,
        loop=0,
    )
    content = output.getvalue()
    case.media_content["original"] = content
    inbound = normalize_message(
        {
            "message_id": 1,
            "date": 1792368000,
            "chat": {"id": -10042, "type": "supergroup"},
            "from": {"id": 42},
            "animation": {"file_id": "original-bot-a-file", **metadata},
        },
        case.original_client,
    )
    inbound = case.media.prepare_message(
        inbound, case.original_client, source_segments=case.source.segments
    )
    runtime = (
        await RuntimeConfigService(
            settings=make_settings(database.url), database=database
        ).snapshot()
    ).vision
    analyze = AsyncMock(side_effect=AssertionError("ordinary input must not buy Vision"))
    monkeypatch.setattr("yuki.vision.qwen.QwenVisionProvider.analyze", analyze)
    await case.media._cache_event(case.source.id, case.original_client, inbound)
    item, path = await case.media.authorized_path(
        event_id=case.source.id,
        attachment_index=0,
    )
    assert path.suffix == ".gif" and path.read_bytes() == content
    inspected = await case.media.inspect(item, path, "看这张动画", runtime=runtime)
    assert inspected["mode"] == "image" and inspected.images
    selected = await WorkspaceInspector(case.store, ImagePreprocessor())(
        None,
        "看这张动画",
        runtime=runtime,
        file_metadata={
            "path": "/workspace/wrong.mp4",
            "version": hashlib.sha256(content).hexdigest(),
        },
        data=content,
    )
    assert selected.images
    assert case.wire.count(("GET", "/file/botfixture-a/photos/original.png")) == 1
    analyze.assert_not_called()


@pytest.mark.parametrize(
    "attachment_tools", [{"kind": "file", "data": {"name": "x" * 100 + ".txt"}}], indirect=True
)
async def test_historical_truncated_filename_reads_original_suffix(attachment_tools, database):
    case = attachment_tools
    case.media_content["original"] = b"original text from a long filename"
    async with database.immediate_session() as writer:
        item = await writer.get(ConversationMediaItemModel, (case.source.id, 0))
        assert item.display_name.endswith(".txt") and len(item.display_name) > 80
        item.display_name = item.display_name[
            :80
        ]  # Existing databases already contain this truncation.
    result = await case.workspace.execute(
        "inspect_conversation_attachment",
        {"event_id": case.source.id, "attachment_index": 0, "question": "读取文本"},
        runtime=case.runtime,
        request_id="long-filename-history",
    )
    assert result["mode"] == "document"
    assert "original text" in result["result"]["text"]


@pytest.mark.parametrize(
    "attachment_tools",
    [
        {
            "content": TGS,
            "kind": "file",
            "data": {
                **TGS_DATA,
                "preview": {"file": "actual-sticker-preview"},
            },
        }
    ],
    indirect=True,
)
async def test_cached_tgs_preview_cannot_switch_to_another_live_bot(attachment_tools):
    case = attachment_tools
    await case.media.authorized_path(
        event_id=case.source.id,
        attachment_index=0,
    )
    case.registry.disconnect(case.original_client)
    case.wire.clear()
    with pytest.raises(WorkspaceError, match="resource_unavailable"):
        await case.workspace.execute(
            "inspect_conversation_attachment",
            {"event_id": case.source.id, "attachment_index": 0, "question": "看一下贴纸"},
            runtime=case.runtime,
            request_id="offline-original-preview",
        )
    assert not case.wire


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize(
    "attachment_tools", [{"content": TGS, "kind": "file", "data": TGS_DATA}], indirect=True
)
async def test_workspace_tgs_returns_real_metadata_without_decoder_or_model(
    attachment_tools, monkeypatch, published
):
    from tests.support.workspace_snapshots import snapshot_bytes

    case = attachment_tools
    decode = AsyncMock(side_effect=AssertionError("TGS has no direct image pixels"))
    provider = AsyncMock(side_effect=AssertionError("explicit workspace read must not buy Vision"))
    inspector = WorkspaceInspector(case.store, ImagePreprocessor())
    monkeypatch.setattr(inspector.preparer, "prepare_image", decode)
    monkeypatch.setattr("yuki.vision.qwen.QwenVisionProvider.analyze", provider)
    if published:
        artifact = snapshot_bytes(case.store, "original.tgs", TGS)
        result = await inspector(artifact["artifact_id"], "看这张贴纸")
        assert case.store.read_bytes(artifact["artifact_id"])[1] == TGS
    else:
        result = await inspector(
            None,
            "看这张贴纸",
            data=TGS,
            file_metadata={
                "path": "/workspace/original.tgs",
                "version": hashlib.sha256(TGS).hexdigest(),
            },
        )
    assert result["mode"] == "sticker_metadata" and result["status"] == "preview_unavailable"
    assert result["format"] == "TGS" and result["width"] == result["height"] == 64
    assert not result.images and result["sha256"] == hashlib.sha256(TGS).hexdigest()
    decode.assert_not_called()
    provider.assert_not_called()


@pytest.mark.parametrize(
    "attachment_tools",
    [
        {
            "content": TGS,
            "kind": "image",
            "data": {"name": "sticker", "mime_type": None},
        }
    ],
    indirect=True,
)
async def test_original_tgs_bytes_survive_image_label_without_platform_metadata(
    attachment_tools, database, monkeypatch
):
    case = attachment_tools
    async with database.sessions() as session:
        event = await session.get(ChatEventModel, case.source.id)
        frozen_segments = event.segments_json
    inbound = replace(
        case.runtime.inbound,
        attachments=(MessageAttachment(AttachmentKind.IMAGE, "image", file="original-bot-a-file"),),
    )
    inbound = case.media.prepare_message(
        inbound, case.original_client, source_segments=case.source.segments
    )
    runtime = (
        await RuntimeConfigService(
            settings=make_settings(database.url), database=database
        ).snapshot()
    ).vision
    analyze = AsyncMock(side_effect=AssertionError("native file input must not buy Vision"))
    monkeypatch.setattr("yuki.vision.qwen.QwenVisionProvider.analyze", analyze)
    decode = AsyncMock(side_effect=AssertionError("raw TGS cannot become image pixels"))
    monkeypatch.setattr(case.media.preparer, "prepare_image", decode)
    await case.media._cache_event(case.source.id, case.original_client, inbound)
    item, path = await case.media.authorized_path(
        event_id=case.source.id,
        attachment_index=0,
    )
    assert path.suffix == ".gz" and path.read_bytes() == TGS
    result = await case.media.inspect(item, path, "读取原件", runtime=runtime)
    assert result["mode"] == "sticker_metadata" and result["status"] == "preview_unavailable"
    assert result["format"] == "TGS" and result["sha256"] == hashlib.sha256(TGS).hexdigest()
    assert case.wire.count(("GET", "/file/botfixture-a/photos/original.png")) == 1
    async with database.sessions() as session:
        event = await session.get(ChatEventModel, case.source.id)
        assert event.segments_json == frozen_segments
    analyze.assert_not_called()
    decode.assert_not_called()
