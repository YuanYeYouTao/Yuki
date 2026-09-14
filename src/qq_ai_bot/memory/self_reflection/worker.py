"""Serial durable reflection cycles, fair drain and source-batch retries."""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections import Counter
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from qq_ai_bot.config import Settings
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.self_reflection.control import (
    ReflectionControlRepository,
    ReflectionDailyLimit,
)
from qq_ai_bot.memory.self_reflection.models import SelfReflectionHealth
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.self_reflection.service import SelfReflectionService
from qq_ai_bot.model_runtime.structured import StructuredTaskError

logger = logging.getLogger(__name__)


class SelfReflectionWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: SelfReflectionRepository,
        service: SelfReflectionService,
        metrics: MemoryLifecycleMetrics,
    ) -> None:
        self._settings, self._repository, self._service, self._metrics = (
            settings,
            repository,
            service,
            metrics,
        )
        self.control = ReflectionControlRepository(repository.database, settings)
        self._hours = frozenset(
            int(v.strip()) for v in settings.memory_self_reflection_schedule_hours.split(",")
        )
        self._timezone = ZoneInfo(settings.memory_self_reflection_timezone)
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._process_lock = asyncio.Lock()
        self.social: Any = None

    async def start(self) -> None:
        if not self._settings.memory_self_reflection_enabled or (
            self._task and not self._task.done()
        ):
            return
        self._stop.clear()
        # There is one Bot/worker; old processing claims now belong to this restart.
        await self._repository.recover_stale_runs(started_before=datetime.now(UTC))
        await self._repository.scan_new_events()
        self._task = asyncio.create_task(self._run(), name="memory-self-reflection-worker")
        logger.info(
            "memory_self_reflection_started max_batches=%d per_conversation=%d "
            "daily_requests=%d output_tokens=%d timeout=%s drain=%s",
            self._settings.memory_self_reflection_max_batches_per_run,
            self._settings.memory_self_reflection_max_batches_per_conversation_per_run,
            self._settings.memory_self_reflection_max_daily_calls,
            self._settings.memory_self_reflection_max_output_tokens,
            self._settings.memory_self_reflection_timeout_seconds,
            self._settings.memory_self_reflection_drain_enabled,
        )

    async def close(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run_now(self, *, source_event_id: int, conversation_id: str) -> dict[str, Any]:
        if not self._settings.memory_self_reflection_enabled:
            raise RuntimeError("Self Reflection 当前未启用")
        cycle = await self.control.enqueue(
            trigger="manual",
            key=f"event:{conversation_id}:{source_event_id}",
            source_event_id=source_event_id,
            conversation_id=conversation_id,
        )
        self._wake.set()
        return cycle

    async def process_once(self, now: datetime | None = None, *, force: bool = False) -> int:
        async with self._process_lock:
            await self._repository.scan_new_events()
            local = (now or datetime.now(UTC)).astimezone(self._timezone)
            snapshot = await self.control.snapshot()
            base = f"{local.date().isoformat()}:{local.hour:02d}"
            if local.hour in self._hours:
                await self.control.enqueue(trigger="scheduled", key=base)
            if force:
                await self.control.enqueue(trigger="drain", key=f"force:{local.isoformat()}")
            elif snapshot["calls_today"] < snapshot["daily_limit"]:
                events = snapshot["actionable"]["events"]
                active = events >= self._settings.memory_self_reflection_drain_high_events or (
                    events >= self._settings.memory_self_reflection_drain_low_events
                    and await self.control.drain_active()
                )
                if self._settings.memory_self_reflection_drain_enabled and active:
                    slot = int(
                        local.timestamp()
                        // self._settings.memory_self_reflection_drain_interval_seconds
                    )
                    await self.control.enqueue(trigger="drain", key=f"drain:{slot}")
                elif await self.control.retry_due():
                    await self.control.enqueue(
                        trigger="retry", key=f"retry:{int(local.timestamp() // 300)}"
                    )
            cycle = await self.control.claim()
            count = await self._process_cycle(cycle, local) if cycle else 0
            await self._deliver_reports()
            return count

    async def _process_cycle(self, cycle: dict[str, Any], local: datetime) -> int:
        rows = await self.control.cycle_runs(cycle["id"])
        seen = Counter(r["owner"] for r in rows)
        attempted = len(rows)
        exhausted = False
        while (
            attempted < self._settings.memory_self_reflection_max_batches_per_run
            and not self._stop.is_set()
        ):
            await self._repository.scan_new_events()
            snapshot = await self.control.snapshot()
            if snapshot["calls_today"] >= snapshot["daily_limit"]:
                exhausted = True
                break
            # Rotate through every owner before granting its next share.
            floor = min(seen.values(), default=0)
            excluded = frozenset(
                k
                for k, count in seen.items()
                if count > floor
                or count
                >= self._settings.memory_self_reflection_max_batches_per_conversation_per_run
            )
            batch = None
            for exclusion in (
                frozenset(seen),
                excluded,
                frozenset(
                    k
                    for k, v in seen.items()
                    if v
                    >= self._settings.memory_self_reflection_max_batches_per_conversation_per_run
                ),
            ):
                batches = await self._repository.claim_due(
                    scheduled_slot=f"{local.date().isoformat()}:{cycle['id'][3:15]}:{attempted}",
                    local_date=local.date().isoformat(),
                    event_threshold=self._settings.memory_self_reflection_event_threshold,
                    character_threshold=self._settings.memory_self_reflection_character_threshold,
                    low_event_threshold=self._settings.memory_self_reflection_low_event_threshold,
                    low_character_threshold=self._settings.memory_self_reflection_low_character_threshold,
                    natural_gap_seconds=self._settings.memory_self_reflection_natural_gap_seconds,
                    max_wait_seconds=self._settings.memory_self_reflection_max_wait_seconds,
                    max_sessions=1,
                    max_daily_calls=self._settings.memory_self_reflection_max_daily_calls,
                    max_events=self._settings.memory_self_reflection_max_events,
                    max_characters=self._settings.memory_self_reflection_max_characters,
                    force=cycle["trigger"] == "retry",
                    excluded_conversation_keys=exclusion,
                    cycle_id=cycle["id"],
                    bot_display_name=self._settings.bot_display_name,
                    timezone=self._settings.memory_self_reflection_timezone,
                )
                if batches:
                    batch = batches[0]
                    break
            if batch is None:
                break
            attempted += 1
            seen[batch.state.conversation_key_hash] += 1
            try:
                proposals, committed = await self._service.reflect(batch)
                await self._repository.complete(batch, proposals=proposals, committed=committed)
                self._metrics.increment("self_reflection_processed_events_total", len(batch.events))
            except asyncio.CancelledError:
                await self._repository.recover_interrupted(batch.run_id, "interrupted")
                raise
            except Exception as exc:
                category = _error_category(exc)
                await self._repository.recover_interrupted(batch.run_id, category)
                self._metrics.increment(f"self_reflection_error_{category}")
                logger.warning(
                    "memory_self_reflection_failed run_id=%d error_category=%s",
                    batch.run_id,
                    category,
                )
                if isinstance(exc, ReflectionDailyLimit):
                    exhausted = True
                    break
        rows = await self.control.cycle_runs(cycle["id"])
        completed = [r for r in rows if r["status"] == "completed"]
        deferred = [r for r in rows if r["error"] in ("daily_limit_reached", "preempted")]
        failed = [r for r in rows if r["status"] == "failed" and r not in deferred]
        errors = dict(Counter(r["error"] for r in failed))
        report = {
            "attempted_batches": len(rows),
            "completed_batches": len(completed),
            "failed_batches": len(failed),
            "deferred_batches": len(deferred),
            "processed_events": sum(r["events"] for r in completed),
            "processed_characters": sum(r["characters"] for r in completed),
            "proposal_count": sum(r["proposals"] for r in rows),
            "committed_count": sum(r["committed"] for r in rows),
            "errors": errors,
            "failures": [{**r, "checkpoint_advanced": False} for r in failed],
            "limit_flags": {
                "daily": exhausted,
                "batches": len(rows) >= self._settings.memory_self_reflection_max_batches_per_run,
                "output": "output_budget_exhausted" in errors,
                "per_conversation": any(
                    count
                    >= self._settings.memory_self_reflection_max_batches_per_conversation_per_run
                    for count in seen.values()
                ),
            },
            "reason": "daily_limit_reached"
            if exhausted
            else "processed"
            if rows
            else "all_due_batches_waiting_retry"
            if snapshot["waiting_retry"]["events"]
            else "all_pending_policy_ineligible"
            if snapshot["policy_ineligible"]["conversations"]
            else "all_pending_recent_not_due"
            if snapshot["recent_not_due"]["events"]
            else "no_actionable_backlog",
        }
        await self._repository.scan_new_events()
        result = await self.control.finish(cycle["id"], report)
        after = result["after"]
        if (
            after["actionable"]["events"]
            >= self._settings.memory_self_reflection_drain_critical_events
            or after["isolated"]["events"]
            or report["limit_flags"]["output"]
            or after["oldest_actionable_age_seconds"] > 28800
            or after["three_cycles_without_decrease"]
        ):
            logger.warning(
                "self_reflection_backlog_alert cycle_id=%s actionable=%d "
                "isolated=%d output_budget=%s",
                cycle["id"],
                after["actionable"]["events"],
                after["isolated"]["events"],
                report["limit_flags"]["output"],
            )
        await self._repository.cleanup_receipts()
        return len(completed)

    async def health(self) -> SelfReflectionHealth:
        snap = await self.control.snapshot()
        pending, _, status, completed_at = await self._repository.health_snapshot(
            local_date=datetime.now(UTC).astimezone(self._timezone).date().isoformat()
        )
        return SelfReflectionHealth(
            enabled=self._settings.memory_self_reflection_enabled,
            running=self._task is not None and not self._task.done(),
            schedule_hours=tuple(sorted(self._hours)),
            timezone=self._settings.memory_self_reflection_timezone,
            pending_conversations=pending,
            calls_today=snap["calls_today"],
            last_run_status=status,
            last_run_completed_at=completed_at,
            backlog=snap,
        )

    async def _deliver_reports(self) -> None:
        if self.social is None:
            return
        from qq_ai_bot.memory.self_reflection.reporting import deliver_report

        for cycle in await self.control.pending_reports():
            try:
                receipt = await deliver_report(self.social, cycle)
                await self.control.delivered(cycle["id"], receipt)
                if receipt.get("status") != "succeeded":
                    logger.warning(
                        "self_reflection_report_not_delivered cycle_id=%s status=%s",
                        cycle["id"],
                        receipt.get("status", "unknown"),
                    )
            except Exception as exc:
                logger.warning(
                    "self_reflection_report_delivery_failed cycle_id=%s error_category=%s",
                    cycle["id"],
                    type(exc).__name__,
                )

    async def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "memory_self_reflection_cycle_failed error_category=%s error_detail=%s",
                    type(exc).__name__,
                    _error_detail(exc),
                )
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._settings.memory_self_reflection_poll_seconds
                )
            except TimeoutError:
                pass


def _error_category(exc: BaseException) -> str:
    if isinstance(exc, StructuredTaskError):
        return {
            "schema_validation": "json_schema_validation",
            "unknown_reference": "reference_validation",
        }.get(exc.reason_code, exc.reason_code)[:64]
    return {
        "LLMTimeoutError": "timeout",
        "LLMEmptyResponseError": "empty_response",
        "ReflectionDailyLimit": "daily_limit_reached",
        "BackgroundModelPreempted": "preempted",
        "RuntimeError": "mutation_failure",
    }.get(type(exc).__name__, type(exc).__name__)[:64]


def _error_detail(exc: BaseException) -> str:
    if isinstance(exc, StructuredTaskError):
        return f"attempts={exc.attempts} reason={exc.reason_code}"
    # Do not log exception strings, source lines, locals, or absolute paths: database
    # errors can embed memory contents. Function names and line numbers locate bugs.
    return (
        ",".join(
            f"{frame.name}:{frame.lineno}" for frame in traceback.extract_tb(exc.__traceback__)[-6:]
        )
        or "none"
    )
