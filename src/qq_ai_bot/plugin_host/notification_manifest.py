"""Generic canonical publication identity for plugin notification idempotency.

Host compares this immutable manifest on the existing-event path. Media identity
is ordered ``(index, sha256)``; handle ids are not part of the request identity.
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
    """One publication media slot. Identity is index plus stored artifact digest."""

    index: int
    sha256: str


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


def legacy_media_part_key(*, index: int, handle_id: str) -> str:
    """Pre-C3 outbox key. Dual-read only when the stored artifact proves SHA."""

    if index < 0 or not handle_id:
        raise ValueError("legacy media part key is invalid")
    return f"{_MEDIA_PART_PREFIX}{index}:{handle_id}"


def parse_media_part_key(part_key: str) -> tuple[int, str] | None:
    """Return ``(index, token)`` for ``media:{index}:{sha256|handle_id}``."""

    if not part_key.startswith(_MEDIA_PART_PREFIX):
        return None
    rest = part_key[len(_MEDIA_PART_PREFIX) :]
    index_text, separator, token = rest.partition(":")
    if not separator or not token:
        return None
    try:
        index = int(index_text)
    except ValueError:
        return None
    if index < 0:
        return None
    return index, token


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


def manifest_from_stored(
    *,
    plugin_id: str,
    event_key: str,
    occurred_at: datetime,
    summary: str,
    payload: object,
    text: str,
    target_type: str,
    target_id: str,
    external_source: str,
    event_type: str,
    ask_agent: bool,
    resume_waiting_work: bool,
    agent_intent: str,
    media: tuple[MediaIdentity, ...],
) -> PublicationManifest:
    """Rebuild identity from the event row plus existing children. No writes."""

    return PublicationManifest(
        plugin_id=plugin_id,
        event_key=event_key,
        external_source=external_source,
        event_type=event_type,
        target_type=target_type,
        target_id=target_id,
        occurred_at=canonical_occurred_at(occurred_at),
        payload_json=canonical_json(payload),
        summary=summary,
        text=text,
        ask_agent=ask_agent,
        resume_waiting_work=resume_waiting_work,
        agent_intent=agent_intent,
        media=_ordered_media(media),
    )
