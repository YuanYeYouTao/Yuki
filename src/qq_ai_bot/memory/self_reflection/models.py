"""Model-safe contracts for bounded Yuki self-reflection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.memory.enums import MemoryAuthority, MemoryConflictState, MemoryKind
from qq_ai_bot.memory.mutation.models import MemoryMutationOperation
from qq_ai_bot.persistence.repository_records import EventRecord


class SelfReflectionVisibility(StrEnum):
    CURRENT_SCOPE = "current_scope"
    GLOBAL = "global"


class SelfCandidateDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    DEFER = "defer"


class _Contract(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class SelfReflectionEvent(_Contract):
    ref: str = Field(pattern=r"^event_[1-9]\d*$")
    occurred_at: datetime
    direction: str = Field(pattern=r"^(?:inbound|outbound)$")
    author_kind: AuthorKind | None = None
    rendered: str = Field(min_length=1)


class SelfReflectionContextEvent(_Contract):
    ref: str = Field(pattern=r"^context_[1-9]\d*$")
    occurred_at: datetime
    direction: str = Field(pattern=r"^(?:inbound|outbound)$")
    author_kind: AuthorKind | None = None
    rendered: str = Field(min_length=1)


class SelfReflectionToolReceipt(_Contract):
    ref: str = Field(pattern=r"^tool_[1-9]\d*$")
    tool_name: str = Field(min_length=1)
    success: bool
    result_excerpt: str
    occurred_at: datetime | None = None


class SelfReflectionFact(_Contract):
    ref: str = Field(pattern=r"^(?:fact|candidate)_[1-9]\d*$")
    kind: MemoryKind | None = None
    category: str
    memory_key: str
    content: str
    status: str
    authority: MemoryAuthority | None = None
    conflict_state: MemoryConflictState | None = None
    evidence_count: int | None = Field(default=None, ge=0)


class SelfReflectionInput(_Contract):
    source_kind: Literal["chat", "initiative_tools"] = "chat"
    scope_type: ScopeType
    group_id: str | None = Field(default=None)
    private_peer_user_id: str | None = Field(default=None)
    context_events: tuple[SelfReflectionContextEvent, ...] = ()
    events: tuple[SelfReflectionEvent, ...]
    tool_receipts: tuple[SelfReflectionToolReceipt, ...] = ()
    self_facts: tuple[SelfReflectionFact, ...] = ()
    existing_episodes: tuple[SelfReflectionFact, ...] = ()
    self_candidates: tuple[SelfReflectionFact, ...] = ()

    @model_validator(mode="after")
    def _scope_identity(self) -> SelfReflectionInput:
        if self.scope_type is ScopeType.GROUP:
            if self.group_id is None or self.private_peer_user_id is not None:
                raise ValueError("group reflection input requires only group_id")
        elif self.private_peer_user_id is None or self.group_id is not None:
            raise ValueError("private reflection input requires only private_peer_user_id")
        return self


class SelfReflectionProposal(_Contract):
    operation: MemoryMutationOperation | Literal["noop"]
    fact_ref: str | None = Field(default=None, pattern=r"^fact_[1-9]\d*$")
    merge_fact_ref: str | None = Field(default=None, pattern=r"^fact_[1-9]\d*$")
    candidate_ref: str | None = Field(default=None, pattern=r"^candidate_[1-9]\d*$")
    candidate_decision: SelfCandidateDecision | None = None
    evidence_refs: tuple[str, ...] = ()
    visibility: SelfReflectionVisibility = SelfReflectionVisibility.CURRENT_SCOPE
    category: str | None = None
    kind: MemoryKind | None = None
    memory_key: str | None = Field(default=None)
    content: str | None = None
    reason: str = ""
    confidence: float = Field(default=0.85, ge=0, le=1)
    importance: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def _shape(self) -> SelfReflectionProposal:
        if self.candidate_ref is None and self.candidate_decision is not None:
            raise ValueError("candidate decision requires candidate_ref")
        if self.operation == "noop":
            if self.candidate_decision is SelfCandidateDecision.ACCEPT:
                raise ValueError("candidate acceptance requires a memory mutation")
            return self
        if self.candidate_decision in {
            SelfCandidateDecision.REJECT,
            SelfCandidateDecision.DEFER,
        }:
            raise ValueError("candidate rejection or deferral requires noop")
        if not self.evidence_refs:
            raise ValueError("self-reflection mutations require trusted evidence aliases")
        if self.operation is MemoryMutationOperation.CREATE:
            if self.fact_ref or self.merge_fact_ref:
                raise ValueError("create cannot reference an existing fact")
            if not all((self.category, self.kind, self.memory_key, self.content)):
                raise ValueError("create requires category, kind, key, and content")
        elif self.fact_ref is None:
            raise ValueError("existing-fact operation requires fact_ref")
        if self.operation is MemoryMutationOperation.MERGE and self.merge_fact_ref is None:
            raise ValueError("merge requires merge_fact_ref")
        return self


class SelfEpisodePassage(_Contract):
    """One contiguous narrative passage with explicit source bindings."""

    evidence_refs: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "先选择能支持一个核心经历的 event_N/tool_N，再撰写 content。"
            "只选问题不能证明回答内容；聊天中声称查过不能证明工具确实执行。"
            "未选中的窗口消息不是该条正文的证据。"
        ),
    )
    content: str = Field(
        min_length=1,
        description=(
            "只写一个核心经历及其直接进展，保留实际来源支持的经历。"
            "话题无因果关联时选最有价值的一件，舍弃其余；"
            "同一天、同一群或都是自己参与不足以合并。正文须由本条 evidence_refs 支持。"
        ),
    )

    @model_validator(mode="after")
    def _trusted_evidence_aliases(self) -> SelfEpisodePassage:
        if not self.content.strip():
            raise ValueError("episode passage content must not be blank")
        return self


class SelfEpisodeProposal(_Contract):
    passages: tuple[SelfEpisodePassage, ...] = Field(
        min_length=1,
        description=(
            "同一个核心经历的连续叙述片段；每段先绑定直接支持它的来源，再写正文。"
            "提问与回答使用各自来源；旧回复中的外部说法须表述为当时的说法。"
            "不是多个独立经历，不需要凑满片段。后端按顺序连接正文，不另写总述。"
        ),
    )
    importance: int = Field(ge=1, le=5)

    @property
    def content(self) -> str:
        return "\n".join(passage.content for passage in self.passages)

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(ref for part in self.passages for ref in part.evidence_refs))


class SelfReflectionOutput(_Contract):
    proposals: tuple[SelfReflectionProposal, ...] = Field(default=())
    episodes: tuple[SelfEpisodeProposal, ...] = Field(default=())


@dataclass(frozen=True, slots=True)
class SelfReflectionState:
    id: int
    conversation_key_hash: str
    # Creation-time transport provenance only; never use as Yuki's execution identity.
    bot_user_id: str
    canonical_person_id: str | None
    canonical_space_id: str | None
    external_person_id: str | None
    external_space_id: str | None
    last_event_id: int
    latest_event_id: int
    pending_events: int
    pending_characters: int
    pending_since: datetime | None
    has_yuki_reply: bool
    has_tool_result: bool
    high_value_signal: bool


@dataclass(frozen=True, slots=True)
class SelfReflectionBatch:
    state: SelfReflectionState
    events: tuple[EventRecord, ...]
    context_events: tuple[EventRecord, ...]
    trigger_reason: str
    scheduled_slot: str
    run_id: int
    max_input_characters: int
    initiative_run_id: str | None = None
    first_receipt_id: int | None = None
    last_receipt_id: int | None = None
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StoredToolReceipt:
    id: int
    trigger_event_id: int | None
    tool_name: str
    success: bool
    result_excerpt: str
    initiative_run_id: str | None = None
    bot_user_id: str = ""
    occurred_at: datetime | None = None


class SelfReflectionHealth(_Contract):
    """Content-free scheduler state exposed to administrators and health checks."""

    backlog: dict[str, object] = Field(default_factory=dict)
    enabled: bool
    running: bool
    schedule_hours: tuple[int, ...]
    timezone: str
    pending_conversations: int = Field(ge=0)
    calls_today: int = Field(ge=0)
    last_run_status: str | None = None
    last_run_completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SelfReflectionCycleResult:
    """Content-free outcome of one bounded worker cycle."""

    attempted_batches: int = 0
    completed_batches: int = 0
    failed_batches: int = 0
    proposal_count: int = 0
    committed_count: int = 0


@dataclass(frozen=True, slots=True)
class SelfReflectionManualRun:
    """Content-free result returned by the explicit administrator command."""

    attempted_batches: int
    completed_batches: int
    failed_batches: int
    proposal_count: int
    committed_count: int
    health: SelfReflectionHealth
    max_daily_calls: int
