"""Stable event-to-attachment indexing at the canonical ingress boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.gateway.local_clients import LOCAL_CLIENT_PLATFORMS
from yuki.identity.db_models import IdentityBindingModel
from yuki.persistence.models import ConversationMediaItemModel

_IGNORED = frozenset({"text", "at", "mention", "face", "reply", "yuki_context", "agent_target"})
_MEDIA = frozenset({"image", "video", "file", "record", "audio"})
_ORIGIN_KEYS = ("file", "url", "file_id", "media_id")


def attachment_origin(data: Mapping[str, Any]) -> str:
    """Identity of the bytes. Caption text and segment position are not part of it."""
    picked = {
        key: data[key] for key in _ORIGIN_KEYS if isinstance(data.get(key), str) and data[key]
    }
    return json.dumps(picked, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def live_media_data(segments_json: str, attachment_index: int) -> dict[str, Any] | None:
    """Return the media payload at one stable attachment slot."""
    try:
        segments = json.loads(segments_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(segments, list):
        return None
    for index, _position, _kind, data in indexed_media_segments(segments):
        if index == attachment_index and isinstance(data, dict):
            return data
    return None


def segment_origin(segments_json: str, segment_index: int, *, preview: bool = False) -> str:
    """Origin captured with the admitted segment, before later position edits."""
    try:
        segment = json.loads(segments_json)[segment_index]
    except (TypeError, ValueError, IndexError):
        return attachment_origin({})
    data = segment.get("data") if isinstance(segment, dict) else {}
    if not isinstance(data, dict):
        data = {}
    if preview:
        nested = data.get("preview")
        data = nested if isinstance(nested, dict) else {}
    return attachment_origin(data)


def attachment_source_version(data: Mapping[str, Any] | None, snapshot: str | None = None) -> str:
    """Source version for selected pixels. A caption edit does not change it."""
    if snapshot is not None:
        material = snapshot
    else:
        body = data or {}
        material = json.dumps(
            {"origin": attachment_origin(body), "preview": body.get("preview")},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return hashlib.sha256(material.encode()).hexdigest()


def _preview_origin(data: Mapping[str, Any]) -> str:
    preview = data.get("preview")
    if not isinstance(preview, Mapping):
        return attachment_origin({})
    return attachment_origin(preview)


def same_attachment_origin(previous: Any, prepared: Any) -> bool:
    """True when both segments still name the same file and preview.

    Caption text and segment position are not part of this comparison. A
    different preview file is a different source, so the cached original is
    published again with the new preview.
    """
    if not isinstance(previous, dict) or not isinstance(prepared, dict):
        return False
    if previous.get("type") != prepared.get("type"):
        return False
    previous_data = previous.get("data")
    prepared_data = prepared.get("data")
    if not isinstance(previous_data, dict):
        previous_data = {}
    if not isinstance(prepared_data, dict):
        prepared_data = {}
    return attachment_origin(previous_data) == attachment_origin(prepared_data) and (
        _preview_origin(previous_data) == _preview_origin(prepared_data)
    )


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


def segment_snapshot(segment: Mapping[str, Any]) -> str:
    """The one observed segment an attachment slot keeps as its source."""
    return json.dumps(segment, ensure_ascii=False, separators=(",", ":"))


def reply_media_snapshots(segments: Sequence[dict[str, Any]]) -> dict[int, str]:
    """Freeze only the actual observed media segments before entering a writer."""
    return {
        position: segment_snapshot(segments[position])
        for _index, position, _kind, _data in indexed_media_segments(segments)
    }


def _media_item(
    *,
    event_id: int,
    attachment_index: int,
    conversation_id: str,
    generation: int,
    segment_index: int,
    kind: str,
    data: Mapping[str, Any],
    snapshot: str | None,
    now: datetime,
) -> ConversationMediaItemModel:
    raw_name = str(data.get("name") or data.get("filename") or "")
    size = data.get("file_size")
    return ConversationMediaItemModel(
        source_event_id=event_id,
        attachment_index=attachment_index,
        conversation_id=conversation_id,
        generation=generation,
        segment_index=segment_index,
        kind=kind,
        source_snapshot_json=snapshot,
        display_name=raw_name.replace("\\", "/").split("/")[-1],
        declared_size=int(size) if isinstance(size, int) and size >= 0 else None,
        created_at=now,
        cache_status="uncached",
    )


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
            items.append(
                _media_item(
                    event_id=event_id,
                    attachment_index=attachment_index,
                    conversation_id=conversation_id,
                    generation=generation,
                    segment_index=segment_index,
                    kind=kind,
                    data=data,
                    snapshot=snapshots[segment_index] if observed_reply else None,
                    now=now,
                )
            )
    return tuple(items)


def observed_identity(segment: Mapping[str, Any]) -> str:
    """One original across observations: its platform handle, not a rotating signed URL.

    Uses the same origin keys as ``attachment_origin``; ``url`` counts only when
    the segment carries no other handle.
    """
    data = segment.get("data")
    body = data if isinstance(data, Mapping) else {}
    origin = attachment_origin({key: value for key, value in body.items() if key != "url"})
    if origin == attachment_origin({}):
        origin = attachment_origin(body)
    return f"{segment.get('type')}:{origin}"


async def observe_media_slots(
    session: AsyncSession,
    *,
    event_id: int,
    conversation_id: str,
    generation: int,
    segments: Sequence[dict[str, Any]],
    observed: Sequence[tuple[int, dict[str, Any]]],
) -> dict[str, int]:
    """Give originals observed through this receive event a slot after all of its own.

    ``observed`` pairs the event's own segment position with one actually
    returned media segment. The same original keeps its slot; a not yet cached
    slot keeps the latest observed segment (a fresh signed URL), a cached one
    keeps the segment its bytes came from. Only this event's rows are read.
    Returns the slot of each observed segment snapshot.
    """
    existing = (
        await session.scalars(
            select(ConversationMediaItemModel).where(
                ConversationMediaItemModel.source_event_id == event_id
            )
        )
    ).all()
    rows = {
        observed_identity(json.loads(row.source_snapshot_json)): row
        for row in existing
        if row.source_snapshot_json is not None
    }
    following = max(
        [
            sum(segment.get("type") not in _IGNORED for segment in segments),
            *(row.attachment_index + 1 for row in existing),
        ]
    )
    now = datetime.now(UTC)
    slots: dict[str, int] = {}
    for position, segment in observed:
        snapshot = segment_snapshot(segment)
        row = rows.get(observed_identity(segment))
        if row is None:
            data = segment.get("data")
            row = _media_item(
                event_id=event_id,
                attachment_index=following,
                conversation_id=conversation_id,
                generation=generation,
                segment_index=position,
                kind=str(segment.get("type")),
                data=data if isinstance(data, dict) else {},
                snapshot=snapshot,
                now=now,
            )
            session.add(row)
            rows[observed_identity(segment)] = row
            following += 1
        elif row.cache_status == "uncached":
            row.source_snapshot_json = snapshot
        slots[snapshot] = row.attachment_index
    return slots


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
