"""Restart-safe bounded background reflection over deterministic memory anomalies."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from qq_ai_bot.config import Settings
from qq_ai_bot.memory.enums import MemoryAuthority, MemoryStatus
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import MemoryFact
from qq_ai_bot.memory.mutation.models import (
    MemoryMutationOperation,
    MemoryMutationOutcome,
)
from qq_ai_bot.memory.mutation.service import MemoryMutationService
from qq_ai_bot.memory.reflection.models import MemoryReflectionIssue, MemoryReflectionJob
from qq_ai_bot.memory.reflection.repository import MemoryReflectionRepository
from qq_ai_bot.memory.service import MemoryFactService

logger = logging.getLogger(__name__)

_AUTHORITY_RANK = {
    MemoryAuthority.THIRD_PARTY: 0,
    MemoryAuthority.GROUP_REPORT: 1,
    MemoryAuthority.SELF_REPORT: 2,
    MemoryAuthority.AGENT_REFLECTION: 3,
    MemoryAuthority.EXPLICIT: 4,
}


class MemoryReflectionWorker:
    """Discover, claim, retry, and recover deterministic governance tasks."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: MemoryReflectionRepository,
        facts: MemoryFactService,
        mutations: MemoryMutationService,
        metrics: MemoryLifecycleMetrics | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._facts = facts
        self._mutations = mutations
        self.metrics = metrics or MemoryLifecycleMetrics()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="memory-reflection-worker")

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._settings.memory_maintenance_interval_seconds,
                )
            except TimeoutError:
                pass
            self._wake.clear()
            if not self._stop.is_set() and self._settings.memory_maintenance_enabled:
                await self.process_once()

    async def process_once(self) -> int:
        """Run one bounded discovery and claimed-job batch."""

        if not self._settings.memory_maintenance_enabled:
            return 0
        now = datetime.now(UTC)
        stale_after = max(60.0, self._settings.memory_maintenance_interval_seconds * 2)
        recovered = await self._repository.recover_stale(
            before=now - timedelta(seconds=stale_after),
            now=now,
        )
        if recovered:
            self.metrics.increment("reflection_jobs_recovered", recovered)
        limit = self._settings.memory_maintenance_batch_limit
        candidates = await self._repository.discover(limit=limit)
        discovered = await self._repository.enqueue(candidates, now=now)
        if discovered:
            self.metrics.increment("reflection_jobs_discovered", discovered)
        jobs = await self._repository.claim(limit=limit, now=now)
        if jobs:
            self.metrics.increment("reflection_jobs_claimed", len(jobs))
        completed = 0
        for job in jobs:
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                await self._repository.fail(job.id, type(exc).__name__)
                self.metrics.increment("reflection_jobs_failed")
                logger.warning(
                    "memory_reflection_job_failed job_id=%d issue=%s category=%s",
                    job.id,
                    job.issue_type.value,
                    type(exc).__name__,
                    exc_info=True,
                )
                continue
            await self._repository.complete(job.id)
            self.metrics.increment("reflection_jobs_completed")
            completed += 1
        return completed

    async def _process(self, job: MemoryReflectionJob) -> None:
        fact = await self._facts.get_fact(job.fact_id)
        if fact is None or fact.status not in {MemoryStatus.ACTIVE, MemoryStatus.CONTESTED}:
            return
        if job.issue_type is MemoryReflectionIssue.DUPLICATE:
            related = (
                await self._facts.get_fact(job.related_fact_id)
                if job.related_fact_id is not None
                else None
            )
            if related is None or not self._still_duplicates(fact, related):
                return
            source, target = self._merge_order(fact, related)
            result = await self._mutations.mutate_reflection(
                source,
                operation=MemoryMutationOperation.MERGE,
                reason="reflection_duplicate",
                merge_fact_id=target.id,
            )
            if result.outcome is MemoryMutationOutcome.REJECTED:
                raise RuntimeError(result.reason_code)
            if result.ok:
                self.metrics.increment("reflection_duplicates_merged")
            return
        reason = (
            "reflection_attribution_anomaly"
            if job.issue_type is MemoryReflectionIssue.ATTRIBUTION
            else "reflection_contested_review"
        )
        result = await self._mutations.mutate_reflection(
            fact,
            operation=MemoryMutationOperation.CONTEST,
            reason=reason,
        )
        if result.outcome is MemoryMutationOutcome.REJECTED:
            raise RuntimeError(result.reason_code)
        if job.issue_type is MemoryReflectionIssue.ATTRIBUTION and result.ok:
            self.metrics.increment("reflection_attributions_contested")

    @staticmethod
    def _still_duplicates(first: MemoryFact, second: MemoryFact) -> bool:
        return bool(
            first.id != second.id
            and second.status in {MemoryStatus.ACTIVE, MemoryStatus.CONTESTED}
            and first.scope_type is second.scope_type
            and first.canonical_subject_person_id == second.canonical_subject_person_id
            and first.canonical_subject_space_id == second.canonical_subject_space_id
            and first.canonical_visibility_person_id == second.canonical_visibility_person_id
            and first.canonical_visibility_space_id == second.canonical_visibility_space_id
            and first.kind is second.kind
            and first.memory_key != second.memory_key
            and first.normalized_content
            and first.normalized_content == second.normalized_content
        )

    @staticmethod
    def _merge_order(first: MemoryFact, second: MemoryFact) -> tuple[MemoryFact, MemoryFact]:
        def score(fact: MemoryFact) -> tuple[int, int, float, int, float, int]:
            confirmed = fact.last_confirmed_at
            if confirmed.tzinfo is None:
                confirmed = confirmed.replace(tzinfo=UTC)
            return (
                1 if fact.status is MemoryStatus.ACTIVE else 0,
                _AUTHORITY_RANK[fact.authority],
                fact.confidence,
                fact.evidence_count,
                confirmed.timestamp(),
                -fact.id,
            )

        return (second, first) if score(first) >= score(second) else (first, second)
