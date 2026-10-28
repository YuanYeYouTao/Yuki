"""Convert OneBot v11 events into transport-independent domain messages."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Protocol

from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)

from yuki.adapters.onebot.card_parser import parse_card_segment
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    MessageMention,
    SenderIdentity,
)
from yuki.services.renderer import sanitize_input

_ATTACHMENT_TYPES: dict[str, AttachmentKind] = {
    "image": AttachmentKind.IMAGE,
    "record": AttachmentKind.AUDIO,
    "video": AttachmentKind.VIDEO,
    "file": AttachmentKind.FILE,
    "forward": AttachmentKind.FORWARD,
    "node": AttachmentKind.FORWARD,
    "xml": AttachmentKind.CARD,
    "json": AttachmentKind.CARD,
}


class FaceNameResolver(Protocol):
    """Small adapter boundary used to keep normalization deterministic."""

    def resolve(self, face_id: str | int) -> str:
        """Return a readable name, or an ``ID <value>`` fallback."""


@dataclass(frozen=True, slots=True)
class MentionProjection:
    """Deterministic model-facing projection of one ordered OneBot message."""

    text: str
    mentions_yuki: bool
    attachments: tuple[MessageAttachment, ...]
    mentioned_user_ids: tuple[str, ...]
    ordered_mentions: tuple[MessageMention, ...]


class SegmentProjectionError(ValueError):
    """A persisted safe-segment payload cannot be projected deterministically."""


@lru_cache(maxsize=1)
def _default_face_resolver() -> FaceNameResolver:
    from yuki.services.qq_face_resolver import QQFaceResolver

    return QQFaceResolver()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _optional_integer(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, str | int | float):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _attachment_from_segment(
    segment: MessageSegment,
    *,
    segment_index: int,
    source: str,
    summary: str | None = None,
    url: str | None = None,
) -> MessageAttachment:
    data = segment.data
    return MessageAttachment(
        kind=_ATTACHMENT_TYPES.get(segment.type, AttachmentKind.UNKNOWN),
        label=segment.type,
        segment_index=segment_index,
        source=source,
        file=_optional_string(data.get("id") if segment.type == "forward" else data.get("file")),
        url=url or _optional_string(data.get("url")),
        summary=summary or _optional_string(data.get("summary")),
        sub_type=_optional_string(data.get("sub_type")),
        file_size=_optional_integer(data.get("file_size")),
        emoji_id=_optional_string(data.get("emoji_id")),
        emoji_package_id=_optional_string(data.get("emoji_package_id")),
        is_sticker=segment.type == "image" and _sticker_metadata(data),
        sticker_format=_optional_string(data.get("sticker_format")),
        mime_type=_optional_string(data.get("mime_type")),
        width=_optional_integer(data.get("width")),
        height=_optional_integer(data.get("height")),
        key=_optional_string(data.get("key")),
        filename=_optional_string(data.get("name") or data.get("filename")),
    )


def _sticker_metadata(data: dict[str, Any]) -> bool:
    return (
        data.get("is_sticker") is True
        or bool(data.get("emoji_id") or data.get("emoji_package_id"))
        or str(data.get("sub_type", "")).casefold()
        in {"1", "2", "emoji", "market_face", "marketface", "sticker", "meme"}
    )


def _extract_segments(
    segments: Iterable[MessageSegment],
    *,
    yuki_account_ids: frozenset[str],
    source: str = "current",
    face_resolver: FaceNameResolver | None = None,
) -> MentionProjection:
    text_parts: list[str] = []
    text_length = 0
    seen_user_ids: set[str] = set()
    mentions_yuki = False
    attachments: list[MessageAttachment] = []
    mentioned_user_ids: list[str] = []
    ordered_mentions: list[MessageMention] = []
    resolver = face_resolver or _default_face_resolver()

    def append_text(value: str) -> None:
        nonlocal text_length
        text_parts.append(value)
        text_length += len(value)

    for segment_index, segment in enumerate(segments):
        segment_type = segment.type
        data = segment.data
        if segment_type == "text":
            append_text(str(data.get("text", "")))
        elif segment_type == "at":
            target = str(data.get("qq", ""))
            if target == "all":
                kind, marker, name = "all", "[提及全体成员]", "全体成员"
            elif target in yuki_account_ids:
                mentions_yuki = True
                kind, marker, name = "yuki", "[提及Agent]", str(data.get("name") or "")
            elif target.isdecimal():
                if target not in seen_user_ids:
                    seen_user_ids.add(target)
                    mentioned_user_ids.append(target)
                kind, marker, name = "member", f"[提及{target}]", str(data.get("name") or "")
            else:
                continue
            append_text(marker)
            end = text_length
            ordered_mentions.append(
                MessageMention(
                    kind=kind,
                    segment_index=segment_index,
                    text_start=end - len(marker),
                    text_end=end,
                    target_user_id=None if target == "all" else target,
                    display_name=name,
                )
            )
        elif segment_type == "face":
            face_id = str(data.get("id", "未知"))
            append_text(f"[QQ表情：{resolver.resolve(face_id)}]")
        elif segment_type == "reply":
            continue
        else:
            card = parse_card_segment(segment_type, data)
            if card is not None:
                append_text(card.text)
            attachment = _attachment_from_segment(
                segment,
                segment_index=segment_index,
                source=source,
                summary=card.summary if card is not None else None,
                url=card.url if card is not None else None,
            )
            if attachment.kind in {AttachmentKind.IMAGE, AttachmentKind.VIDEO, AttachmentKind.FILE}:
                kind = {
                    AttachmentKind.IMAGE: "图片",
                    AttachmentKind.VIDEO: "视频",
                    AttachmentKind.FILE: "文件",
                }[attachment.kind]
                name = (attachment.filename or "").replace("\\", "/").split("/")[-1]
                name = sanitize_input(name)[:80].strip(" []\r\n\t")
                suffix = f"：{name}" if name else ""
                append_text(f" [{kind}附件{len(attachments)}{suffix}] ")
            attachments.append(attachment)
    text, ordered = _sanitize_mention_text("".join(text_parts), ordered_mentions)
    return MentionProjection(
        text=text,
        mentions_yuki=mentions_yuki,
        attachments=tuple(attachments),
        mentioned_user_ids=tuple(mentioned_user_ids),
        ordered_mentions=ordered,
    )


def _sanitize_mention_text(
    text: str, mentions: list[MessageMention]
) -> tuple[str, tuple[MessageMention, ...]]:
    if not mentions:
        return sanitize_input(text), ()
    characters: list[str] = []
    origins: list[int] = []
    index = 0
    while index < len(text):
        character = text[index]
        origin = index
        index += 1
        if character == "\r":
            if index < len(text) and text[index] == "\n":
                index += 1
            character = "\n"
        if (ord(character) < 32 and character not in {"\t", "\n"}) or character == "\x7f":
            continue
        characters.append(character)
        origins.append(origin)
    normalized = "".join(characters)
    output: list[str] = []
    positions: list[int] = []
    offset = 0
    lines = normalized.splitlines(keepends=True)
    for line_index, line in enumerate(lines):
        body = line.splitlines()[0] if line.splitlines() else ""
        body = body.rstrip()
        output.extend(body)
        positions.extend(origins[offset : offset + len(body)])
        if line_index + 1 < len(lines):
            output.append("\n")
            positions.append(origins[offset + len(line) - 1])
        offset += len(line)
    left, right = 0, len(output)
    while left < right and output[left].isspace():
        left += 1
    while right > left and output[right - 1].isspace():
        right -= 1
    positions = positions[left:right]
    projected = []
    cursor = 0
    for item in mentions:
        while cursor < len(positions) and positions[cursor] < item.text_start:
            cursor += 1
        start = cursor
        while cursor < len(positions) and positions[cursor] < item.text_end:
            cursor += 1
        projected.append(replace(item, text_start=start, text_end=cursor))
    return "".join(output[left:right]), tuple(projected)


def _message_from_serialized_segments(
    segments: Iterable[dict[str, object]],
) -> tuple[MessageSegment, ...]:
    converted: list[MessageSegment] = []
    for item in segments:
        if not isinstance(item, dict):
            raise SegmentProjectionError("segment_not_object")
        segment_type = item.get("type")
        data = item.get("data")
        if not isinstance(segment_type, str) or not segment_type:
            raise SegmentProjectionError("segment_type_invalid")
        if not isinstance(data, dict):
            raise SegmentProjectionError("segment_data_invalid")
        converted.append(MessageSegment(segment_type, dict(data)))
    return tuple(converted)


def project_serialized_segments(
    segments: Iterable[dict[str, object]],
    *,
    yuki_account_ids: frozenset[str],
    source: str = "history",
    face_resolver: FaceNameResolver | None = None,
) -> MentionProjection:
    """Project ledger-safe OneBot segments with the same rules as live input."""

    return _extract_segments(
        _message_from_serialized_segments(segments),
        yuki_account_ids=yuki_account_ids,
        source=source,
        face_resolver=face_resolver,
    )


def reproject_inbound_mentions(
    message: InboundMessage,
    yuki_account_ids: frozenset[str],
    *,
    face_resolver: FaceNameResolver | None = None,
) -> InboundMessage:
    """Reclassify mentions after canonical ingress discovers every Yuki Presence."""

    known_yuki = frozenset(
        item.strip()
        for item in (*message.yuki_account_ids, *yuki_account_ids, message.bot_user_id)
        if item and item.strip()
    )
    if not message.segments:
        return replace(message, yuki_account_ids=known_yuki)
    current = project_serialized_segments(
        message.segments,
        yuki_account_ids=known_yuki,
        source="current",
        face_resolver=face_resolver,
    )
    reply = (
        project_serialized_segments(
            message.reply_segments,
            yuki_account_ids=known_yuki,
            source="reply",
            face_resolver=face_resolver,
        )
        if message.reply_segments
        else None
    )
    return replace(
        message,
        text=current.text,
        mentions_bot=current.mentions_yuki,
        mentioned_user_ids=current.mentioned_user_ids,
        mentions=current.ordered_mentions,
        attachments=current.attachments,
        reply_text=(reply.text or None) if reply is not None else message.reply_text,
        reply_attachments=(reply.attachments if reply is not None else message.reply_attachments),
        reply_mentions=(reply.ordered_mentions if reply is not None else message.reply_mentions),
        yuki_account_ids=known_yuki,
    )


def _json_value(value: Any) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return str(value)


def _serialize_segments(message: Message) -> tuple[dict[str, object], ...]:
    """Preserve safe media/message metadata without downloading payloads."""

    serialized: list[dict[str, object]] = []
    for segment in message:
        data = dict(segment.data)
        if segment.type == "image":
            data["is_sticker"] = _sticker_metadata(data)
        if segment.type in {"image", "record"}:
            for key in ("file", "url", "base64"):
                value = data.get(key)
                if key == "base64" and value is not None:
                    data[key] = "[inline-image-omitted]"
                elif isinstance(value, str) and value.lstrip().casefold().startswith(
                    ("base64://", "data:image/", "data:audio/")
                ):
                    data[key] = "[inline-image-omitted]"
        serialized.append(
            {
                "type": segment.type,
                "data": _json_value(data),
            }
        )
    return tuple(serialized)


def normalize_event(
    event: MessageEvent,
    *,
    ignored_bot_users: frozenset[str] = frozenset(),
    yuki_account_ids: frozenset[str] = frozenset(),
    face_resolver: FaceNameResolver | None = None,
) -> InboundMessage:
    """Normalize a private or group OneBot message without downloading attachments."""

    self_id = str(event.self_id)
    known_yuki = frozenset({self_id, *yuki_account_ids})
    projection = _extract_segments(
        event.original_message,
        yuki_account_ids=known_yuki,
        face_resolver=face_resolver,
    )
    sender_user_id = str(event.sender.user_id or event.user_id)
    reply_message = event.reply.message if event.reply is not None else None
    reply_projection = (
        _extract_segments(
            reply_message,
            yuki_account_ids=known_yuki,
            source="reply",
            face_resolver=face_resolver,
        )
        if reply_message is not None
        else None
    )

    if isinstance(event, GroupMessageEvent):
        scope = ScopeType.GROUP
        group_id: str | None = str(event.group_id)
        group_name = getattr(event, "group_name", "")
    elif isinstance(event, PrivateMessageEvent):
        scope = ScopeType.PRIVATE
        group_id = None
        group_name = ""
    else:
        raise TypeError(f"unsupported OneBot event: {type(event).__name__}")

    return InboundMessage(
        ingress_key=str(event.message_id),
        native_message_id=str(event.message_id),
        event_type=f"message:{event.message_type}:{event.sub_type}",
        scope_type=scope,
        sender=SenderIdentity(
            user_id=sender_user_id,
            nickname=event.sender.nickname or "",
            group_card=event.sender.card or "",
            is_bot=sender_user_id in ignored_bot_users,
            nickname_known=(
                "nickname" in event.sender.model_fields_set
                and isinstance(event.sender.nickname, str)
            ),
            group_card_known=(
                group_id is not None
                and "card" in event.sender.model_fields_set
                and isinstance(event.sender.card, str)
            ),
        ),
        text=projection.text,
        bot_user_id=self_id,
        raw_text=event.raw_message,
        group_id=group_id,
        group_name=group_name if isinstance(group_name, str) else "",
        mentions_bot=projection.mentions_yuki,
        is_self_message=sender_user_id == self_id,
        reply_text=(reply_projection.text or None) if reply_projection is not None else None,
        mentioned_user_ids=projection.mentioned_user_ids,
        mentions=projection.ordered_mentions,
        attachments=projection.attachments,
        segments=_serialize_segments(event.original_message),
        reply_attachments=reply_projection.attachments if reply_projection is not None else (),
        reply_segments=_serialize_segments(reply_message) if reply_message is not None else (),
        reply_mentions=reply_projection.ordered_mentions if reply_projection is not None else (),
        reply_to_message_id=(
            str(event.reply.message_id)
            if event.reply is not None and event.reply.message_id is not None
            else None
        ),
        reply_sender_user_id=(
            str(event.reply.sender.user_id)
            if event.reply is not None and event.reply.sender.user_id is not None
            else None
        ),
        yuki_account_ids=known_yuki,
        occurred_at=datetime.fromtimestamp(event.time, UTC) if event.time is not None else None,
    )
