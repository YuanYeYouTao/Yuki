"""Native sticker facts survive ingress and route through platform send planning."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from tests.unit.test_normalizer import private_event

from yuki.adapters.onebot.normalizer import normalize_event, project_serialized_segments
from yuki.domain.messages import AttachmentKind
from yuki.gateway.providers.telegram_ingress import normalize_message
from yuki.gateway.providers.telegram_social import TelegramSocialOperations
from yuki.gateway.social import TransportMedia, TransportMessage, TransportTarget


@pytest.mark.parametrize("metadata", [{"sub_type": 1}, {"emoji_id": "native"}])
def test_qq_sticker_metadata_survives_original_event_projection(metadata):
    message = Message([MessageSegment("image", {"file": "original", **metadata})])
    current = normalize_event(private_event(message))
    historical = project_serialized_segments(current.segments, yuki_account_ids=frozenset())
    assert current.attachments[0].is_sticker
    assert historical.attachments[0].is_sticker
    assert current.segments[0]["data"]["is_sticker"] is True


def test_qq_ordinary_image_and_builtin_face_do_not_invent_sticker_files():
    message = Message([MessageSegment.face(1), MessageSegment.image("ordinary")])
    current = normalize_event(private_event(message))
    assert len(current.attachments) == 1
    assert not current.attachments[0].is_sticker


@pytest.mark.parametrize(
    ("flags", "kind", "format", "mime"),
    [
        ({}, AttachmentKind.IMAGE, "static", "image/webp"),
        ({"is_animated": True}, AttachmentKind.FILE, "animated", "application/x-tgsticker"),
        ({"is_video": True}, AttachmentKind.VIDEO, "video", "video/webm"),
    ],
)
def test_telegram_current_and_reply_stickers_keep_original_format(flags, kind, format, mime):
    original = {
        "message_id": 21,
        "date": 1792368000,
        "chat": {"id": -1001, "type": "supergroup"},
        "from": {"id": 42, "first_name": "Original"},
        "sticker": {"file_id": "original", "width": 64, "height": 64, "emoji": "笑", **flags},
    }
    raw = {**original, "message_id": 22, "reply_to_message": original}
    normalized = normalize_message(raw, SimpleNamespace(self_id="999", username="yuki"))
    assert normalized is not None
    attachment = normalized.attachments[0]
    assert attachment.is_sticker and attachment.kind is kind
    assert attachment.sticker_format == format and attachment.mime_type == mime
    assert attachment.summary == "笑" and attachment.width == 64
    assert normalized.reply_attachments == (replace(attachment, source="reply"),)
    assert normalized.reply_segments[0]["data"]["sticker_format"] == format
    assert normalized.reply_to_message_id == "-1001:21"


@pytest.mark.parametrize(
    ("mime", "method", "field"),
    [
        ("image/webp", "sendSticker", "sticker"),
        ("application/x-tgsticker", "sendSticker", "sticker"),
        ("video/webm", "sendSticker", "sticker"),
        ("image/gif", "sendAnimation", "animation"),
        ("image/png", "sendPhoto", "photo"),
    ],
)
async def test_telegram_sticker_upload_preserves_real_content_and_method(mime, method, field):
    client = SimpleNamespace(call=AsyncMock(return_value={"message_id": 23}))
    media = TransportMedia("image", b"original", "sticker", mime, True)
    receipt = await TelegramSocialOperations().send(
        client, TransportTarget("space", "-1001"), TransportMessage(media=media)
    )
    args, kwargs = client.call.await_args
    assert args[0] == method and args[1][field] == f"attach://{field}"
    assert kwargs["files"][field][1:] == (b"original", mime)
    assert "caption" not in args[1] and "parse_mode" not in args[1]
    assert receipt.reference == "-1001:23"


def test_telegram_captionless_sticker_is_planned_after_receipted_text():
    media = TransportMedia("image", b"original", "sticker.webp", "image/webp", True)
    parts = TelegramSocialOperations().plan_send(
        TransportMessage(text="正文", media=media, reply_reference="-1001:21")
    )
    assert len(parts) == 2
    assert parts[0].text == "正文" and parts[0].media is None
    assert parts[0].reply_reference == "-1001:21"
    assert parts[1].media is media and not parts[1].text and parts[1].reply_reference is None


async def test_telegram_explicit_gif_file_keeps_document_upload_semantics():
    client = SimpleNamespace(call=AsyncMock(return_value={"message_id": 24}))
    media = TransportMedia("file", b"original-gif", "original.gif", "image/gif")
    operations = TelegramSocialOperations()
    message = TransportMessage(text="文件附言", media=media)
    assert operations.plan_send(message) == (message,)
    await operations.send(client, TransportTarget("person", "42"), message)
    args, kwargs = client.call.await_args
    assert args[0] == "sendDocument" and args[1]["caption"] == "文件附言"
    assert kwargs["files"]["document"] == ("original.gif", b"original-gif", "image/gif")
