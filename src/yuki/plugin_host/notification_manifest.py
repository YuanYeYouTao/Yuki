"""Generic canonical publication identity for plugin notification idempotency.

Host compares this immutable manifest on the existing-event path. Modern media
identity is ordered ``(index, sha256)``. A legacy receipt whose bytes were cleaned
can still prove the exact original handle, without claiming an unknown digest.
This module has no plugin-vendor or source-specific concepts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from yuki_plugin_sdk.models import PublishNotificationRequest

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MEDIA_PART_PREFIX = "media:"


@dataclass(frozen=True, slots=True)
class MediaIdentity:
    """A stored digest, or an exact legacy handle when bytes are no longer retained."""

    index: int
    sha256: str | None
    legacy_handle_id: str | None = None


@dataclass(frozen=True, slots=True)
class PublicationManifest:
    """Byte-stable identity of one whole ``publish()`` request."""

    plugin_id: str
    event_key: str
    external_source: str
    event_type: str
    target_type: str
    target_id: str
    occurred_at: str
    payload_json: str
    summary: str
    text: str
    ask_agent: bool
    resume_waiting_work: bool
    agent_intent: str
    media: tuple[MediaIdentity, ...]


def canonical_json(value: object) -> str:
    """Encode JSON identity with sorted keys and compact separators."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_occurred_at(value: datetime) -> str:
    """Normalize request and stored timestamps to a UTC ISO-8601 token."""

    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat()


def is_sha256_digest(value: str) -> bool:
    return bool(_SHA256.fullmatch(value))


def media_part_key(*, index: int, sha256: str) -> str:
    if index < 0:
        raise ValueError("media index is invalid")
    if not is_sha256_digest(sha256):
        raise ValueError("media sha256 digest is invalid")
    return f"{_MEDIA_PART_PREFIX}{index}:{sha256}"


def _ordered_media(media: tuple[MediaIdentity, ...]) -> tuple[MediaIdentity, ...]:
    return tuple(sorted(media, key=lambda item: item.index))


def manifest_from_request(
    request: PublishNotificationRequest,
    *,
    plugin_id: str,
    media: tuple[MediaIdentity, ...],
) -> PublicationManifest:
    """Build the incoming request identity. Intent is stored only with ask_agent."""

    return PublicationManifest(
        plugin_id=plugin_id,
        event_key=request.event_key,
        external_source=request.external_source,
        event_type=request.event_type,
        target_type=request.target.target_type,
        target_id=request.target.target_id,
        occurred_at=canonical_occurred_at(request.occurred_at),
        payload_json=canonical_json(request.payload),
        summary=request.summary,
        text=request.text,
        ask_agent=request.ask_agent,
        resume_waiting_work=request.resume_waiting_work,
        agent_intent=request.agent_intent,
        media=_ordered_media(media),
    )
