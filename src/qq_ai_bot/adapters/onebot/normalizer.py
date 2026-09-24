"""Convert OneBot v11 events into transport-independent domain messages."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any, Protocol

from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)

from qq_ai_bot.adapters.onebot.card_parser import parse_card_segment
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from qq_ai_bot.services.renderer import sanitize_input

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
class ProjectedMention:
    """One ordered OneBot mention without exposing its account id in prompt text."""

    kind: str
    segment_index: int
    target_user_id: str | None = None
    member_index: int | None = None


@dataclass(frozen=True, slots=True)
class MentionProjection:
    """Deterministic model-facing projection of one ordered OneBot message."""

    text: str
    mentions_yuki: bool
    attachments: tuple[MessageAttachment, ...]
    mentioned_user_ids: tuple[str, ...]
    ordered_mentions: tuple[ProjectedMention, ...]


class SegmentProjectionError(ValueError):
    """A persisted safe-segment payload cannot be projected deterministically."""


@lru_cache(maxsize=1)
def _default_face_resolver() -> FaceNameResolver:
    from qq_ai_bot.services.qq_face_resolver import QQFaceResolver

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
        key=_optional_string(data.get("key")),
        filename=_optional_string(data.get("name") or data.get("filename")),
    )


def _extract_segments(
    segments: Iterable[MessageSegment],
    *,
    yuki_account_ids: frozenset[str],
    source: str = "current",
    face_resolver: FaceNameResolver | None = None,
) -> MentionProjection:
    text_parts: list[str] = []
    mentions_yuki = False
    attachments: list[MessageAttachment] = []
    mentioned_user_ids: list[str] = []
    member_indices: dict[str, int] = {}
    ordered_mentions: list[ProjectedMention] = []
    resolver = face_resolver or _default_face_resolver()
    for segment_index, segment in enumerate(segments):
        segment_type = segment.type
        data = segment.data
        if segment_type == "text":
            text_parts.append(str(data.get("text", "")))
        elif segment_type == "at":
            target = str(data.get("qq", ""))
            if target == "all":
                text_parts.append("[提及全体成员]")
                ordered_mentions.append(ProjectedMention(kind="all", segment_index=segment_index))
            elif target in yuki_account_ids:
                mentions_yuki = True
                text_parts.append("[提及Yuki]")
                ordered_mentions.append(
                    ProjectedMention(
                        kind="yuki",
                        segment_index=segment_index,
                        target_user_id=target,
                    )
                )
            elif target.isdecimal():
                index = member_indices.get(target)
                if index is None:
                    mentioned_user_ids.append(target)
                    index = len(mentioned_user_ids)
                    member_indices[target] = index
                text_parts.append(f"[提及成员{index}]")
                ordered_mentions.append(
                    ProjectedMention(
                        kind="member",
                        segment_index=segment_index,
                        target_user_id=target,
                        member_index=index,
                    )
                )
        elif segment_type == "face":
            face_id = str(data.get("id", "未知"))
            text_parts.append(f"[QQ表情：{resolver.resolve(face_id)}]")
        elif segment_type == "reply":
            continue
        else:
            card = parse_card_segment(segment_type, data)
            if card is not None:
                text_parts.append(card.text)
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
                text_parts.append(f" [{kind}附件{len(attachments)}{suffix}] ")
            attachments.append(attachment)
    return MentionProjection(
        text=sanitize_input("".join(text_parts)),
        mentions_yuki=mentions_yuki,
        attachments=tuple(attachments),
        mentioned_user_ids=tuple(mentioned_user_ids),
        ordered_mentions=tuple(ordered_mentions),
    )


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
        mentions_yuki = message.mentions_bot or any(
            item in known_yuki for item in message.mentioned_user_ids
        )
        return replace(
            message,
            mentions_bot=mentions_yuki,
            mentioned_user_ids=tuple(
                item for item in message.mentioned_user_ids if item not in known_yuki
            ),
            yuki_account_ids=known_yuki,
        )
    try:
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
    except SegmentProjectionError:
        # Live messages are serialized by this module, so this is only a defensive
        # compatibility path for synthetic callers and pre-3.8 records.
        mentions_yuki = message.mentions_bot or any(
            item in known_yuki for item in message.mentioned_user_ids
        )
        return replace(
            message,
            mentions_bot=mentions_yuki,
            mentioned_user_ids=tuple(
                item for item in message.mentioned_user_ids if item not in known_yuki
            ),
            yuki_account_ids=known_yuki,
        )
    return replace(
        message,
        text=current.text,
        mentions_bot=current.mentions_yuki,
        mentioned_user_ids=current.mentioned_user_ids,
        attachments=current.attachments,
        reply_text=(reply.text or None) if reply is not None else message.reply_text,
        reply_attachments=(reply.attachments if reply is not None else message.reply_attachments),
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
    elif isinstance(event, PrivateMessageEvent):
        scope = ScopeType.PRIVATE
        group_id = None
    else:
        raise TypeError(f"unsupported OneBot event: {type(event).__name__}")

    return InboundMessage(
        message_id=str(event.message_id),
        event_type=f"message:{event.message_type}:{event.sub_type}",
        scope_type=scope,
        sender=SenderIdentity(
            user_id=sender_user_id,
            nickname=event.sender.nickname or "",
            group_card=event.sender.card or "",
            is_bot=sender_user_id in ignored_bot_users,
        ),
        text=projection.text,
        bot_user_id=self_id,
        raw_text=event.raw_message,
        group_id=group_id,
        mentions_bot=projection.mentions_yuki,
        is_self_message=sender_user_id == self_id,
        reply_text=(reply_projection.text or None) if reply_projection is not None else None,
        mentioned_user_ids=projection.mentioned_user_ids,
        attachments=projection.attachments,
        segments=_serialize_segments(event.original_message),
        reply_attachments=reply_projection.attachments if reply_projection is not None else (),
        reply_segments=_serialize_segments(reply_message) if reply_message is not None else (),
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
    )
