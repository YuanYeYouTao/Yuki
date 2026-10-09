"""Strict, content-safe contracts for deterministic Memory V2 quality evaluation."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _QualityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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
