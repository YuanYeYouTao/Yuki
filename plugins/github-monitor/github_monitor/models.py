"""Internal normalized GitHub data and the durable singleton-delivery queue."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from yuki_plugin_sdk.models import JsonValue, StrictModel


class RateLimitState(StrictModel):
    remaining: int | None = None
    reset_at: datetime | None = None
    retry_after_seconds: int | None = None
    request_id: str = ""


class GitHubAPIResponse(StrictModel):
    status_code: int
    body: JsonValue = None
    headers: dict[str, str] = Field(default_factory=dict)
    rate_limit: RateLimitState = Field(default_factory=RateLimitState)


class RepositoryState(StrictModel):
    last_event_id: str = ""
    last_event_created_at: datetime | None = None
    etag: str = ""
    last_modified: str = ""
    last_poll_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    paused_until: datetime | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset_at: datetime | None = None
    last_request_id: str = ""
    backlog_truncated: bool = False
    baseline_notified: bool = False


class NormalizedGitHubEvent(StrictModel):
    github_event_id: str
    repository: str
    event_type: str
    actor: str
    created_at: datetime
    action: str = ""
    branch: str = ""
    number: int | None = None
    title: str = ""
    url: str = ""
    summary: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    event_key: str
    is_bot: bool = False
    is_draft: bool = False
    push_before: str = ""
    push_head: str = ""
    push_deleted: bool = False
    legacy_event_key: str = ""


class TargetPolicySnapshot(StrictModel):
    """Delivery policy frozen when a source event enters the durable queue."""

    target_type: Literal["group", "private"]
    target_id: str = Field(min_length=1, max_length=64)
    send_text: bool
    send_card: bool
    ask_agent: bool

    @property
    def target_key(self) -> str:
        return delivery_target_key(self.target_type, self.target_id)


class QueuedSourceEvent(StrictModel):
    """One accepted GitHub API event without retaining the untrusted raw body."""

    github_event_id: str = Field(min_length=1, max_length=128, pattern=r"^[0-9]+$")
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_created_at: datetime | None = None
    normalized: NormalizedGitHubEvent | None = None
    skip_reason: str = Field(default="", max_length=64)
    target_snapshot: tuple[TargetPolicySnapshot, ...] = ()

    @model_validator(mode="after")
    def normalized_identity_matches(self) -> Self:
        if self.normalized is not None and self.normalized.github_event_id != self.github_event_id:
            raise ValueError("queued normalized event id mismatch")
        if (
            self.normalized is not None
            and self.source_created_at is not None
            and self.normalized.created_at != self.source_created_at
        ):
            raise ValueError("queued source timestamp mismatch")
        if (self.normalized is None) == (not self.skip_reason):
            raise ValueError("queued event must be normalized or explicitly skipped")
        keys = tuple(item.target_key for item in self.target_snapshot)
        if len(keys) != len(set(keys)):
            raise ValueError("queued target snapshot must be unique")
        if self.normalized is None and self.target_snapshot:
            raise ValueError("skipped source event cannot retain delivery targets")
        return self


class PreparedMedia(StrictModel):
    index: int = Field(ge=0)
    handle_id: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime | None = None


class PreparedNotification(StrictModel):
    """A byte-equivalent Host request frozen before its first attempt."""

    event_key: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=128)
    external_source: str = Field(min_length=1, max_length=64)
    target_type: Literal["group", "private"]
    target_id: str = Field(min_length=1, max_length=64)
    occurred_at: datetime
    summary: str = Field(min_length=1, max_length=4_000)
    payload_json: str
    text: str = Field(default="", max_length=12_000)
    media: tuple[PreparedMedia, ...] = Field(default=(), max_length=4)
    ask_agent: bool = False
    agent_intent: str = Field(default="", max_length=1_000)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("payload_json")
    @classmethod
    def canonical_payload(cls, value: str) -> str:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("prepared payload must be an object")
        canonical = canonical_json(parsed)
        if value != canonical:
            raise ValueError("prepared payload must use canonical JSON")
        return value

    @model_validator(mode="after")
    def frozen_request_is_consistent(self) -> Self:
        if not self.ask_agent and self.agent_intent:
            raise ValueError("agent_intent requires ask_agent")
        indices = tuple(item.index for item in self.media)
        if indices != tuple(range(len(self.media))):
            raise ValueError("prepared media indices must be contiguous and ordered")
        if self.request_hash != prepared_request_hash(self):
            raise ValueError("prepared request hash mismatch")
        return self


DeliveryStatus = Literal["pending", "prepared", "attempting", "completed", "skipped"]


class TargetDelivery(StrictModel):
    target_key: str = Field(min_length=1, max_length=160)
    target_type: Literal["group", "private"]
    target_id: str = Field(min_length=1, max_length=64)
    send_text: bool
    send_card: bool
    ask_agent: bool
    status: DeliveryStatus = "pending"
    prepared: PreparedNotification | None = None
    notification_id: str = Field(default="", max_length=64)
    source_event_id: int | None = Field(default=None, ge=1)
    completed_at: datetime | None = None
    skipped_reason: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def delivery_state_is_consistent(self) -> Self:
        if self.target_key != delivery_target_key(self.target_type, self.target_id):
            raise ValueError("delivery target key mismatch")
        if self.status == "pending" and self.prepared is not None:
            raise ValueError("pending delivery cannot contain a prepared request")
        if self.status in {"prepared", "attempting", "completed"}:
            if self.prepared is None:
                raise ValueError("delivery status requires a prepared request")
            if (
                self.prepared.target_type != self.target_type
                or self.prepared.target_id != self.target_id
            ):
                raise ValueError("prepared request target mismatch")
        if self.status == "completed" and not self.notification_id:
            raise ValueError("completed delivery requires a notification receipt")
        if self.status == "skipped" and not self.skipped_reason:
            raise ValueError("skipped delivery requires a reason")
        return self


class DeliveryUnit(StrictModel):
    """One sealed FIFO unit. C4 emits singletons; C5 may add adjacent members."""

    unit_id: str = Field(min_length=1, max_length=255)
    members: tuple[QueuedSourceEvent, ...] = Field(min_length=1)
    deliveries: tuple[TargetDelivery, ...]
    sealed_at: datetime

    @model_validator(mode="after")
    def sealed_unit_is_consistent(self) -> Self:
        member_ids = tuple(item.github_event_id for item in self.members)
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("delivery unit members must be unique")
        if member_ids != tuple(sorted(member_ids, key=int)):
            raise ValueError("delivery unit members must use numeric FIFO order")
        target_keys = tuple(item.target_key for item in self.deliveries)
        if len(target_keys) != len(set(target_keys)):
            raise ValueError("delivery unit targets must be unique")
        if len(self.members) > 1 and any(item.status == "pending" for item in self.deliveries):
            raise ValueError("multi-member units must seal prepared target requests")
        return self


class ActivationState(StrictModel):
    activation_id: str = Field(min_length=1, max_length=64)
    occurred_at: datetime
    deliveries: tuple[TargetDelivery, ...]

    @property
    def complete(self) -> bool:
        return all(item.status in {"completed", "skipped"} for item in self.deliveries)


class QueueState(StrictModel):
    """The only authoritative per-repository C4 state value."""

    state_version: Literal[1] = 1
    accepted_cursor: str = ""
    accepted_fingerprint: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    committed_cursor: str = ""
    committed_fingerprint: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    committed_created_at: datetime | None = None
    pending: tuple[QueuedSourceEvent, ...] = ()
    inflight: DeliveryUnit | None = None
    activation: ActivationState | None = None
    legacy_boundary: str = ""
    legacy_imported: bool = False
    etag: str = ""
    last_modified: str = ""
    last_poll_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    paused_until: datetime | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset_at: datetime | None = None
    last_request_id: str = ""
    backlog_pending: bool = False
    rebaseline_mode: Literal["", "baseline", "replay_recent"] = ""
    gap_reason: str = Field(default="", max_length=128)
    gap_at: datetime | None = None

    @field_validator("accepted_cursor", "committed_cursor", "legacy_boundary")
    @classmethod
    def cursor_is_numeric(cls, value: str) -> str:
        if value and not value.isdecimal():
            raise ValueError("GitHub cursor must be numeric")
        return value

    @model_validator(mode="after")
    def queue_invariants(self) -> Self:
        pending_ids = tuple(item.github_event_id for item in self.pending)
        if len(pending_ids) != len(set(pending_ids)):
            raise ValueError("pending source ids must be unique")
        if pending_ids != tuple(sorted(pending_ids, key=int)):
            raise ValueError("pending events must use numeric FIFO order")
        inflight_ids = (
            {item.github_event_id for item in self.inflight.members}
            if self.inflight is not None
            else set()
        )
        if inflight_ids.intersection(pending_ids):
            raise ValueError("inflight members cannot remain pending")
        if inflight_ids and pending_ids:
            if max(map(int, inflight_ids)) >= int(pending_ids[0]):
                raise ValueError("inflight members must precede pending events")
        if self.accepted_cursor and self.committed_cursor:
            if int(self.committed_cursor) > int(self.accepted_cursor):
                raise ValueError("committed cursor cannot exceed accepted cursor")
        if self.committed_cursor and not self.accepted_cursor:
            raise ValueError("committed cursor requires an accepted cursor")
        if self.accepted_fingerprint and not self.accepted_cursor:
            raise ValueError("accepted cursor and fingerprint must be recorded together")
        if self.accepted_cursor and not self.accepted_fingerprint and not self.legacy_imported:
            raise ValueError("accepted cursor requires a source fingerprint")
        if self.committed_fingerprint and not self.committed_cursor:
            raise ValueError("committed cursor and fingerprint must be recorded together")
        if self.committed_cursor and not self.committed_fingerprint and not self.legacy_imported:
            raise ValueError("committed cursor requires a source fingerprint")
        if self.committed_created_at is not None and not self.committed_cursor:
            raise ValueError("committed timestamp requires a committed cursor")
        known_ids = [*pending_ids, *inflight_ids]
        if known_ids and not self.accepted_cursor:
            raise ValueError("queued events require an accepted cursor")
        if self.accepted_cursor and known_ids:
            if min(map(int, known_ids)) <= int(self.committed_cursor or "-1"):
                raise ValueError("queued events must be newer than the committed cursor")
            tail_id = max(known_ids, key=int)
            if tail_id != self.accepted_cursor:
                raise ValueError("accepted cursor must equal the queued tail")
            sources = [*self.pending, *(self.inflight.members if self.inflight else ())]
            tail = next(item for item in sources if item.github_event_id == tail_id)
            if tail.source_fingerprint != self.accepted_fingerprint:
                raise ValueError("accepted fingerprint must equal the queued tail")
        elif self.accepted_cursor != self.committed_cursor:
            raise ValueError("a drained queue requires equal accepted and committed cursors")
        elif self.accepted_fingerprint != self.committed_fingerprint:
            raise ValueError("a drained queue requires equal boundary fingerprints")
        if bool(self.gap_reason) != (self.gap_at is not None):
            raise ValueError("gap reason and timestamp must be recorded together")
        return self


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def delivery_target_key(target_type: str, target_id: str) -> str:
    return f"{target_type}:{target_id}"


def prepared_request_hash(request: PreparedNotification) -> str:
    payload = {
        "event_key": request.event_key,
        "event_type": request.event_type,
        "external_source": request.external_source,
        "target_type": request.target_type,
        "target_id": request.target_id,
        "occurred_at": request.occurred_at.isoformat(),
        "summary": request.summary,
        "payload_json": request.payload_json,
        "text": request.text,
        "media": [
            {
                "index": item.index,
                "handle_id": item.handle_id,
                "sha256": item.sha256,
                "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            }
            for item in request.media
        ],
        "ask_agent": request.ask_agent,
        "agent_intent": request.agent_intent,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_prepared_notification(
    *,
    event_key: str,
    event_type: str,
    target_type: Literal["group", "private"],
    target_id: str,
    occurred_at: datetime,
    summary: str,
    payload: dict[str, JsonValue],
    text: str = "",
    media: tuple[PreparedMedia, ...] = (),
    ask_agent: bool = False,
    agent_intent: str = "",
) -> PreparedNotification:
    normalized_at = (
        occurred_at.replace(tzinfo=UTC)
        if occurred_at.tzinfo is None
        else occurred_at.astimezone(UTC)
    )
    payload_json = canonical_json(payload)
    provisional = PreparedNotification.model_construct(
        event_key=event_key,
        event_type=event_type,
        external_source="github",
        target_type=target_type,
        target_id=target_id,
        occurred_at=normalized_at,
        summary=summary,
        payload_json=payload_json,
        text=text,
        media=media,
        ask_agent=ask_agent,
        agent_intent=agent_intent,
        request_hash="",
    )
    return PreparedNotification(
        event_key=event_key,
        event_type=event_type,
        external_source="github",
        target_type=target_type,
        target_id=target_id,
        occurred_at=normalized_at,
        summary=summary,
        payload_json=payload_json,
        text=text,
        media=media,
        ask_agent=ask_agent,
        agent_intent=agent_intent,
        request_hash=prepared_request_hash(provisional),
    )
