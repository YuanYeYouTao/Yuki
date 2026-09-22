"""Bounded, same-target candidate discovery for Memory V2 consolidation."""

from __future__ import annotations

from qq_ai_bot.memory.enums import MemoryRetrievalMode, MemoryTargetRole
from qq_ai_bot.memory.errors import MemoryRetrievalError
from qq_ai_bot.memory.evidence import MemoryEvidencePolicy
from qq_ai_bot.memory.models import (
    MemoryCandidate,
    MemoryEntityTarget,
    MemoryFact,
    MemoryFactCreate,
    MemoryQuery,
)
from qq_ai_bot.memory.query import normalize_query_text
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.validation import normalize_memory_text


class MemoryConflictCandidateResolver:
    def __init__(
        self,
        repository: MemoryFactRepository,
        *,
        retriever: MemoryRetriever | None = None,
        limit: int = 12,
    ) -> None:
        if limit <= 0:
            raise ValueError("memory consolidation candidate limit must be positive")
        self._repository = repository
        self._retriever = retriever
        self._limit = limit
        self._evidence = MemoryEvidencePolicy()

    async def resolve(
        self,
        fact: MemoryFactCreate,
        *,
        limit: int | None = None,
    ) -> tuple[MemoryCandidate, ...]:
        normalized = normalize_memory_text(fact.content, maximum=4000).casefold()
        bounded_limit = limit or self._limit
        exact_rows = await self._repository.list_conflict_candidates(
            fact,
            normalized_content=normalized,
            limit=bounded_limit,
        )
        relevance: dict[int, float] = {}
        rows: dict[int, MemoryFact] = {row.id: row for row in exact_rows}
        global_cover = await self._repository.find_global_self_cover(
            fact,
            normalized_content=normalized,
        )
        if global_cover is not None:
            rows.setdefault(global_cover.id, global_cover)
            relevance[global_cover.id] = 1.0
        if self._retriever is not None:
            target = self._target(fact)
            query_text = normalize_query_text(
                " ".join((fact.memory_key, fact.category, fact.content))
            )
            try:
                retrieved = await self._retriever.retrieve(
                    MemoryQuery(
                        text=query_text,
                        normalized_text=query_text,
                        mode=MemoryRetrievalMode.RELEVANT,
                        targets=(target,),
                        kinds=(fact.kind,),
                        candidate_limit=bounded_limit,
                        limit_per_target=bounded_limit,
                        always_on_explicit_preference_limit=0,
                        query_term_limit=12,
                        semantic_candidate_limit=bounded_limit,
                    )
                )
            except MemoryRetrievalError:
                retrieved = None
            if retrieved is not None:
                exact_target = {
                    row.id: row
                    for row in await self._repository.get_active_for_exact_target(
                        target,
                        tuple(hit.fact.id for hit in retrieved.hits),
                    )
                }
                for hit in retrieved.hits:
                    candidate = exact_target.get(hit.fact.id)
                    if candidate is None:
                        continue
                    rows.setdefault(candidate.id, candidate)
                    relevance[hit.fact.id] = max(
                        relevance.get(hit.fact.id, 0.0),
                        max(0.0, 1.0 - ((hit.rank - 1) / max(1, bounded_limit))),
                    )
        ordered = sorted(
            rows.values(),
            key=lambda row: (
                row.memory_key != fact.memory_key,
                row.normalized_content != normalized,
                -relevance.get(row.id, 0.0),
                -self._evidence.authority_rank(row.authority),
                -row.updated_at.timestamp(),
                row.id,
            ),
        )[:bounded_limit]
        return tuple(
            MemoryCandidate(
                candidate_ref=f"candidate_{index}",
                fact=row,
                exact_key=row.memory_key == fact.memory_key,
                exact_content=row.normalized_content == normalized,
                relevance=(
                    1.0
                    if row.normalized_content == normalized
                    else 0.9
                    if row.memory_key == fact.memory_key
                    else relevance.get(row.id, 0.0)
                ),
            )
            for index, row in enumerate(ordered, start=1)
        )

    @staticmethod
    def _target(fact: MemoryFactCreate) -> MemoryEntityTarget:
        return MemoryEntityTarget(
            role={
                "person": MemoryTargetRole.CURRENT_PERSON,
                "person_group": MemoryTargetRole.CURRENT_PERSON_GROUP,
                "group": MemoryTargetRole.CURRENT_GROUP,
                "self": MemoryTargetRole.CURRENT_SELF,
            }[fact.scope_type.value],
            scope_type=fact.scope_type,
            subject_user_id=fact.subject_user_id,
            group_id=fact.group_id,
            visibility_type=fact.visibility_type,
            visibility_user_id=fact.visibility_user_id,
            visibility_group_id=fact.visibility_group_id,
            block_id="conflict_candidates",
        )
