"""Stable event-to-attachment indexing at the canonical ingress boundary."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.persistence.models import ConversationMediaItemModel

_IGNORED = frozenset({"text", "at", "face", "reply"})
_MEDIA = frozenset({"image", "video", "file"})


async def index_event_media(
    session: AsyncSession,
    *,
    event_id: int,
    conversation_id: str,
    generation: int,
    segments: Sequence[dict[str, Any]],
) -> None:
    """Create references in the same short transaction as the source event."""

    attachment_index = 0
    now = datetime.now(UTC)
    for segment_index, segment in enumerate(segments):
        kind = segment.get("type")
        if kind in _IGNORED:
            continue
        if kind in _MEDIA:
            data = segment.get("data")
            if not isinstance(data, dict):
                data = {}
            raw_name = str(data.get("name") or data.get("filename") or "")
            name = raw_name.replace("\\", "/").split("/")[-1][:80]
            size = data.get("file_size")
            session.add(
                ConversationMediaItemModel(
                    source_event_id=event_id,
                    attachment_index=attachment_index,
                    conversation_id=conversation_id,
                    generation=generation,
                    segment_index=segment_index,
                    kind=kind,
                    display_name=name,
                    declared_size=int(size) if isinstance(size, int) and size >= 0 else None,
                    created_at=now,
                    cache_status="uncached",
                )
            )
        attachment_index += 1
