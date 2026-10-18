"""Stable event-to-attachment indexing at the canonical ingress boundary."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.gateway.local_clients import LOCAL_CLIENT_PLATFORMS
from yuki.identity.db_models import IdentityBindingModel
from yuki.persistence.models import ConversationMediaItemModel

_IGNORED = frozenset({"text", "at", "mention", "face", "reply", "yuki_context"})
_MEDIA = frozenset({"image", "video", "file", "record", "audio"})


def indexed_media_segments(
    segments: Sequence[dict[str, Any]],
    *,
    offset: int = 0,
) -> Iterator[tuple[int, int, str, dict[str, Any]]]:
    """Share original attachment numbering between persistence and current input."""
    attachment_index = offset
    for segment_index, segment in enumerate(segments):
        kind = segment.get("type")
        if kind in _IGNORED:
            continue
        if kind in _MEDIA:
            data = segment.get("data")
            yield attachment_index, segment_index, kind, data if isinstance(data, dict) else {}
        attachment_index += 1


def observed_reply_media_segments(
    current_segments: Sequence[dict[str, Any]], reply_segments: Sequence[dict[str, Any]]
) -> Iterator[tuple[int, int, str, dict[str, Any]]]:
    """Append this receive event's observed reply slots after its own attachments."""
    offset = sum(segment.get("type") not in _IGNORED for segment in current_segments)
    yield from indexed_media_segments(reply_segments, offset=offset)


def reply_media_snapshots(segments: Sequence[dict[str, Any]]) -> dict[int, str]:
    """Freeze only the actual observed media segments before entering a writer."""
    return {
        position: json.dumps(segments[position], ensure_ascii=False, separators=(",", ":"))
        for _index, position, _kind, _data in indexed_media_segments(segments)
    }


def prepare_event_media(
    *,
    event_id: int,
    conversation_id: str,
    generation: int,
    segments: Sequence[dict[str, Any]],
    reply_segments: Sequence[dict[str, Any]] = (),
    reply_snapshots: Mapping[int, str] | None = None,
) -> tuple[ConversationMediaItemModel, ...]:
    """Prepare source positions and metadata without acquiring a writer."""

    now = datetime.now(UTC)
    items = []
    snapshots = (
        reply_media_snapshots(reply_segments) if reply_snapshots is None else reply_snapshots
    )
    sources = (
        (False, indexed_media_segments(segments)),
        (True, observed_reply_media_segments(segments, reply_segments)),
    )
    for observed_reply, positions in sources:
        for attachment_index, segment_index, kind, data in positions:
            raw_name = str(data.get("name") or data.get("filename") or "")
            name = raw_name.replace("\\", "/").split("/")[-1]
            size = data.get("file_size")
            items.append(
                ConversationMediaItemModel(
                    source_event_id=event_id,
                    attachment_index=attachment_index,
                    conversation_id=conversation_id,
                    generation=generation,
                    segment_index=segment_index,
                    kind=kind,
                    source_snapshot_json=snapshots[segment_index] if observed_reply else None,
                    display_name=name,
                    declared_size=int(size) if isinstance(size, int) and size >= 0 else None,
                    created_at=now,
                    cache_status="uncached",
                )
            )
    return tuple(items)


async def index_event_media(
    session: AsyncSession,
    *,
    event_id: int,
    conversation_id: str,
    generation: int,
    segments: Sequence[dict[str, Any]],
    reply_segments: Sequence[dict[str, Any]] = (),
    reply_snapshots: Mapping[int, str] | None = None,
) -> None:
    """Create references in the same short transaction as the source event."""

    for item in prepare_event_media(
        event_id=event_id,
        conversation_id=conversation_id,
        generation=generation,
        segments=segments,
        reply_segments=reply_segments,
        reply_snapshots=reply_snapshots,
    ):
        source = reply_segments if item.source_snapshot_json is not None else segments
        data = source[item.segment_index].get("data")
        if isinstance(data, dict) and data.get("media_id"):
            from yuki.conversation.uploads import owned_upload

            conversation = await session.get(CanonicalConversationModel, conversation_id)
            binding = (
                await session.get(IdentityBindingModel, conversation.identity_binding_id)
                if conversation
                else None
            )
            if binding is not None and binding.platform in LOCAL_CLIENT_PLATFORMS:
                uploaded = await owned_upload(session, binding.person_id, data["media_id"])
                item.cache_status = "cached"
                item.content_sha256 = uploaded["sha256"]
                item.cache_name = f"upload-{uploaded['sha256']}.blob"
                item.cached_at = item.created_at
                item.expires_at = datetime.fromtimestamp(uploaded["expires_at"], UTC)
        session.add(item)
