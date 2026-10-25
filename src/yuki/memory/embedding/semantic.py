"""Authorized semantic search over normalized SQLite float32 BLOBs."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from heapq import nsmallest

from yuki.memory.authorized_scope import AuthorizedMemoryScope
from yuki.memory.embedding.codec import Float32VectorCodec
from yuki.memory.embedding.models import (
    EmbeddingProviderProfile,
    EmbeddingVector,
    MemorySemanticCandidate,
)
from yuki.memory.embedding.repository import MemoryEmbeddingRepository, VectorCandidates
from yuki.memory.embedding.text import EmbeddingDocumentBuilder
from yuki.memory.enums import MemoryKind
from yuki.memory.models import MemoryEntityTarget, MemoryTemporalIntent


@dataclass(frozen=True, slots=True)
class AuthorizedSemanticCandidate:
    fact_id: int
    cosine_similarity: float
    semantic_rank: int


@dataclass(frozen=True, slots=True)
class SemanticSearchResult[T]:
    candidates: tuple[T, ...]
    truncated: bool
    coverage_complete: bool
    approximate: bool


class MemorySemanticIndex:
    """Compute similarity only after repository SQL enforces identity and profile."""

    def __init__(
        self,
        repository: MemoryEmbeddingRepository,
        *,
        documents: EmbeddingDocumentBuilder,
        codec: Float32VectorCodec | None = None,
    ) -> None:
        self._repository = repository
        self._documents = documents
        self._codec = codec or Float32VectorCodec()

    @property
    def repository(self) -> MemoryEmbeddingRepository:
        return self._repository

    def _score(
        self,
        loaded: VectorCandidates,
        query_vector: EmbeddingVector,
        dimensions: int,
        candidate_limit: int,
    ) -> tuple[list[tuple[int, float]], int, int]:
        normalized_query = None
        stale_count = score_count = 0

        def scores() -> Iterator[tuple[int, float]]:
            nonlocal normalized_query, stale_count, score_count
            for row in loaded.rows:
                current_hash = self._documents.content_hash_fields(
                    kind=row.kind,
                    category=row.category,
                    memory_key=row.memory_key,
                    content=row.content,
                )
                if row.content_hash != current_hash:
                    stale_count += 1
                    continue
                vector = self._codec.decode_normalized(row.vector_blob, dimensions=dimensions)
                if normalized_query is None:
                    normalized_query = self._codec.normalize(query_vector)
                score_count += 1
                yield row.fact_id, self._codec.dot_normalized(normalized_query, vector)

        selected = nsmallest(candidate_limit, scores(), key=lambda item: (-item[1], item[0]))
        return selected, stale_count, score_count

    async def search_authorized(
        self,
        *,
        scope: AuthorizedMemoryScope,
        query_vector: EmbeddingVector,
        profile: EmbeddingProviderProfile,
        profile_id: int,
        candidate_limit: int,
        kinds: tuple[MemoryKind, ...],
        temporal: MemoryTemporalIntent | None = None,
    ) -> SemanticSearchResult[AuthorizedSemanticCandidate]:
        loaded = await self._repository.load_authorized_vectors(
            scope=scope,
            profile_id=profile_id,
            kinds=tuple(kind.value for kind in kinds),
            temporal=temporal,
            query_blob=self._codec.encode(query_vector),
            candidate_count=candidate_limit * 4,
        )
        selected, stale_count, score_count = self._score(
            loaded, query_vector, profile.dimensions, candidate_limit
        )
        return SemanticSearchResult(
            tuple(
                AuthorizedSemanticCandidate(fact_id, similarity, rank)
                for rank, (fact_id, similarity) in enumerate(selected, start=1)
            ),
            loaded.approximate or score_count > candidate_limit,
            not stale_count and loaded.indexed_count == loaded.active_count,
            loaded.approximate,
        )

    async def search(
        self,
        *,
        target: MemoryEntityTarget,
        query_vector: EmbeddingVector,
        profile: EmbeddingProviderProfile,
        profile_id: int,
        candidate_limit: int,
        kinds: tuple[MemoryKind, ...],
        temporal: MemoryTemporalIntent | None = None,
    ) -> SemanticSearchResult[MemorySemanticCandidate]:
        loaded = await self._repository.load_target_vectors(
            target=target,
            profile_id=profile_id,
            kinds=tuple(kind.value for kind in kinds),
            temporal=temporal,
            query_blob=self._codec.encode(query_vector),
            candidate_count=candidate_limit * 4,
        )
        selected, stale_count, score_count = self._score(
            loaded, query_vector, profile.dimensions, candidate_limit
        )
        return SemanticSearchResult(
            tuple(
                MemorySemanticCandidate(
                    fact_id=fact_id,
                    target=target,
                    cosine_similarity=similarity,
                    semantic_rank=rank,
                )
                for rank, (fact_id, similarity) in enumerate(selected, start=1)
            ),
            loaded.approximate or score_count > candidate_limit,
            not stale_count and loaded.indexed_count == loaded.active_count,
            loaded.approximate,
        )
