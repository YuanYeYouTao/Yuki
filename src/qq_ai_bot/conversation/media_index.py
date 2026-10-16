"""Stable event-to-attachment indexing at the canonical ingress boundary."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.db_models import IdentityBindingModel
from qq_ai_bot.persistence.models import ConversationMediaItemModel

_IGNORED = frozenset({"text", "at", "face", "reply"})
_MEDIA = frozenset({"image", "video", "file"})


def prepare_event_media(
    *,
    event_id: int,
    conversation_id: str,
    generation: int,
    segments: Sequence[dict[str, Any]],
) -> tuple[ConversationMediaItemModel, ...]:
    """Prepare source positions and metadata without acquiring a writer."""

    attachment_index = 0
    now = datetime.now(UTC)
    items = []
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
            item = ConversationMediaItemModel(
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
            items.append(item)
        attachment_index += 1
    return tuple(items)


async def index_event_media(
    session: AsyncSession,
    *,
    event_id: int,
    conversation_id: str,
    generation: int,
    segments: Sequence[dict[str, Any]],
) -> None:
    """Create references in the same short transaction as the source event."""

    for item in prepare_event_media(
        event_id=event_id,
        conversation_id=conversation_id,
        generation=generation,
        segments=segments,
    ):
        data = segments[item.segment_index].get("data")
        if isinstance(data, dict) and data.get("media_id"):
            from qq_ai_bot.conversation.uploads import owned_upload

            conversation = await session.get(CanonicalConversationModel, conversation_id)
            binding = (
                await session.get(IdentityBindingModel, conversation.identity_binding_id)
                if conversation
                else None
            )
            if binding is not None and binding.platform == "app":
                uploaded = await owned_upload(session, binding.person_id, data["media_id"])
                item.cache_status = "cached"
                item.content_sha256 = uploaded["sha256"]
                item.cache_name = f"upload-{uploaded['sha256']}.blob"
                item.cached_at = item.created_at
                item.expires_at = datetime.fromtimestamp(uploaded["expires_at"], UTC)
        session.add(item)
