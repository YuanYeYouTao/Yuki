"""Canonical conversation identity and public-history snapshot contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from yuki.domain.conversations import ConversationScope
from yuki.persistence.repository_records import EventRecord

if TYPE_CHECKING:
    from yuki.conversation.frozen_fragments import FrozenFragments
    from yuki.conversation.projections import ProjectionSnapshot
    from yuki.conversation.prompt_history import PromptHistorySource
    from yuki.event_prompt import EventPromptReferences


@dataclass(frozen=True, slots=True)
class ConversationScopeState:
    id: str
    scope: ConversationScope
    generation: int
    starts_after_event_id: int
    last_event_id: int
    last_generation_change_event_id: int
    created_at: datetime
    updated_at: datetime
    runtime_scope_key: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationPromptSnapshot:
    scope: ConversationScopeState
    raw_events: tuple[EventRecord, ...]
    checkpoint_text: str
    checkpoint_kind: str | None
    checkpoint_revision: int
    effective_coverage: int
    raw_tail_end_event_id: int
    prompt_source_revision: int
    projection: ProjectionSnapshot | None = None
    source: PromptHistorySource | None = None
    references: EventPromptReferences | None = None
    current_event: EventRecord | None = None
    frozen_prefix: FrozenFragments | None = None


class ConversationCoverageError(RuntimeError):
    """Canonical identity or source changed while preparing public history."""
