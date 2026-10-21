"""Backend-owned query construction for Memory V2 retrieval."""

from __future__ import annotations

import re
import unicodedata

from pydantic import ValidationError

from yuki.admin.models import RuntimeConfigSnapshot
from yuki.domain.messages import InboundMessage
from yuki.memory.enums import (
    MemoryContextMode,
    MemoryRetrievalMode,
)
from yuki.memory.errors import MemoryRetrievalError
from yuki.memory.models import MemoryEntityTarget, MemoryQuery, MemoryQueryIntent
from yuki.memory.targets import MemoryTargetResolver

_WHITESPACE = re.compile(r"\s+")


def normalize_query_text(value: str) -> str:
    """Normalize text for safe FTS construction, never for intent classification."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _WHITESPACE.sub(" ", normalized).strip()


class MemoryQueryBuilder:
    """Build a strict query from current-event text and structured memory intent."""

    def __init__(self, targets: MemoryTargetResolver) -> None:
        self._targets = targets

    async def resolve_targets(
        self,
        inbound: InboundMessage,
        *,
        self_recall: bool = False,
    ) -> tuple[MemoryEntityTarget, ...]:
        return await self._targets.resolve(
            inbound,
            include_self=self_recall,
        )

    @staticmethod
    def for_targets(
        *,
        text: str,
        mode: MemoryRetrievalMode,
        targets: tuple[MemoryEntityTarget, ...],
        runtime: RuntimeConfigSnapshot,
        limit: int | None = None,
        intent: MemoryQueryIntent | None = None,
    ) -> MemoryQuery:
        """Build a query inside pre-resolved targets.

        Calls without structured intent are management/plugin reads and retain
        the legacy neutral ordering.
        """

        memory = runtime.memory
        default_limit = (
            memory.overview_limit_per_entity
            if mode is MemoryRetrievalMode.OVERVIEW
            else memory.context_limit_per_entity
        )
        try:
            query = MemoryQuery(
                text=text,
                normalized_text=normalize_query_text(text),
                mode=mode,
                targets=targets,
                kinds=(),
                candidate_limit=memory.lexical_candidate_limit,
                limit_per_target=limit if limit is not None else default_limit,
                query_term_limit=memory.query_term_limit,
                short_query_fallback_enabled=memory.short_query_fallback_enabled,
                semantic_enabled=memory.semantic_enabled,
                semantic_candidate_limit=memory.semantic_candidate_limit,
                hybrid_lexical_weight=memory.hybrid_lexical_weight,
                hybrid_semantic_weight=memory.hybrid_semantic_weight,
                hybrid_rrf_k=memory.hybrid_rrf_k,
                intent=intent,
            )
        except ValidationError as exc:
            raise MemoryRetrievalError("memory_query_invalid") from exc
        if intent is not None and intent.mode in {
            MemoryContextMode.LEXICAL,
            MemoryContextMode.OVERVIEW,
        }:
            return query.model_copy(update={"semantic_enabled": False})
        return query
