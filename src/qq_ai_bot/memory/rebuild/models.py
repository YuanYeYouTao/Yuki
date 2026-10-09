"""Strict public models for Memory V2 historical rebuild."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import (
    MemoryRebuildExpiredClaimPolicy,
    MemoryRebuildRunStatus,
    MemoryRebuildThirdPartyMode,
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryRebuildSelection(_Model):
    all_events: bool = False
    bot_user_ids: tuple[str, ...] = ()
    scope_types: tuple[ScopeType, ...] = ()
    sender_user_ids: tuple[str, ...] = ()
    group_ids: tuple[str, ...] = ()
    after: datetime | None = None
    before: datetime | None = None
    minimum_event_id: int | None = Field(default=None, gt=0)
    maximum_event_id: int | None = Field(default=None, gt=0)
    maximum_events: int | None = Field(default=None, gt=0)
    include_failed_live_jobs: bool = False
    third_party_mode: MemoryRebuildThirdPartyMode = MemoryRebuildThirdPartyMode.DISABLED
    expired_claim_policy: MemoryRebuildExpiredClaimPolicy = MemoryRebuildExpiredClaimPolicy.SKIP

    @field_validator("bot_user_ids", "sender_user_ids", "group_ids", mode="before")
    @classmethod
    def _canonical_ids(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list | tuple | set | frozenset):
            raise ValueError("identity filters must be arrays")
        normalized = tuple(sorted({str(item).strip() for item in value if str(item).strip()}))
        if any(not str(item).strip() for item in value):
            raise ValueError("identity filters cannot contain blanks")
        return normalized

    @field_validator("scope_types", mode="before")
    @classmethod
    def _canonical_scopes(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list | tuple | set | frozenset):
            raise ValueError("scope_types must be an array")
        normalized = tuple(sorted({str(getattr(item, "value", item)) for item in value}))
        return normalized

    @field_validator("after", "before", mode="after")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def _bounded(self) -> MemoryRebuildSelection:
        bounds = (
            self.bot_user_ids,
            self.scope_types,
            self.sender_user_ids,
            self.group_ids,
            self.after,
            self.before,
            self.minimum_event_id,
            self.maximum_event_id,
        )
        if not self.all_events and not any(bounds):
            raise ValueError("all_events=false requires at least one range criterion")
        if self.after and self.before and self.after > self.before:
            raise ValueError("after must not be later than before")
        if (
            self.minimum_event_id is not None
            and self.maximum_event_id is not None
            and self.minimum_event_id > self.maximum_event_id
        ):
            raise ValueError("minimum_event_id must not exceed maximum_event_id")
        return self


class MemoryRebuildPlanStatistics(_Model):
    matched_events: int = Field(ge=0)
    eligible_events: int = Field(ge=0)
    already_processed: int = Field(ge=0)
    live_pending_processing: int = Field(ge=0)
    failed_live_jobs: int = Field(ge=0)
    private_events: int = Field(ge=0)
    group_events: int = Field(ge=0)
    input_characters: int = Field(ge=0)
    earliest_event: datetime | None = None
    latest_event: datetime | None = None
    estimated_extraction_requests: int = Field(ge=0)


class MemoryRebuildRun(_Model):
    public_id: str
    status: MemoryRebuildRunStatus
    selection: MemoryRebuildSelection
    selection_hash: str
    snapshot_max_event_id: int = Field(ge=0)
    snapshot_created_at: datetime
    scan_checkpoint_occurred_at: datetime | None = None
    scan_checkpoint_event_id: int | None = None
    commit_checkpoint_event_id: int | None = None
    commit_checkpoint_claim_index: int | None = None
    extraction_fingerprint: str
    plan_statistics: MemoryRebuildPlanStatistics
    error_category: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    review_ready_at: datetime | None = None
    commit_started_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None

    @field_validator(
        "snapshot_created_at",
        "scan_checkpoint_occurred_at",
        "created_at",
        "updated_at",
        "started_at",
        "review_ready_at",
        "commit_started_at",
        "completed_at",
        "cancelled_at",
        mode="after",
    )
    @classmethod
    def _normalize_run_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class MemoryRebuildReviewEntry(_Model):
    proposal_id: int
    event_id: int
    event_time: datetime
    sender_user_id: str
    group_id: str | None
    subject_user_id: str | None
    scope_type: str
    operation: str
    kind: str
    memory_key: str
    content: str
    confidence: float
    authority: str
    valid_from: str | None
    valid_until: str | None
    source_excerpt: str
    review_status: str


class MemoryRebuildHealth(_Model):
    enabled: bool
    planned_runs: int = 0
    extracting_runs: int = 0
    paused_runs: int = 0
    review_runs: int = 0
    committing_runs: int = 0
    failed_runs: int = 0
    oldest_active_run: datetime | None = None
    active_in_flight_calls: int = 0
    pending_items: int = 0
    pending_proposals: int = 0
    failed_items: int = 0
    failed_proposals: int = 0
    last_successful_extraction: datetime | None = None
    last_successful_commit: datetime | None = None
    last_error_category: str | None = None
