"""Restart-safe polling worker with database leases and misfire handling."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from qq_ai_bot.automation.executor import AutomationExecutor
from qq_ai_bot.automation.models import RunStatus
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.config import Settings
from qq_ai_bot.time.schedules import schedule_after_completion
from qq_ai_bot.time.service import TimeContextService

logger = logging.getLogger(__name__)


class AutomationWorker:
    """Lease due tasks, execute each slot once, and advance periodic schedules."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: AutomationRepository,
        executor: AutomationExecutor,
        time_service: TimeContextService,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._executor = executor
        self._time = time_service
        self._worker_id = uuid.uuid4().hex
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._running: set[asyncio.Task[None]] = set()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self._settings.automation_enabled or self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="automation-worker")

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        if self._running:
            _done, pending = await asyncio.wait(
                self._running,
                timeout=float(self._settings.automation_max_runtime_seconds),
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                rows = await self._repository.claim_due(
                    worker_id=self._worker_id,
                    now=self._time.clock.now(),
                    lease_seconds=self._settings.automation_lease_seconds,
                )
                for row in rows:
                    task = asyncio.create_task(
                        self._process_guarded(row), name=f"automation-{row.id}"
                    )
                    self._running.add(task)
                    task.add_done_callback(self._running.discard)
            except Exception as exc:
                logger.error("automation_poll_failed category=%s", type(exc).__name__, exc_info=exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._settings.automation_poll_seconds
                )
            except TimeoutError:
                continue

    async def _process_guarded(self, automation: Any) -> None:
        """Contain unexpected task failures and release the lease for recovery."""

        try:
            await self._process(automation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "automation_process_failed automation_id=%s category=%s",
                getattr(automation, "id", "unknown"),
                type(exc).__name__,
                exc_info=exc,
            )
            try:
                await self._repository.release_claim(
                    automation.id,
                    worker_id=self._worker_id,
                    next_run_at=automation.next_run_at,
                )
            except Exception as release_exc:
                logger.error(
                    "automation_claim_release_failed automation_id=%s category=%s",
                    getattr(automation, "id", "unknown"),
                    type(release_exc).__name__,
                )

    async def _process(self, automation: Any) -> None:
        scheduled_for = automation.next_run_at
        if scheduled_for is None:
            await self._repository.release_claim(automation.id, worker_id=self._worker_id)
            return
        now = self._time.clock.now()
        lateness = (now - scheduled_for).total_seconds()
        next_run = schedule_after_completion(
            automation.script.schedule,
            scheduled_for,
            now,
            automation.timezone,
        )
        if lateness > automation.misfire_grace_seconds:
            run = await self._repository.create_run(
                automation.id,
                scheduled_for=scheduled_for,
                actual_started_at=now,
            )
            if run is not None:
                await self._repository.finish_run(
                    run.id,
                    status=RunStatus.MISSED,
                    steps_completed=0,
                    llm_calls=0,
                    tool_calls=0,
                    messages_sent=0,
                    error_category="misfire_grace_exceeded",
                    summary={},
                    finished_at=now,
                )
            await self._repository.finish_automation_run(
                automation.id,
                worker_id=self._worker_id,
                status=RunStatus.MISSED,
                next_run_at=next_run,
                now=now,
                max_consecutive_failures=self._settings.automation_max_consecutive_failures,
            )
            return
        run = await self._repository.create_run(
            automation.id,
            scheduled_for=scheduled_for,
            actual_started_at=now,
        )
        if run is None:
            await self._repository.release_claim(
                automation.id, worker_id=self._worker_id, next_run_at=next_run
            )
            return
        result = await self._executor.execute(automation, run)
        finished = self._time.clock.now()
        await self._repository.finish_run(
            run.id,
            status=result.status,
            steps_completed=result.steps_completed,
            llm_calls=result.llm_calls,
            tool_calls=result.tool_calls,
            messages_sent=result.messages_sent,
            error_category=result.error_category,
            summary=result.summary,
            finished_at=finished,
        )
        await self._repository.finish_automation_run(
            automation.id,
            worker_id=self._worker_id,
            status=result.status,
            next_run_at=next_run,
            now=finished,
            max_consecutive_failures=self._settings.automation_max_consecutive_failures,
        )
        logger.info(
            "automation_run_finished automation_id=%d run_id=%d creator_user_id=%s "
            "bot_user_id=%s schedule_type=%s status=%s error_category=%s "
            "messages_sent=%d llm_calls=%d tool_calls=%d",
            automation.id,
            run.id,
            automation.creator_user_id,
            automation.bot_user_id,
            automation.script.schedule.type,
            result.status.value,
            result.error_category,
            result.messages_sent,
            result.llm_calls,
            result.tool_calls,
        )
