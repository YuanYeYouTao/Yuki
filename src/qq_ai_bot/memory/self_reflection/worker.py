"""Three-times-daily, bounded Yuki self-reflection scheduler."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from qq_ai_bot.config import Settings
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.self_reflection.models import (
    SelfReflectionCycleResult,
    SelfReflectionHealth,
)
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.self_reflection.service import SelfReflectionService
from qq_ai_bot.model_runtime.structured import StructuredTaskError

logger = logging.getLogger(__name__)

_MINIMUM_STALE_RUN_SECONDS = 3600.0


class SelfReflectionWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: SelfReflectionRepository,
        service: SelfReflectionService,
        metrics: MemoryLifecycleMetrics,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._service = service
        self._metrics = metrics
        self._hours = frozenset(
            int(item.strip()) for item in settings.memory_self_reflection_schedule_hours.split(",")
        )
        self._timezone = ZoneInfo(settings.memory_self_reflection_timezone)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._process_lock = asyncio.Lock()

    async def start(self) -> None:
        if not self._settings.memory_self_reflection_enabled or self._task is not None:
            return
        self._stop.clear()
        await self._repository.scan_new_events()
        self._task = asyncio.create_task(self._run(), name="memory-self-reflection-worker")
        logger.info(
            "memory_self_reflection_started schedule_hours=%s timezone=%s "
            "max_batches=%d max_batches_per_conversation=%d max_daily_calls=%d",
            ",".join(str(item) for item in sorted(self._hours)),
            self._settings.memory_self_reflection_timezone,
            self._settings.memory_self_reflection_max_batches_per_run,
            self._settings.memory_self_reflection_max_batches_per_conversation_per_run,
            self._settings.memory_self_reflection_max_daily_calls,
        )

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run_now(self) -> SelfReflectionCycleResult:
        """Run one manual bounded cycle without waiting for a scheduled hour."""

        if not self._settings.memory_self_reflection_enabled:
            raise RuntimeError("Self Reflection 当前未启用")
        if self._process_lock.locked():
            raise RuntimeError("Self Reflection 当前正在运行")
        async with self._process_lock:
            return await self._process_cycle(now=None, force=True)

    async def process_once(
        self,
        now: datetime | None = None,
        *,
        force: bool = False,
    ) -> int:
        async with self._process_lock:
            result = await self._process_cycle(now=now, force=force)
            return result.completed_batches

    async def _process_cycle(
        self,
        *,
        now: datetime | None,
        force: bool,
    ) -> SelfReflectionCycleResult:
        stale_seconds = max(
            _MINIMUM_STALE_RUN_SECONDS,
            float(self._settings.llm_timeout_seconds) * 4,
        )
        recovered = await self._repository.recover_stale_runs(
            started_before=datetime.now(UTC) - timedelta(seconds=stale_seconds)
        )
        if recovered:
            logger.warning("memory_self_reflection_stale_runs_recovered count=%d", recovered)
        await self._repository.scan_new_events()
        local = (now or datetime.now(UTC)).astimezone(self._timezone)
        if not force and local.hour not in self._hours:
            return SelfReflectionCycleResult()
        base_slot = (
            f"{local.date().isoformat()}:m:{local.strftime('%H%M%S%f')}"
            if force
            else f"{local.date().isoformat()}:{local.hour:02d}"
        )
        attempted = 0
        completed = 0
        failed = 0
        proposal_count = 0
        committed_count = 0
        failed_conversation_keys: set[str] = set()
        for round_index in range(
            self._settings.memory_self_reflection_max_batches_per_conversation_per_run
        ):
            remaining = self._settings.memory_self_reflection_max_batches_per_run - attempted
            if remaining <= 0:
                break
            batches = await self._repository.claim_due(
                scheduled_slot=f"{base_slot}:r{round_index + 1:02d}",
                local_date=local.date().isoformat(),
                event_threshold=self._settings.memory_self_reflection_event_threshold,
                character_threshold=self._settings.memory_self_reflection_character_threshold,
                low_event_threshold=self._settings.memory_self_reflection_low_event_threshold,
                low_character_threshold=(
                    self._settings.memory_self_reflection_low_character_threshold
                ),
                natural_gap_seconds=self._settings.memory_self_reflection_natural_gap_seconds,
                max_wait_seconds=self._settings.memory_self_reflection_max_wait_seconds,
                max_sessions=remaining,
                max_daily_calls=self._settings.memory_self_reflection_max_daily_calls,
                max_events=self._settings.memory_self_reflection_max_events,
                max_characters=self._settings.memory_self_reflection_max_characters,
                context_events=4,
                force=force,
                excluded_conversation_keys=frozenset(failed_conversation_keys),
            )
            if not batches:
                break
            attempted += len(batches)
            for batch in batches:
                try:
                    proposals, committed = await self._service.reflect(batch)
                    await self._repository.complete(
                        batch,
                        proposals=proposals,
                        committed=committed,
                    )
                    completed += 1
                    proposal_count += proposals
                    committed_count += committed
                    logger.info(
                        "memory_self_reflection_completed run_id=%d trigger=%s "
                        "events=%d proposals=%d committed=%d",
                        batch.run_id,
                        batch.trigger_reason,
                        len(batch.events),
                        proposals,
                        committed,
                    )
                except asyncio.CancelledError:
                    try:
                        outcome = await self._repository.recover_interrupted(
                            batch.run_id,
                            "cancelled",
                        )
                        logger.info(
                            "memory_self_reflection_cancelled run_id=%d recovery=%s",
                            batch.run_id,
                            outcome or "already_finalized",
                        )
                    except (OSError, RuntimeError, ValueError) as recovery_exc:
                        logger.error(
                            "memory_self_reflection_cancel_recovery_failed "
                            "run_id=%d error_category=%s",
                            batch.run_id,
                            type(recovery_exc).__name__,
                        )
                    raise
                except (OSError, RuntimeError, ValueError) as exc:
                    error_category = _error_category(exc)
                    await self._repository.fail(batch.run_id, error_category)
                    failed_conversation_keys.add(batch.state.conversation_key_hash)
                    failed += 1
                    self._metrics.increment("self_reflection_failed")
                    logger.warning(
                        "memory_self_reflection_failed run_id=%d trigger=%s error_category=%s "
                        "error_detail=%s",
                        batch.run_id,
                        batch.trigger_reason,
                        error_category,
                        _error_detail(exc),
                    )
        await self._repository.cleanup_receipts()
        return SelfReflectionCycleResult(
            attempted_batches=attempted,
            completed_batches=completed,
            failed_batches=failed,
            proposal_count=proposal_count,
            committed_count=committed_count,
        )

    async def health(self) -> SelfReflectionHealth:
        local = datetime.now(UTC).astimezone(self._timezone)
        pending, calls, status, completed_at = await self._repository.health_snapshot(
            local_date=local.date().isoformat()
        )
        return SelfReflectionHealth(
            enabled=self._settings.memory_self_reflection_enabled,
            running=self._task is not None and not self._task.done(),
            schedule_hours=tuple(sorted(self._hours)),
            timezone=self._settings.memory_self_reflection_timezone,
            pending_conversations=pending,
            calls_today=calls,
            last_run_status=status,
            last_run_completed_at=completed_at,
        )

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning(
                    "memory_self_reflection_cycle_failed error_category=%s",
                    type(exc).__name__,
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._settings.memory_self_reflection_poll_seconds,
                )
            except TimeoutError:
                pass


def _error_category(exc: BaseException) -> str:
    if isinstance(exc, StructuredTaskError):
        return f"StructuredTaskError:{exc.reason_code}"[:64]
    return type(exc).__name__[:64]


def _error_detail(exc: BaseException) -> str:
    if isinstance(exc, StructuredTaskError):
        detail = exc.detail or "none"
        return f"attempts={exc.attempts} {detail}"[:600]
    return "none"
