"""Local-only embedding health and explicit remote doctor service."""

from __future__ import annotations

from yuki.memory.embedding.models import MemoryEmbeddingHealth
from yuki.memory.embedding.provider import EmbeddingProvider
from yuki.memory.embedding.repository import MemoryEmbeddingRepository
from yuki.memory.embedding.text import EmbeddingDocumentBuilder


class MemoryEmbeddingHealthService:
    def __init__(
        self,
        *,
        enabled: bool,
        provider: EmbeddingProvider | None,
        repository: MemoryEmbeddingRepository,
        profile_id: int | None,
        documents: EmbeddingDocumentBuilder,
    ) -> None:
        self._enabled = enabled
        self._provider = provider
        self._repository = repository
        self._profile_id = profile_id
        self._documents = documents

    async def health(self) -> MemoryEmbeddingHealth:
        if self._profile_id is None:
            active_count = await self._repository.active_fact_count()
            return MemoryEmbeddingHealth(
                enabled=self._enabled,
                provider_configured=self._provider is not None,
                current_profile=None,
                active_fact_count=active_count,
                ready_embedding_count=0,
                coverage_ratio=1.0 if active_count == 0 else 0,
                pending_job_count=0,
                processing_job_count=0,
                failed_job_count=0,
                stale_embedding_count=0,
                orphan_embedding_count=0,
                old_profile_count=0,
            )
        values = await self._repository.local_health(
            current_profile_id=self._profile_id,
            documents=self._documents,
        )
        active = int(values["active_fact_count"])
        ready = int(values["ready_embedding_count"])
        return MemoryEmbeddingHealth(
            enabled=self._enabled,
            provider_configured=self._provider is not None,
            current_profile=(
                self._provider.profile.fingerprint if self._provider is not None else None
            ),
            coverage_ratio=(ready / active if active else 1.0),
            **values,
        )

    async def doctor(self) -> int:
        if self._provider is None:
            raise RuntimeError("memory embedding provider is not configured")
        result = await self._provider.embed_query("Fixed provider health check text.")
        if len(result.vectors) != 1:
            raise RuntimeError("memory embedding provider returned an invalid result")
        return result.vectors[0].dimensions
