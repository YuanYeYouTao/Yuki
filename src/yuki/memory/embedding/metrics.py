"""Content-free counters for embedding cost and retrieval diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class MemoryEmbeddingMetricSnapshot:
    document_embedding_requests: int = 0
    document_embedding_input_count: int = 0
    document_embedding_input_tokens: int = 0
    query_embedding_requests: int = 0
    query_embedding_input_tokens: int = 0
    query_embedding_failures: int = 0
    query_embedding_cache_hits: int = 0
    query_embedding_cache_misses: int = 0
    semantic_degraded_count: int = 0
    last_embedding_latency: float = 0


class MemoryEmbeddingMetrics:
    def __init__(self) -> None:
        self._snapshot = MemoryEmbeddingMetricSnapshot()

    def snapshot(self) -> MemoryEmbeddingMetricSnapshot:
        return self._snapshot

    def record_documents(
        self, *, input_count: int, input_tokens: int | None, latency: float
    ) -> None:
        current = self._snapshot
        self._snapshot = replace(
            current,
            document_embedding_requests=current.document_embedding_requests + 1,
            document_embedding_input_count=(current.document_embedding_input_count + input_count),
            document_embedding_input_tokens=(
                current.document_embedding_input_tokens + (input_tokens or 0)
            ),
            last_embedding_latency=latency,
        )

    def record_query(
        self,
        *,
        input_tokens: int | None,
        latency: float,
        failed: bool = False,
    ) -> None:
        current = self._snapshot
        self._snapshot = replace(
            current,
            query_embedding_requests=current.query_embedding_requests + 1,
            query_embedding_input_tokens=(
                current.query_embedding_input_tokens + (input_tokens or 0)
            ),
            query_embedding_failures=current.query_embedding_failures + int(failed),
            query_embedding_cache_misses=current.query_embedding_cache_misses + 1,
            semantic_degraded_count=current.semantic_degraded_count + int(failed),
            last_embedding_latency=latency,
        )

    def record_query_cache_hit(self) -> None:
        current = self._snapshot
        self._snapshot = replace(
            current,
            query_embedding_cache_hits=current.query_embedding_cache_hits + 1,
        )
