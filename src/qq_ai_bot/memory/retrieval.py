"""Single query-driven read entrypoint for Memory V2 facts."""

from __future__ import annotations

import hashlib
import logging
import time

from qq_ai_bot.memory.authorized_scope import (
    AuthorizedMemoryScope,
    target_for_authorized_fact,
)
from qq_ai_bot.memory.embedding.metrics import MemoryEmbeddingMetrics
from qq_ai_bot.memory.embedding.models import (
    EmbeddingBatchResult,
    MemoryEmbeddingProfileRecord,
    MemorySemanticCandidate,
)
from qq_ai_bot.memory.embedding.provider import EmbeddingProvider, EmbeddingProviderError
from qq_ai_bot.memory.embedding.query_cache import QueryEmbeddingCache
from qq_ai_bot.memory.embedding.semantic import AuthorizedSemanticCandidate, MemorySemanticIndex
from qq_ai_bot.memory.embedding.text import EmbeddingQueryBuilder
from qq_ai_bot.memory.enums import (
    MemoryRetrievalMode,
    MemoryTargetRole,
)
from qq_ai_bot.memory.fts import (
    AuthorizedLexicalCandidate,
    MemoryLexicalIndex,
    SQLiteMemoryFTSIndex,
    build_safe_lexical_query,
)
from qq_ai_bot.memory.metrics import MemoryRetrievalMetric, MemoryRetrievalMetrics
from qq_ai_bot.memory.models import (
    MemoryEntityTarget,
    MemoryFact,
    MemoryLexicalCandidate,
    MemoryQuery,
    MemoryRetrievalBlock,
    MemoryRetrievalHit,
    MemoryRetrievalResult,
)
from qq_ai_bot.memory.ranking import MemoryRanker
from qq_ai_bot.memory.repository import MemoryFactRepository

logger = logging.getLogger(__name__)


class MemoryRetriever:
    """SQL-filter each identity target, then compare its candidates in one pool."""

    def __init__(
        self,
        *,
        repository: MemoryFactRepository,
        lexical_index: MemoryLexicalIndex,
        ranker: MemoryRanker | None = None,
        metrics: MemoryRetrievalMetrics | None = None,
        semantic_index: MemorySemanticIndex | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        embedding_profile: MemoryEmbeddingProfileRecord | None = None,
        embedding_queries: EmbeddingQueryBuilder | None = None,
        embedding_metrics: MemoryEmbeddingMetrics | None = None,
        query_embedding_cache: QueryEmbeddingCache | None = None,
    ) -> None:
        self._repository = repository
        self._index = lexical_index
        self._ranker = ranker or MemoryRanker()
        self._metrics = metrics or MemoryRetrievalMetrics()
        self._semantic_index = semantic_index
        self._embedding_provider = embedding_provider
        self._embedding_profile = embedding_profile
        self._embedding_queries = embedding_queries
        self._embedding_metrics = embedding_metrics
        self._query_embedding_cache = query_embedding_cache

    @property
    def metrics(self) -> MemoryRetrievalMetrics:
        return self._metrics

    def configure_semantic(
        self,
        *,
        semantic_index: MemorySemanticIndex,
        provider: EmbeddingProvider,
        profile: MemoryEmbeddingProfileRecord,
        queries: EmbeddingQueryBuilder,
        metrics: MemoryEmbeddingMetrics | None = None,
        query_cache: QueryEmbeddingCache | None = None,
    ) -> None:
        self._semantic_index = semantic_index
        self._embedding_provider = provider
        self._embedding_profile = profile
        self._embedding_queries = queries
        self._embedding_metrics = metrics
        self._query_embedding_cache = query_cache

    async def retrieve_authorized(
        self,
        query: MemoryQuery,
        scope: AuthorizedMemoryScope,
        *,
        limit: int,
    ) -> MemoryRetrievalResult:
        """One authorization-filtered global candidate pool, including unbound owners."""
        if not isinstance(self._index, SQLiteMemoryFTSIndex):
            raise RuntimeError("global memory search requires SQLite FTS index")
        query_hash = hashlib.sha256(query.normalized_text.encode("utf-8")).hexdigest()
        safe = build_safe_lexical_query(query.normalized_text, term_limit=query.query_term_limit)
        lexical, lexical_truncated = await self._index.search_authorized(
            scope,
            safe,
            candidate_limit=query.candidate_limit,
            kinds=query.kinds,
            temporal=query.intent.temporal if query.intent else None,
        )
        semantic: tuple[AuthorizedSemanticCandidate, ...] = ()
        semantic_truncated = False
        semantic_coverage_complete = True
        semantic_status = "disabled"
        semantic_degraded = False
        if query.semantic_enabled:
            if (
                self._semantic_index is None
                or self._embedding_provider is None
                or self._embedding_profile is None
                or self._embedding_queries is None
            ):
                semantic_status = "not_configured"
            else:
                try:
                    query_text = self._embedding_queries.build(query)
                    if query_text:
                        provider = self._embedding_provider
                        profile = self._embedding_profile

                        async def embed_once() -> EmbeddingBatchResult:
                            embedded = await provider.embed_query(query_text)
                            if len(embedded.vectors) != 1:
                                raise EmbeddingProviderError(
                                    "embedding_invalid_response",
                                    "Embedding provider returned an invalid response.",
                                    retryable=False,
                                )
                            return embedded

                        if self._query_embedding_cache is not None:
                            embedded, _ = await self._query_embedding_cache.get_or_create(
                                profile_fingerprint=profile.profile.fingerprint,
                                query_text=query_text,
                                factory=embed_once,
                            )
                        else:
                            embedded = await embed_once()
                        (
                            semantic,
                            semantic_truncated,
                            semantic_coverage_complete,
                        ) = await self._semantic_index.search_authorized(
                            scope=scope,
                            query_vector=embedded.vectors[0],
                            profile=profile.profile,
                            profile_id=profile.id,
                            candidate_limit=query.semantic_candidate_limit,
                            kinds=query.kinds,
                            temporal=query.intent.temporal if query.intent else None,
                        )
                        semantic_status = (
                            "ready" if semantic_coverage_complete else "index_incomplete"
                        )
                    else:
                        semantic_status = "empty_query"
                except (EmbeddingProviderError, ValueError) as exc:
                    semantic_status = (
                        exc.code
                        if isinstance(exc, EmbeddingProviderError)
                        else "embedding_index_invalid"
                    )
                    semantic_degraded = True
                    logger.warning("memory_semantic_degraded error_category=%s", semantic_status)
        candidate_ids = tuple(
            dict.fromkeys([item.fact_id for item in lexical] + [item.fact_id for item in semantic])
        )
        facts = await self._repository.get_active_authorized(scope, candidate_ids)
        lexical_by_id: dict[int, AuthorizedLexicalCandidate] = {
            item.fact_id: item for item in lexical
        }
        semantic_by_id: dict[int, AuthorizedSemanticCandidate] = {
            item.fact_id: item for item in semantic
        }
        by_target: dict[str, list[MemoryFact]] = {}
        targets: dict[str, MemoryEntityTarget] = {}
        for fact in facts:
            target = target_for_authorized_fact(fact, scope)
            targets[target.block_id] = target
            by_target.setdefault(target.block_id, []).append(fact)
        pooled: list[MemoryRetrievalHit] = []
        for block_id, target_facts in by_target.items():
            target = targets[block_id]
            target_lexical = tuple(
                MemoryLexicalCandidate(
                    fact_id=fact.id,
                    target=target,
                    fts_rank=lexical_by_id[fact.id].fts_rank,
                    exact_match=lexical_by_id[fact.id].exact_match,
                    matched_terms=lexical_by_id[fact.id].matched_terms,
                )
                for fact in target_facts
                if fact.id in lexical_by_id
            )
            target_semantic = tuple(
                MemorySemanticCandidate(
                    fact_id=fact.id,
                    target=target,
                    cosine_similarity=semantic_by_id[fact.id].cosine_similarity,
                    semantic_rank=semantic_by_id[fact.id].semantic_rank,
                )
                for fact in target_facts
                if fact.id in semantic_by_id
            )
            pooled.extend(
                self._ranker.rank_hybrid(
                    facts=tuple(target_facts),
                    lexical_candidates=target_lexical,
                    semantic_candidates=target_semantic,
                    target=target,
                    normalized_query=query.normalized_text,
                    lexical_weight=query.hybrid_lexical_weight,
                    semantic_weight=query.hybrid_semantic_weight,
                    rrf_k=query.hybrid_rrf_k,
                    limit=len(target_facts),
                )
            )
        ranked = self._ranker.rank_global(tuple(pooled), query)
        selected = ranked[:limit]
        blocks = tuple(
            MemoryRetrievalBlock(
                target=target,
                hits=tuple(hit for hit in selected if hit.target == target),
            )
            for target in targets.values()
        )
        candidate_truncated = lexical_truncated or semantic_truncated
        output_truncated = len(ranked) > limit
        semantic_unavailable = query.semantic_enabled and semantic_status == "not_configured"
        partial_reason = "global_candidate_budget" if candidate_truncated else None
        if partial_reason is None and semantic_unavailable:
            partial_reason = "semantic_not_configured"
        if partial_reason is None and semantic_degraded:
            partial_reason = "semantic_degraded"
        if partial_reason is None and not semantic_coverage_complete:
            partial_reason = "semantic_index_incomplete"
        if output_truncated and partial_reason is None:
            partial_reason = "global_result_limit"
        return MemoryRetrievalResult(
            blocks=blocks,
            hits=selected,
            candidate_count=len(candidate_ids),
            selected_count=len(selected),
            query_hash=query_hash,
            mode=query.mode,
            semantic_status=semantic_status,
            semantic_degraded=semantic_degraded,
            embedding_profile=(
                self._embedding_profile.profile.fingerprint
                if semantic and self._embedding_profile is not None
                else None
            ),
            exhaustive=not candidate_truncated
            and not semantic_degraded
            and not semantic_unavailable
            and semantic_coverage_complete
            and not output_truncated,
            truncated=candidate_truncated or output_truncated,
            partial_reason=partial_reason,
            ranked_count=len(ranked),
        )

    async def retrieve(
        self,
        query: MemoryQuery,
        *,
        lexical_enabled: bool = True,
    ) -> MemoryRetrievalResult:
        started = time.perf_counter()
        fts_latency = 0.0
        semantic_latency = 0.0
        hybrid_latency = 0.0
        candidate_count = 0
        semantic_candidate_count = 0
        candidate_truncated = False
        blocks: list[MemoryRetrievalBlock] = []
        all_hits: list[MemoryRetrievalHit] = []
        short_fallback_used = False
        query_vector = None
        semantic_degraded = False
        semantic_status = "disabled"
        semantic_requested = (
            lexical_enabled
            and query.mode is MemoryRetrievalMode.RELEVANT
            and query.semantic_enabled
        )
        if semantic_requested and not query.normalized_text:
            semantic_status = "empty_query"
        elif semantic_requested and (
            self._semantic_index is None
            or self._embedding_provider is None
            or self._embedding_profile is None
            or self._embedding_queries is None
        ):
            semantic_status = "not_configured"
        elif semantic_requested:
            provider = self._embedding_provider
            profile = self._embedding_profile
            queries = self._embedding_queries
            assert provider is not None
            assert profile is not None
            assert queries is not None
            embedding_started = time.perf_counter()
            try:
                semantic_status = "ready"
                query_text = queries.build(query)
                if query_text:

                    async def embed_once() -> EmbeddingBatchResult:
                        result = await provider.embed_query(query_text)
                        if len(result.vectors) != 1:
                            raise EmbeddingProviderError(
                                "embedding_invalid_response",
                                "Embedding provider returned an invalid response.",
                                retryable=False,
                            )
                        return result

                    cache_hit = False
                    if self._query_embedding_cache is not None:
                        embedded, cache_hit = await self._query_embedding_cache.get_or_create(
                            profile_fingerprint=profile.profile.fingerprint,
                            query_text=query_text,
                            factory=embed_once,
                        )
                    else:
                        embedded = await embed_once()
                    embedding_latency = time.perf_counter() - embedding_started
                    semantic_latency += embedding_latency
                    if self._embedding_metrics is not None:
                        if cache_hit:
                            self._embedding_metrics.record_query_cache_hit()
                        else:
                            self._embedding_metrics.record_query(
                                input_tokens=embedded.usage.input_tokens,
                                latency=embedding_latency,
                            )
                    query_vector = embedded.vectors[0]
                else:
                    semantic_status = "empty_query"
            except EmbeddingProviderError as exc:
                if self._embedding_metrics is not None:
                    self._embedding_metrics.record_query(
                        input_tokens=None,
                        latency=time.perf_counter() - embedding_started,
                        failed=True,
                    )
                semantic_status = exc.code
                semantic_degraded = True
                logger.warning("memory_semantic_degraded error_category=%s", exc.code)
        elif query.mode is MemoryRetrievalMode.OVERVIEW:
            semantic_status = "overview"
        for target in query.targets:
            hits: tuple[MemoryRetrievalHit, ...]
            if query.mode is MemoryRetrievalMode.OVERVIEW or not lexical_enabled:
                overview_pool_limit = query.limit_per_target
                facts = await self._repository.list_overview(
                    target,
                    limit=overview_pool_limit + 1,
                    temporal=query.intent.temporal if query.intent is not None else None,
                )
                candidate_truncated = candidate_truncated or len(facts) > overview_pool_limit
                facts = facts[:overview_pool_limit]
                candidate_count += len(facts)
                hits = self._ranker.rank_overview(
                    facts,
                    target=target,
                    limit=overview_pool_limit,
                    reason=("overview" if lexical_enabled else "retrieval_disabled_fallback"),
                )
            else:
                safe = build_safe_lexical_query(
                    query.normalized_text,
                    term_limit=query.query_term_limit,
                )
                search_started = time.perf_counter()
                candidates = await self._index.search(
                    target,
                    safe,
                    candidate_limit=query.candidate_limit + 1,
                    kinds=query.kinds,
                    short_query_fallback_enabled=query.short_query_fallback_enabled,
                    temporal=query.intent.temporal if query.intent is not None else None,
                )
                fts_latency += time.perf_counter() - search_started
                candidate_truncated = candidate_truncated or len(candidates) > query.candidate_limit
                candidates = candidates[: query.candidate_limit]
                short_fallback_used = short_fallback_used or bool(
                    safe.short_term and query.short_query_fallback_enabled
                )
                semantic_candidates: tuple[MemorySemanticCandidate, ...] = ()
                if query_vector is not None:
                    assert self._semantic_index is not None
                    assert self._embedding_profile is not None
                    semantic_started = time.perf_counter()
                    try:
                        semantic_candidates = await self._semantic_index.search(
                            target=target,
                            query_vector=query_vector,
                            profile=self._embedding_profile.profile,
                            profile_id=self._embedding_profile.id,
                            candidate_limit=query.semantic_candidate_limit + 1,
                            kinds=query.kinds,
                            temporal=query.intent.temporal if query.intent is not None else None,
                        )
                    except ValueError:
                        semantic_degraded = True
                        semantic_status = "embedding_index_invalid"
                        logger.warning(
                            "memory_semantic_degraded error_category=%s",
                            semantic_status,
                        )
                    semantic_latency += time.perf_counter() - semantic_started
                    candidate_truncated = (
                        candidate_truncated
                        or len(semantic_candidates) > query.semantic_candidate_limit
                    )
                    semantic_candidates = semantic_candidates[: query.semantic_candidate_limit]
                candidate_ids = tuple(
                    dict.fromkeys(
                        [item.fact_id for item in candidates]
                        + [item.fact_id for item in semantic_candidates]
                    )
                )
                candidate_facts = await self._repository.get_active_for_target(
                    target, candidate_ids
                )
                candidate_count += len(candidates) + len(semantic_candidates)
                semantic_candidate_count += len(semantic_candidates)
                hybrid_started = time.perf_counter()
                lexical_hits = self._ranker.rank_hybrid(
                    facts=candidate_facts,
                    lexical_candidates=candidates,
                    semantic_candidates=semantic_candidates,
                    target=target,
                    normalized_query=query.normalized_text,
                    lexical_weight=query.hybrid_lexical_weight,
                    semantic_weight=query.hybrid_semantic_weight,
                    rrf_k=query.hybrid_rrf_k,
                    limit=len(candidate_facts),
                )
                hybrid_latency += time.perf_counter() - hybrid_started
                hits = lexical_hits
            blocks.append(MemoryRetrievalBlock(target=target, hits=hits))
            all_hits.extend(hits)

        if query.mode is MemoryRetrievalMode.RELEVANT:
            ranked = self._ranker.rank_global(tuple(all_hits), query)
        else:
            ranked = tuple(
                hit.model_copy(update={"rank": rank})
                for rank, hit in enumerate(
                    sorted(
                        all_hits,
                        key=lambda hit: (
                            -hit.fact.importance,
                            -hit.fact.confidence,
                            -hit.fact.updated_at.timestamp(),
                            hit.fact.id,
                        ),
                    ),
                    1,
                )
            )
        all_hits = []
        per_target: dict[str, int] = {}
        output_truncated = False
        for hit in ranked:
            key = hit.target.block_id
            if per_target.get(key, 0) >= query.limit_per_target:
                output_truncated = True
                continue
            per_target[key] = per_target.get(key, 0) + 1
            all_hits.append(hit.model_copy(update={"rank": len(all_hits) + 1}))
        blocks = [
            MemoryRetrievalBlock(
                target=target, hits=tuple(hit for hit in all_hits if hit.target == target)
            )
            for target in query.targets
        ]

        query_hash = hashlib.sha256(query.normalized_text.encode("utf-8")).hexdigest()
        semantic_unavailable = query.semantic_enabled and semantic_status == "not_configured"
        partial_reason = (
            "explicit_candidate_budget"
            if candidate_truncated
            else "semantic_degraded"
            if semantic_degraded
            else "semantic_not_configured"
            if semantic_unavailable
            else "explicit_result_limit"
            if output_truncated
            else "explicit_semantic_coverage_unknown"
            if query.semantic_enabled
            else None
        )
        result = MemoryRetrievalResult(
            blocks=tuple(blocks),
            hits=tuple(all_hits),
            candidate_count=candidate_count,
            selected_count=len(all_hits),
            query_hash=query_hash,
            mode=query.mode,
            semantic_status=semantic_status,
            semantic_degraded=semantic_degraded,
            embedding_profile=(
                self._embedding_profile.profile.fingerprint
                if query_vector is not None and self._embedding_profile is not None
                else None
            ),
            exhaustive=not candidate_truncated
            and not output_truncated
            and not query.semantic_enabled,
            truncated=candidate_truncated or output_truncated,
            partial_reason=partial_reason,
            ranked_count=len(ranked),
        )
        referenced = {
            target.subject_user_id
            for target in query.targets
            if target.role
            in {
                MemoryTargetRole.REFERENCED_PERSON,
                MemoryTargetRole.REFERENCED_PERSON_GROUP,
            }
        }
        self._metrics.record(
            MemoryRetrievalMetric(
                mode=query.mode,
                query_hash=query_hash,
                target_count=len(query.targets),
                candidate_count=candidate_count,
                selected_count=len(all_hits),
                context_selected_count=0,
                fts_latency=fts_latency,
                total_latency=time.perf_counter() - started,
                overview_used=query.mode is MemoryRetrievalMode.OVERVIEW,
                short_query_fallback_used=short_fallback_used,
                referenced_person_count=len(referenced - {None}),
                semantic_candidate_count=semantic_candidate_count,
                semantic_selected_count=sum(1 for hit in all_hits if "semantic" in hit.sources),
                hybrid_selected_count=sum(
                    1 for hit in all_hits if "semantic" in hit.sources and "lexical" in hit.sources
                ),
                semantic_degraded=semantic_degraded,
                semantic_search_latency=semantic_latency,
                hybrid_rank_latency=hybrid_latency,
            )
        )
        return result
