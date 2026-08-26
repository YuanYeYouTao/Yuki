"""Pure, deterministic planning for safe adjacent GitHub event batches."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from yuki_plugin_sdk.models import JsonValue

from .events import event_key_for_boundary
from .models import (
    NormalizedGitHubEvent,
    QueuedSourceEvent,
    TargetPolicySnapshot,
    canonical_json,
)

BATCH_EVENT_TYPE = "github_event_batch"
BATCH_KEY_VERSION = "v1"
MAX_HOST_PAYLOAD_BYTES = 32 * 1024
_COALESCIBLE_TYPES = frozenset({"CreateEvent", "DeleteEvent", "WatchEvent", "ForkEvent"})
_TEXT_LIMIT = 12_000


@dataclass(frozen=True, slots=True)
class AggregateProjection:
    event_key: str
    occurred_at: datetime
    summary: str
    text: str
    payload: dict[str, JsonValue]


class AggregatePayloadTooLarge(ValueError):
    """The deterministic aggregate cannot fit the Host notification contract."""


def plan_adjacent_members(
    pending: Sequence[QueuedSourceEvent],
    *,
    coalesce: bool,
    limit: int,
) -> tuple[QueuedSourceEvent, ...]:
    """Select one immutable FIFO prefix without crossing an incompatible event."""

    if not pending or limit < 1:
        return ()
    first = pending[0]
    if not coalesce or first.normalized is None:
        return (first,)
    if first.normalized.event_type not in _COALESCIBLE_TYPES:
        return (first,)
    members = [first]
    for candidate in pending[1:limit]:
        if not _can_coalesce(first, candidate):
            break
        members.append(candidate)
    return tuple(members)


def build_aggregate_projection(
    members: tuple[QueuedSourceEvent, ...],
    *,
    legacy_boundary: str,
) -> AggregateProjection:
    """Build a body-free Host request projection from a sealed member list."""

    if len(members) < 2:
        raise ValueError("aggregate projection requires multiple members")
    events = tuple(item.normalized for item in members)
    if any(event is None for event in events):
        raise ValueError("aggregate members must contain normalized events")
    normalized = tuple(event for event in events if event is not None)
    first = normalized[0]
    if any(not _can_coalesce(members[0], item) for item in members[1:]):
        raise ValueError("aggregate members are not mutually compatible")
    singleton_keys = tuple(event_key_for_boundary(event, legacy_boundary) for event in normalized)
    summary = _aggregate_summary(normalized)
    lines = tuple(_member_line(event) for event in normalized)
    payload: dict[str, JsonValue] = {
        "aggregate_version": BATCH_KEY_VERSION,
        "repository": first.repository,
        "event_type": first.event_type,
        "count": len(normalized),
        "source_event_ids": [event.github_event_id for event in normalized],
        "content_trust": "external_untrusted",
    }
    if first.event_type in {"CreateEvent", "DeleteEvent"}:
        payload["grouping"] = {
            "actor": first.actor,
            "ref_type": str(first.payload.get("ref_type", "")),
        }
    if len(canonical_json(payload).encode("utf-8")) > MAX_HOST_PAYLOAD_BYTES:
        raise AggregatePayloadTooLarge("aggregate payload exceeds Host limit")
    return AggregateProjection(
        event_key=_batch_event_key(first.repository, singleton_keys),
        occurred_at=_utc(normalized[-1].created_at),
        summary=summary,
        text=_bounded_text(summary, lines),
        payload=payload,
    )


def _can_coalesce(first: QueuedSourceEvent, candidate: QueuedSourceEvent) -> bool:
    left = first.normalized
    right = candidate.normalized
    if left is None or right is None:
        return False
    if left.event_type not in _COALESCIBLE_TYPES or right.event_type != left.event_type:
        return False
    if left.repository.casefold() != right.repository.casefold():
        return False
    if not _same_targets(first.target_snapshot, candidate.target_snapshot):
        return False
    if left.event_type in {"CreateEvent", "DeleteEvent"}:
        return left.actor.casefold() == right.actor.casefold() and str(
            left.payload.get("ref_type", "")
        ) == str(right.payload.get("ref_type", ""))
    return True


def _same_targets(
    left: tuple[TargetPolicySnapshot, ...],
    right: tuple[TargetPolicySnapshot, ...],
) -> bool:
    return left == right


def _batch_event_key(repository: str, singleton_keys: tuple[str, ...]) -> str:
    member_digest = hashlib.sha256(canonical_json(list(singleton_keys)).encode("utf-8")).hexdigest()
    raw = f"github:{repository}:batch:{BATCH_KEY_VERSION}:{member_digest}"
    if len(raw) <= 255:
        return raw
    repository_digest = hashlib.sha256(repository.casefold().encode("utf-8")).hexdigest()[:24]
    return f"github:batch:{BATCH_KEY_VERSION}:{repository_digest}:{member_digest}"


def _aggregate_summary(events: tuple[NormalizedGitHubEvent, ...]) -> str:
    first = events[0]
    repository = first.repository
    count = len(events)
    if first.event_type in {"CreateEvent", "DeleteEvent"}:
        verb = "创建" if first.event_type == "CreateEvent" else "删除"
        ref_type = str(first.payload.get("ref_type", ""))
        label = {"branch": "分支", "tag": "标签"}.get(ref_type, "引用")
        return f"{repository}：{first.actor} 连续{verb}了 {count} 个{label}"
    if first.event_type == "WatchEvent":
        return f"{repository} 收到 {count} 个新的 Star"
    return f"{repository} 被 Fork 了 {count} 次"


def _member_line(event: NormalizedGitHubEvent) -> str:
    if event.event_type in {"CreateEvent", "DeleteEvent"}:
        detail = event.branch or "未命名引用"
    else:
        detail = event.actor or "unknown"
    return f"- #{event.github_event_id} {detail}"


def _bounded_text(summary: str, lines: tuple[str, ...]) -> str:
    complete = "\n".join((summary, *lines))
    if len(complete) <= _TEXT_LIMIT:
        return complete
    kept = [summary]
    for index, line in enumerate(lines):
        omitted = len(lines) - index
        suffix = f"- 其余 {omitted} 条已省略；source event IDs 已完整保留"
        candidate = "\n".join((*kept, line, suffix))
        if len(candidate) > _TEXT_LIMIT:
            kept.append(suffix)
            break
        kept.append(line)
    return "\n".join(kept)[:_TEXT_LIMIT]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
