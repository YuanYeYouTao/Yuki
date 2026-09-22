"""Persistent background worker for slow relationship score changes."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.relationships import RelationshipEvaluation
from qq_ai_bot.llm.base import LLMError
from qq_ai_bot.model_runtime.executor import BackgroundModelPreempted
from qq_ai_bot.persistence.relationship_repository import RelationshipClaimLost
from qq_ai_bot.persistence.repositories import (
    RelationshipJobRecord,
    RelationshipJobRepository,
    RelationshipRepository,
)
from qq_ai_bot.services.relationship_evaluator import (
    RelationshipEvaluator,
    validate_evaluation,
)

logger = logging.getLogger(__name__)


class RelationshipWorker:
    """Wake by interval or queue threshold and process at most ten turns."""

    def __init__(
        self,
        *,
        settings: Settings,
        jobs: RelationshipJobRepository,
        relationships: RelationshipRepository,
        evaluator: RelationshipEvaluator,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._relationships = relationships
        self._evaluator = evaluator
        self._runtime_config = runtime_config or RuntimeConfigService(
            settings=settings,
            database=relationships._database,
        )
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._queued_since_wake = 0

    async def start(self) -> None:
        if self._settings.relationship_enabled and self._task is None:
            self._task = asyncio.create_task(self._run(), name="relationship-worker")

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def enqueue(
        self,
        *,
        trigger_event_id: int,
        user_id: str,
        conversation_key: str,
    ) -> None:
        if not self._settings.relationship_enabled:
            return
        await self._jobs.enqueue(
            trigger_event_id=trigger_event_id,
            user_id=user_id,
            conversation_key=conversation_key,
        )
        self._queued_since_wake += 1
        if self._queued_since_wake >= self._settings.relationship_batch_trigger_count:
            self._queued_since_wake = 0
            self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._settings.relationship_batch_seconds,
                )
            except TimeoutError:
                pass
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                await self.process_once()
            except (SQLAlchemyError, OSError, RuntimeError) as exc:
                logger.error("relationship_worker_loop_failed", exc_info=exc)

    async def process_once(self) -> int:
        if not self._settings.relationship_enabled:
            return 0
        jobs = await self._jobs.claim(limit=self._settings.relationship_batch_max_turns)
        if not jobs:
            return 0
        try:
            return await self._process_claimed(jobs)
        except asyncio.CancelledError:
            # Cancellation is not an evaluation failure. Release only surviving
            # claims; already committed scores and newer owners stay untouched.
            try:
                await self._jobs.defer(jobs)
            except (SQLAlchemyError, OSError, RuntimeError) as exc:
                logger.warning("relationship_cancel_release_failed", exc_info=exc)
            raise

    async def _process_claimed(self, jobs: tuple[RelationshipJobRecord, ...]) -> int:
        try:
            evaluations = await self._evaluator.evaluate(jobs)
        except BackgroundModelPreempted:
            await self._jobs.defer(jobs)
            logger.info("relationship_batch_preempted count=%d", len(jobs))
            return 0
        except (LLMError, OSError, RuntimeError, TypeError, ValueError) as exc:
            category = type(exc).__name__
            logger.warning("relationship_batch_failed exception_category=%s", category)
            for job in jobs:
                await self._jobs.fail(job, category)
            return 0

        completed = 0
        for job in jobs:
            runtime = await self._runtime_config.snapshot(
                user_id=job.user_id,
                group_id=job.trigger_event.group_id,
            )
            raw = evaluations.get(
                job.job_id,
                RelationshipEvaluation(0, 0, "neutral", 0.0),
            )
            evaluation = validate_evaluation(
                job,
                raw,
                confidence_threshold=runtime.relationship.confidence_threshold,
                affection_max_delta=runtime.relationship.max_auto_delta,
                trust_max_delta=runtime.relationship.max_auto_delta,
            )
            try:
                await self._relationships.apply_automatic(
                    user_id=job.user_id,
                    source_event_id=job.trigger_event.id,
                    evaluation=evaluation,
                    max_auto_delta=runtime.relationship.max_auto_delta,
                    daily_positive_cap=runtime.relationship.daily_positive_cap,
                    daily_negative_cap=runtime.relationship.daily_negative_cap,
                    claim=job,
                )
                completed += 1
            except RelationshipClaimLost:
                logger.info("relationship_claim_lost job_id=%d", job.job_id)
            except (SQLAlchemyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                category = type(exc).__name__
                logger.warning(
                    "relationship_job_failed job_id=%d exception_category=%s",
                    job.job_id,
                    category,
                )
                await self._jobs.fail(job, category)
        return completed
