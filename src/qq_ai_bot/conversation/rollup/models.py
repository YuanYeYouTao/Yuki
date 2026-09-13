"""Immutable contracts for single-checkpoint rollup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.persistence.repository_records import EventRecord


class RollupKind(StrEnum):
    MODEL = "model"
    EXTRACTIVE = "extractive"
    MIGRATION = "migration"
    EMERGENCY = "emergency"


class RollupJobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"


class EmergencyOverlayDisposition(StrEnum):
    """How to release the job after an overlay write. No schema change."""

    FOREGROUND = "foreground"
    MODEL_FAILURE = "model_failure"
    POLICY = "policy"


LLM_ORIGIN_INELIGIBLE = "llm_origin_ineligible"
# Park policy-ineligible jobs far enough that claim_next will not pick them,
# without using a database infinity. A later force signal sets next_attempt_at=now.
POLICY_PARK_DELAY = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class RollupPolicyConfig:
    raw_tail_events: int = 128
    raw_tail_characters: int = 20_480
    trigger_events: int = 384
    trigger_characters: int = 81_920
    stop_events: int = 0
    stop_characters: int = 0
    batch_max_events: int = 256
    batch_max_characters: int = 32_768
    summary_max_characters: int = 2400
    bot_display_name: str = "Yuki"
    timezone: str = "Asia/Shanghai"
    llm_origins: frozenset[str] = frozenset({"user_message"})

    def __post_init__(self) -> None:
        positive = (
            self.raw_tail_events,
            self.raw_tail_characters,
            self.trigger_events,
            self.trigger_characters,
            self.batch_max_events,
            self.batch_max_characters,
            self.summary_max_characters,
        )
        if any(value < 1 for value in positive):
            raise ValueError("positive rollup settings must be at least one")
        if self.trigger_events < 2:
            raise ValueError("trigger_events must be at least two")
        if self.stop_events < 0 or self.stop_characters < 0:
            raise ValueError("rollup low watermarks must not be negative")
        if self.trigger_events <= self.stop_events:
            raise ValueError("trigger_events must be greater than stop_events")
        if self.trigger_characters <= self.stop_characters:
            raise ValueError("trigger_characters must be greater than stop_characters")
        if not self.llm_origins:
            object.__setattr__(self, "llm_origins", frozenset({"user_message"}))


@dataclass(frozen=True, slots=True)
class ConversationScopeState:
    id: int
    scope: ConversationScope
    generation: int
    starts_after_event_id: int
    last_event_id: int
    last_generation_change_event_id: int
    uncovered_event_count: int
    uncovered_character_count: int
    created_at: datetime
    updated_at: datetime
    runtime_scope_key: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationRollupState:
    scope_id: int
    generation: int
    covered_through_event_id: int
    summary_text: str
    summary_kind: RollupKind
    source_fingerprint: str
    revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RollupJobClaim:
    scope_id: int
    generation: int
    claimed_signal_revision: int
    failure_count: int
    lease_owner: str
    lease_token: str
    lease_until: datetime
    conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class RollupCandidate:
    scope_id: int
    generation: int
    source_coverage: int
    source_rollup_revision: int
    previous_summary: str
    events: tuple[EventRecord, ...]
    event_count: int
    projection_characters: int
    fingerprint: str
    conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationPromptSnapshot:
    scope: ConversationScopeState
    rollup: ConversationRollupState | None
    raw_events: tuple[EventRecord, ...]
    effective_coverage: int
    raw_tail_end_event_id: int
    overlay: ConversationRollupState | None = None
    rewrite_pending: bool = False
    conversation_id: str | None = None
    prompt_source_revision: int = 0
    rollup_stamp: tuple[int, int] = (0, 0)


@dataclass(frozen=True, slots=True)
class RollupCommitResult:
    rollup: ConversationRollupState
    claim_retained: bool


@dataclass(frozen=True, slots=True)
class RollupCheckpointStatus:
    """Metadata-only checkpoint view. Never carries summary text."""

    kind: RollupKind
    revision: int
    covered_through_event_id: int


@dataclass(frozen=True, slots=True)
class RollupJobMetadata:
    status: str
    signal_revision: int
    failure_count: int
    created_at: datetime | None
    last_error_category: str | None


@dataclass(frozen=True, slots=True)
class ConversationRollupDetailedStatus:
    """Transport-neutral rollup observability. Metadata only; no summary content."""

    scope: ConversationScopeState | None
    semantic: RollupCheckpointStatus | None
    overlay: RollupCheckpointStatus | None
    effective_coverage: int
    rewrite_pending: bool
    semantic_uncovered_event_count: int
    semantic_uncovered_character_count: int
    effective_prompt_tail_event_count: int
    effective_prompt_tail_character_count: int
    job: RollupJobMetadata | None
