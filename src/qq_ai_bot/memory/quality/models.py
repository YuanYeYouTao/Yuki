"""Strict, content-safe contracts for deterministic Memory V2 quality evaluation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _QualityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QualitySuiteMode(StrEnum):
    STRUCTURAL = "structural"
    PIPELINE = "pipeline"
    RETRIEVAL = "retrieval"
    CONTEXT = "context"
    REBUILD = "rebuild"
    FULL = "full"


class QualityEvent(_QualityModel):
    event_ref: str = Field(pattern=r"^event_[a-z0-9_]+$")
    speaker: str
    scope_type: str = Field(pattern=r"^(private|group)$")
    content: str = ""
    group: str | None = None
    direction: str = Field(default="inbound", pattern=r"^(inbound|outbound)$")
    occurred_at: datetime
    mentioned: tuple[str, ...] = ()
    reply_speaker: str | None = None

    @model_validator(mode="after")
    def _scope(self) -> QualityEvent:
        if (self.scope_type == "group") != bool(self.group):
            raise ValueError("group events require group and private events forbid it")
        if self.occurred_at.tzinfo is None:
            raise ValueError("quality event timestamps must include a timezone")
        return self


class QualityClaim(_QualityModel):
    event_ref: str
    operation: str = "assert"
    subject_ref: str = "speaker"
    scope_type: str = "person"
    kind: str = "fact"
    memory_key: str
    category: str
    content: str
    evidence_quote: str
    importance: int = Field(default=3, ge=1, le=5)
    confidence: float = Field(default=0.9, ge=0, le=1)
    source_type: str = "automatic"
    subject_basis: str = "omitted_self"
    retention: str = "durable"
    source_style: str = "natural_statement"
    value_reason: str = "Synthetic fixture declares a stable fact useful for future recall."
    temporal_mode: str = "persistent"
    valid_from: str | None = None
    valid_until: str | None = None


class QualityFactSpec(_QualityModel):
    fact_ref: str = Field(pattern=r"^fact_[a-z0-9_]+$")
    scope_type: str
    subject: str | None = None
    group: str | None = None
    kind: str = "fact"
    memory_key: str
    category: str = "quality"
    content: str
    status: str = "active"
    conflict_state: str = "clear"
    source_type: str = "automatic"
    authority: str = "self_report"
    importance: int = Field(default=3, ge=1, le=5)
    confidence: float = Field(default=0.9, ge=0, le=1)
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def _scope(self) -> QualityFactSpec:
        if self.scope_type == "person" and (not self.subject or self.group):
            raise ValueError("person facts require only subject")
        if self.scope_type == "person_group" and (not self.subject or not self.group):
            raise ValueError("person_group facts require subject and group")
        if self.scope_type == "group" and (self.subject or not self.group):
            raise ValueError("group facts require only group")
        return self


class QualityEvidenceSpec(_QualityModel):
    fact_ref: str
    event_ref: str
    speaker: str
    excerpt: str
    relation: str


class QualityRelationSpec(_QualityModel):
    source_fact_ref: str
    target_fact_ref: str
    relation_type: str


class QualityQuerySpec(_QualityModel):
    query_ref: str = Field(pattern=r"^query_[a-z0-9_]+$")
    text: str
    scope_type: str
    subject: str | None = None
    group: str | None = None
    expected_fact_refs: tuple[str, ...] = ()
    forbidden_fact_refs: tuple[str, ...] = ()
    limit: int = Field(default=5, gt=0, le=100)
    semantic: bool = False
    context: bool = True
    requester: str | None = None


class QualityRebuildExpectation(_QualityModel):
    committed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    receipts: int = Field(default=0, ge=0)
    historical_regressions: int = Field(default=0, ge=0)


class MemoryQualityCase(_QualityModel):
    schema_version: str = "1"
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    category: str
    description: str
    events: tuple[QualityEvent, ...] = ()
    fake_model_outputs: tuple[QualityClaim, ...] = ()
    initial_facts: tuple[QualityFactSpec, ...] = ()
    initial_relations: tuple[QualityRelationSpec, ...] = ()
    initial_state_events: tuple[dict[str, str], ...] = ()
    queries: tuple[QualityQuerySpec, ...] = ()
    expected_claims: tuple[str, ...] = ()
    expected_facts: tuple[QualityFactSpec, ...] = ()
    expected_evidence: tuple[QualityEvidenceSpec, ...] = ()
    expected_relations: tuple[QualityRelationSpec, ...] = ()
    expected_retrieval: tuple[str, ...] = ()
    expected_context: tuple[str, ...] = ()
    forbidden_facts: tuple[str, ...] = ()
    forbidden_context: tuple[str, ...] = ()
    expected_rebuild: QualityRebuildExpectation | None = None
    tags: tuple[str, ...] = ()
    historical_memberships: tuple[tuple[str, str], ...] = ()

    @field_validator("events")
    @classmethod
    def _unique_events(cls, value: tuple[QualityEvent, ...]) -> tuple[QualityEvent, ...]:
        refs = [item.event_ref for item in value]
        if len(refs) != len(set(refs)):
            raise ValueError("event_ref values must be unique within a case")
        return value

    @model_validator(mode="after")
    def _non_empty(self) -> MemoryQualityCase:
        if not (self.events or self.initial_facts or self.queries or self.expected_rebuild):
            raise ValueError("quality cases must exercise at least one pipeline surface")
        return self


class QualityManifest(_QualityModel):
    schema_version: str = "1"
    suite_version: str
    read_policy_version: str
    case_files: tuple[str, ...]
    dataset_hash: str
    symbolic_identities: dict[str, str]
    fake_model_id: str
    fake_embedding_id: str


class QualityObservation(_QualityModel):
    case_id: str
    claims: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    relations: tuple[str, ...] = ()
    retrieval: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    rebuild: QualityRebuildExpectation | None = None
    extraction_requests: int = Field(default=0, ge=0)
    consolidation_requests: int = Field(default=0, ge=0)
    query_embedding_requests: int = Field(default=0, ge=0)
    context_characters: int = Field(default=0, ge=0)
    extraction_latency_ms: float = Field(default=0, ge=0)
    retrieval_latency_ms: tuple[float, ...] = ()
    context_latency_ms: tuple[float, ...] = ()
    error_code: str | None = None


class QualityCaseResult(_QualityModel):
    case_id: str
    category: str
    passed: bool
    failures: tuple[str, ...] = ()
    observation: QualityObservation


class QualityMetricValue(_QualityModel):
    value: float | None
    numerator: float
    denominator: float
    unit: str = "ratio"


class QualityGateResult(_QualityModel):
    metric: str
    passed: bool
    observed: float | None
    operator: str
    threshold: float
    detail: str = ""


class MemoryQualityReport(_QualityModel):
    schema_version: str = "1"
    suite_version: str
    suite_mode: QualitySuiteMode
    commit: str
    python_version: str
    sqlite_version: str
    dataset_hash: str
    gate_config_hash: str
    fake_model_id: str
    fake_embedding_id: str
    deterministic: bool = True
    model_provider_id: str | None = None
    embedding_provider_id: str | None = None
    started_at: datetime
    duration_seconds: float = Field(ge=0)
    case_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    cases: tuple[QualityCaseResult, ...]
    metrics: dict[str, QualityMetricValue]
    gates: tuple[QualityGateResult, ...]
    baseline_regressions: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            self.failed_count == 0
            and all(item.passed for item in self.gates)
            and not self.baseline_regressions
        )


class QualityPerformanceScenario(_QualityModel):
    users: int = Field(default=100, gt=0)
    facts_per_user: int = Field(default=100, gt=1)
    groups: int = Field(default=10, gt=0)
    chat_events: int = Field(default=100_000, gt=0)
    query_count: int = Field(default=50, gt=0)
    keyset_batch_size: int = Field(default=1_000, gt=0)


class QualityPerformanceReport(_QualityModel):
    schema_version: str = "1"
    generated_at: datetime
    machine_class: str
    python_version: str
    sqlite_version: str
    scenario: QualityPerformanceScenario
    populated_fact_count: int = Field(ge=0)
    active_embedded_fact_count: int = Field(default=0, ge=0)
    populated_event_count: int = Field(ge=0)
    plan_latency_ms: float = Field(ge=0)
    keyset_scan_seconds: float = Field(ge=0)
    keyset_events_per_second: float = Field(ge=0)
    retrieval_p50_ms: float = Field(ge=0)
    retrieval_p95_ms: float = Field(ge=0)
    context_p50_ms: float = Field(ge=0)
    context_p95_ms: float = Field(ge=0)
    quality_suite_total_ms: float | None = Field(default=None, ge=0)
    peak_memory_mib: float = Field(ge=0)
    model_request_count: int = Field(ge=0)
    embedding_document_request_count: int = Field(ge=0)
    embedding_query_request_count: int = Field(ge=0)


class QualityBaseline(_QualityModel):
    schema_version: str = "1"
    suite_version: str
    commit: str
    python_version: str
    sqlite_version: str
    generated_at: datetime
    dataset_hash: str
    gate_config_hash: str
    fake_model_id: str
    fake_embedding_id: str
    case_count: int = Field(ge=0)
    metrics: dict[str, float | None]
    performance: QualityPerformanceReport | None = None


class ProductionAuditIssue(_QualityModel):
    issue_code: str
    severity: str = Field(pattern=r"^(info|warning|error)$")
    count: int = Field(ge=0)
    sample_ids: tuple[int, ...] = ()


class ProductionAuditReport(_QualityModel):
    schema_version: str = "1"
    generated_at: datetime
    database_fingerprint: str
    issues: tuple[ProductionAuditIssue, ...]

    @property
    def error_count(self) -> int:
        return sum(item.count for item in self.issues if item.severity == "error")


class HygienePlan(_QualityModel):
    schema_version: str = "1"
    generated_at: datetime
    database_fingerprint: str
    fingerprint: str
    issue_counts: dict[str, int]
    invalid_fact_ids: tuple[int, ...] = ()
    rebuild_fts: bool = False
    enqueue_embedding_fact_ids: tuple[int, ...] = ()
    purge_terminal_rebuild_run_ids: tuple[int, ...] = ()


class ReleaseCheckItem(_QualityModel):
    code: str
    status: str = Field(pattern=r"^(pass|warn|fail)$")
    detail: str


class ReleaseCheckReport(_QualityModel):
    schema_version: str = "1"
    version: str
    alembic_head: str
    generated_at: datetime
    items: tuple[ReleaseCheckItem, ...]

    @property
    def passed(self) -> bool:
        return all(item.status != "fail" for item in self.items)
