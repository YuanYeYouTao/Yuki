"""Deterministic ranking for Memory V2 search and overview candidates.

Contract: docs/architecture/memory-v2-retrieval.md "查询与排序". Lexical scores come
from ``memory/lexical.py`` (computed over the whole candidate pool, so comparable across
owners); the final order is RRF over the lexical and vector ranks, with facts whose key
or content equals the query first.
"""

from __future__ import annotations

from collections.abc import Mapping

from yuki.memory.embedding.models import MemorySemanticCandidate
from yuki.memory.enums import MemoryConflictState
from yuki.memory.models import (
    MemoryEntityTarget,
    MemoryFact,
    MemoryQuery,
    MemoryRetrievalHit,
)
from yuki.memory.query import normalize_query_text

_FIRST = frozenset({"memory_key_exact", "content_exact"})


def _exact(fact: MemoryFact, normalized_query: str) -> str | None:
    if normalize_query_text(fact.memory_key) == normalized_query:
        return "memory_key_exact"
    if normalize_query_text(fact.normalized_content) == normalized_query:
        return "content_exact"
    if normalize_query_text(fact.category) == normalized_query:
        return "category_exact"
    return None


class MemoryRanker:
    """Rank without an LLM and always end with a stable fact-id tie break."""

    @staticmethod
    def hits(
        *,
        facts: tuple[MemoryFact, ...],
        lexical_scores: Mapping[int, float],
        semantic_candidates: tuple[MemorySemanticCandidate, ...],
        target: MemoryEntityTarget,
        normalized_query: str,
    ) -> tuple[MemoryRetrievalHit, ...]:
        """One unranked hit per fact with a positive lexical score or a vector match."""
        semantic = {candidate.fact_id: candidate for candidate in semantic_candidates}
        found: list[MemoryRetrievalHit] = []
        for fact in facts:
            score = lexical_scores.get(fact.id, 0.0)
            vector = semantic.get(fact.id)
            if score <= 0 and vector is None:
                continue
            exact = _exact(fact, normalized_query) if score > 0 else None
            sources = tuple(
                source
                for source, present in (("lexical", score > 0), ("semantic", vector is not None))
                if present
            )
            found.append(
                MemoryRetrievalHit(
                    fact=fact,
                    target=target,
                    rank=1,
                    lexical_score=score if score > 0 else None,
                    semantic_score=vector.cosine_similarity if vector is not None else None,
                    sources=sources,
                    selection_reason=exact
                    or (
                        "hybrid_match"
                        if len(sources) == 2
                        else "semantic_match"
                        if vector is not None
                        else "lexical_match"
                    ),
                )
            )
        return tuple(found)

    @staticmethod
    def rank_global(
        hits: tuple[MemoryRetrievalHit, ...], query: MemoryQuery
    ) -> tuple[MemoryRetrievalHit, ...]:
        """RRF over the common pool's lexical and vector ranks, not inside each owner."""
        unique = tuple({hit.fact.id: hit for hit in reversed(hits)}.values())
        lexical = {
            hit.fact.id: rank
            for rank, hit in enumerate(
                sorted(
                    (hit for hit in unique if hit.lexical_score is not None),
                    key=lambda hit: (
                        hit.selection_reason not in _FIRST,
                        -(hit.lexical_score or 0),
                        hit.fact.conflict_state is MemoryConflictState.CONTESTED,
                        -hit.fact.importance,
                        -hit.fact.confidence,
                        -hit.fact.updated_at.timestamp(),
                        hit.fact.id,
                    ),
                ),
                1,
            )
        }
        semantic = {
            hit.fact.id: rank
            for rank, hit in enumerate(
                sorted(
                    (hit for hit in unique if hit.semantic_score is not None),
                    key=lambda hit: (-float(hit.semantic_score or 0), hit.fact.id),
                ),
                1,
            )
        }
        scored = tuple(
            hit.model_copy(
                update={
                    "lexical_rank": lexical.get(hit.fact.id),
                    "semantic_rank": semantic.get(hit.fact.id),
                    "fusion_score": (
                        query.hybrid_lexical_weight / (query.hybrid_rrf_k + lexical[hit.fact.id])
                        if hit.fact.id in lexical
                        else 0
                    )
                    + (
                        query.hybrid_semantic_weight / (query.hybrid_rrf_k + semantic[hit.fact.id])
                        if hit.fact.id in semantic
                        else 0
                    ),
                }
            )
            for hit in unique
        )
        ordered = sorted(
            scored,
            key=lambda hit: (
                hit.selection_reason not in _FIRST,
                -hit.fusion_score,
                -hit.fact.importance,
                hit.fact.id,
            ),
        )
        return tuple(hit.model_copy(update={"rank": rank}) for rank, hit in enumerate(ordered, 1))

    @staticmethod
    def rank_overview(
        facts: tuple[MemoryFact, ...],
        *,
        target: MemoryEntityTarget,
        limit: int,
        reason: str = "overview",
    ) -> tuple[MemoryRetrievalHit, ...]:
        ordered = sorted(
            facts,
            key=lambda fact: (
                1 if fact.conflict_state is MemoryConflictState.CONTESTED else 0,
                -fact.importance,
                -fact.confidence,
                -fact.updated_at.timestamp(),
                fact.id,
            ),
        )[:limit]
        return tuple(
            MemoryRetrievalHit(
                fact=fact,
                target=target,
                rank=rank,
                sources=(reason,),
                selection_reason=reason,
            )
            for rank, fact in enumerate(ordered, start=1)
        )
