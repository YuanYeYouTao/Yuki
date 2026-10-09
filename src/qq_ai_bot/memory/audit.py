"""Bounded Memory V2 fact inspection and local consistency diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import (
    MemoryConsistencyHealth,
    MemoryEvidence,
    MemoryFact,
    MemoryFactRelation,
    MemoryFactStateEvent,
)
from qq_ai_bot.memory.repository import MemoryFactRepository


class MemoryAuditService:
    def __init__(
        self,
        repository: MemoryFactRepository,
        *,
        metrics: MemoryLifecycleMetrics | None = None,
    ) -> None:
        self._repository = repository
        self._metrics = metrics or MemoryLifecycleMetrics()

    async def get_fact(self, fact_id: int) -> MemoryFact | None:
        return await self._repository.get_fact(fact_id)

    async def get_evidence(self, fact_id: int, *, limit: int = 100) -> tuple[MemoryEvidence, ...]:
        return await self._repository.list_evidence(fact_id, limit=limit)

    async def get_relations(self, fact_id: int) -> tuple[MemoryFactRelation, ...]:
        return await self._repository.list_relations(fact_id)

    async def get_state_history(self, fact_id: int) -> tuple[MemoryFactStateEvent, ...]:
        return await self._repository.list_state_events(fact_id)

    async def get_supersession_chain(
        self, fact_id: int, *, limit: int = 20
    ) -> tuple[MemoryFact, ...]:
        result: list[MemoryFact] = []
        seen: set[int] = set()
        current = await self._repository.get_fact(fact_id)
        while current is not None and current.id not in seen and len(result) < max(1, limit):
            result.append(current)
            seen.add(current.id)
            current = (
                await self._repository.get_fact(current.supersedes_id)
                if current.supersedes_id is not None
                else None
            )
        return tuple(result)

    async def list_conflicts(
        self,
        *,
        subject_user_id: str | None = None,
        group_id: str | None = None,
        limit: int = 100,
    ) -> tuple[MemoryFact, ...]:
        return await self._repository.list_conflicts(
            subject_user_id=subject_user_id,
            group_id=group_id,
            limit=limit,
        )

    async def explain(self, fact_id: int) -> dict[str, Any] | None:
        fact = await self.get_fact(fact_id)
        if fact is None:
            return None
        evidence = await self.get_evidence(fact_id)
        relations = await self.get_relations(fact_id)
        state_events = await self.get_state_history(fact_id)
        chain = await self.get_supersession_chain(fact_id)
        return {
            "fact_id": fact.id,
            "status": fact.status.value,
            "scope_type": fact.scope_type.value,
            "authority": fact.authority.value,
            "confidence": fact.confidence,
            "conflict_state": fact.conflict_state.value,
            "evidence_count": len(evidence),
            "evidence": [
                {
                    "relation": row.relation.value,
                    "authority": row.authority.value,
                    "confidence": row.confidence,
                    "created_at": row.created_at.isoformat(),
                }
                for row in evidence[-20:]
            ],
            "last_confirmed_at": fact.last_confirmed_at.isoformat(),
            "last_injected_at": (
                fact.last_injected_at.isoformat() if fact.last_injected_at is not None else None
            ),
            "supersession_chain": [row.id for row in chain],
            "relations": [
                {
                    "source_fact_id": row.source_fact_id,
                    "target_fact_id": row.target_fact_id,
                    "relation_type": row.relation_type.value,
                    "confidence": row.confidence,
                }
                for row in relations
            ],
            "state_events": [
                {
                    "action": row.action.value,
                    "reason_code": row.reason_code,
                    "created_at": row.created_at.isoformat(),
                }
                for row in state_events[-20:]
            ],
        }

    async def health(self) -> MemoryConsistencyHealth:
        queries = {
            "contested_fact_count": "SELECT COUNT(*) FROM memory_facts WHERE status='contested'",
            "active_contested_count": """
                SELECT COUNT(*) FROM memory_facts
                WHERE status='active' AND conflict_state='contested'
            """,
            "orphan_relation_count": """
                SELECT COUNT(*) FROM memory_fact_relations r
                LEFT JOIN memory_facts s ON s.id=r.source_fact_id
                LEFT JOIN memory_facts t ON t.id=r.target_fact_id
                WHERE s.id IS NULL OR t.id IS NULL
            """,
            "cross_target_relation_count": """
                SELECT COUNT(*) FROM memory_fact_relations r
                JOIN memory_facts s ON s.id=r.source_fact_id
                JOIN memory_facts t ON t.id=r.target_fact_id
                WHERE s.scope_type != t.scope_type
                    OR COALESCE(s.canonical_subject_person_id, '')
                        != COALESCE(t.canonical_subject_person_id, '')
                    OR COALESCE(s.canonical_subject_space_id, '')
                        != COALESCE(t.canonical_subject_space_id, '')
                    OR COALESCE(s.visibility_type, '') != COALESCE(t.visibility_type, '')
                    OR COALESCE(s.canonical_visibility_person_id, '')
                        != COALESCE(t.canonical_visibility_person_id, '')
                    OR COALESCE(s.canonical_visibility_space_id, '')
                        != COALESCE(t.canonical_visibility_space_id, '')
            """,
            "orphan_state_event_count": """
                SELECT COUNT(*) FROM memory_fact_state_events e
                LEFT JOIN memory_facts f ON f.id=e.fact_id WHERE f.id IS NULL
            """,
            "invalidated_without_reason_count": """
                SELECT COUNT(*) FROM memory_facts
                WHERE status='invalidated' AND invalidated_reason IS NULL
            """,
            "superseded_without_chain_count": """
                SELECT COUNT(*) FROM memory_facts
                WHERE status='superseded' AND id NOT IN (
                    SELECT supersedes_id FROM memory_facts WHERE supersedes_id IS NOT NULL
                ) AND id NOT IN (
                    SELECT source_fact_id FROM memory_fact_relations
                    WHERE relation_type IN ('equivalent', 'refines')
                )
            """,
            "expired_active_count": """
                SELECT COUNT(*) FROM memory_facts
                WHERE status IN ('active','contested')
                    AND valid_until IS NOT NULL AND valid_until <= :now
            """,
        }
        now = datetime.now(UTC)
        values: dict[str, int] = {}
        async with self._repository.database.sessions() as session:
            for key, sql in queries.items():
                values[key] = int((await session.scalar(text(sql), {"now": now})) or 0)
        return MemoryConsistencyHealth(
            **values,
            maintenance_last_success_at=self._metrics.maintenance_last_success_at,
        )
