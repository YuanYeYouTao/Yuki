"""Strict, provider-neutral models for the rebuildable embedding index."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yuki.memory.models import MemoryEntityTarget


class _EmbeddingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmbeddingProviderProfile(_EmbeddingModel):
    provider_id: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=128)
    dimensions: int = Field(gt=0)
    output_type: str = "dense"
    document_template_version: int = Field(gt=0)
    endpoint_identity: str = Field(min_length=1, max_length=512)
    fingerprint: str = ""

    @model_validator(mode="after")
    def _fingerprint(self) -> EmbeddingProviderProfile:
        identity = {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "dimensions": self.dimensions,
            "output_type": self.output_type,
            "document_template_version": self.document_template_version,
            "endpoint_identity": self.endpoint_identity,
        }
        expected = hashlib.sha256(
            json.dumps(identity, ensure_ascii=True, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("embedding profile fingerprint does not match its identity")
        object.__setattr__(self, "fingerprint", expected)
        return self


class EmbeddingProviderCapabilities(_EmbeddingModel):
    max_batch_size: int = Field(gt=0)
    supports_query_document_type: bool
    supports_instruct: bool
    supported_dimensions: tuple[int, ...]

    @model_validator(mode="after")
    def _dimensions(self) -> EmbeddingProviderCapabilities:
        if not self.supported_dimensions or any(value <= 0 for value in self.supported_dimensions):
            raise ValueError("supported dimensions must contain positive values")
        return self


class EmbeddingUsage(_EmbeddingModel):
    input_count: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    request_id: str | None = None


class EmbeddingVector(_EmbeddingModel):
    values: tuple[float, ...]
    dimensions: int = Field(gt=0)

    @model_validator(mode="after")
    def _values(self) -> EmbeddingVector:
        if len(self.values) != self.dimensions:
            raise ValueError("embedding vector dimension mismatch")
        if any(not math.isfinite(value) for value in self.values):
            raise ValueError("embedding vector contains a non-finite value")
        if not any(value != 0 for value in self.values):
            raise ValueError("embedding vector cannot be zero")
        return self


class EmbeddingBatchResult(_EmbeddingModel):
    vectors: tuple[EmbeddingVector, ...]
    usage: EmbeddingUsage


class MemoryEmbeddingProfileRecord(_EmbeddingModel):
    id: int = Field(gt=0)
    profile: EmbeddingProviderProfile
    created_at: datetime


class MemoryEmbeddingRecord(_EmbeddingModel):
    fact_id: int = Field(gt=0)
    profile_id: int = Field(gt=0)
    content_hash: str = Field(min_length=64, max_length=64)
    vector: EmbeddingVector
    created_at: datetime
    updated_at: datetime


class MemoryEmbeddingJobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class MemoryEmbeddingJob(_EmbeddingModel):
    id: int = Field(gt=0)
    fact_id: int = Field(gt=0)
    profile_id: int = Field(gt=0)
    content_hash: str = Field(min_length=64, max_length=64)
    status: MemoryEmbeddingJobStatus
    attempts: int = Field(ge=0)
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime
    error_category: str | None = None


class MemorySemanticCandidate(_EmbeddingModel):
    fact_id: int = Field(gt=0)
    target: MemoryEntityTarget
    cosine_similarity: float = Field(ge=-1, le=1)
    semantic_rank: int = Field(gt=0)


class MemoryEmbeddingHealth(_EmbeddingModel):
    enabled: bool
    provider_configured: bool
    current_profile: str | None
    active_fact_count: int = Field(ge=0)
    ready_embedding_count: int = Field(ge=0)
    coverage_ratio: float = Field(ge=0, le=1)
    pending_job_count: int = Field(ge=0)
    processing_job_count: int = Field(ge=0)
    failed_job_count: int = Field(ge=0)
    stale_embedding_count: int = Field(ge=0)
    orphan_embedding_count: int = Field(ge=0)
    old_profile_count: int = Field(ge=0)
    last_success_at: datetime | None = None
    last_error_category: str | None = None
