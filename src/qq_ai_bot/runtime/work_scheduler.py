"""Bounded selection and maintenance for persisted root work."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import func, or_, select

from qq_ai_bot.persistence.sqlite_diagnostics import TimingSummary
from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.work_recovery_schema import deliveries, recovery
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import inputs, scope, work
from qq_ai_bot.runtime.work_wait import WorkWaitRepository
from qq_ai_bot.runtime.work_wait_schema import waits

logger = logging.getLogger(__name__)


# Resume one selected Work; returns that run's error category (None on success).
ResumeWork = Callable[[dict[str, Any]], Awaitable[str | None]]

# Selection limit doubles as the cap on concurrently dispatched root scopes.
_MAX_IN_FLIGHT = 8


class WorkScheduler:
    def __init__(
        self,
        repository: WorkRepository,
        resume: ResumeWork,
        *,
        chat_admission_enabled: bool,
    ) -> None:
        self.repository = repository
        self._resume = resume
        # One task per conversation scope; it resumes that scope's selected items in order.
        self._in_flight: dict[str, asyncio.Task[None]] = {}
        self._accepting = True
        self._chat_admission_enabled = chat_admission_enabled
        self._waits = WorkWaitRepository(repository)
        self._worker: asyncio.Task[None] | None = None
        self._selection_worker: asyncio.Task[None] | None = None
        self._wait_worker: asyncio.Task[None] | None = None
        self._last_error: str | None = None
        self._last_wait_error: str | None = None
        self._last_reclaim = 0.0
        self._phase_timings = {
            name: TimingSummary()
            for name in (
                "repair_inputs",
                "wake_rollups",
                "reclaim",
                "protocol_cleanup",
                "selection",
                "resume",
            )
        }

    @contextmanager
    def _timed(self, phase: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            seconds = time.perf_counter() - started
            self._phase_timings[phase].record(seconds)
            if seconds >= 1:
                logger.info("work_scheduler_phase phase=%s seconds=%.6f", phase, seconds)

    @property
    def running(self) -> bool:
        return self._worker is not None and not self._worker.done()

    async def start(self) -> None:
        # Existing accepted Work must recover even when optional chat admission is off.
        # SELF always uses durable Work, including the legacy participation proposer.
        if self._worker is None:
            self._accepting = True
            from qq_ai_bot.runtime.execution_receipts import PROCESS_ID

            await self.repository.repair_abandoned_inputs(PROCESS_ID)
            self._worker = asyncio.create_task(self._loop(), name="runtime-work-scheduler")

    async def close(self) -> None:
        self._accepting = False
        task, self._worker = self._worker, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        active = tuple(self._in_flight.values())
        for worker in active:
            worker.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        self._in_flight.clear()

    async def health(self) -> dict[str, object]:
        async with self.repository.database.sessions() as session:
            oldest = await session.scalar(
                select(func.min(inputs.c.created)).where(inputs.c.state.in_(("pending", "staged")))
            )
            active = await session.scalar(
                select(func.count())
                .select_from(work)
                .where(work.c.state.not_in(("completed", "failed", "cancelled")))
            )
            active_waits = await session.scalar(
                select(func.count()).select_from(waits).where(waits.c.status == "active")
            )
            oldest_wait = await session.scalar(
                select(func.min(waits.c.created)).where(waits.c.status == "active")
            )
        return {
            "pending_oldest_seconds": max(0, int(time.time() - oldest)) if oldest else 0,
            "active_work_count": active or 0,
            "active_wait_count": active_waits or 0,
            "active_wait_oldest_seconds": max(0, int(time.time() - oldest_wait))
            if oldest_wait
            else 0,
            "enabled": True,
            "chat_admission_enabled": self._chat_admission_enabled,
            "running": self.running,
            "active_scopes": len(self._in_flight),
            "wait_running": self._wait_worker is not None and not self._wait_worker.done(),
            "last_error_category": self._last_error,
            "wait_error_category": self._last_wait_error,
            "phase_timings": {
                name: timing.snapshot() for name, timing in self._phase_timings.items()
            },
        }

    async def _loop(self) -> None:
        # Model turns may await many requests. Their duration must not delay the
        # sole Work time driver, including waits owned by automation or children.
        selection = asyncio.create_task(self._selection_loop(), name="runtime-work-selection")
        wait_poll = asyncio.create_task(self._wait_loop(), name="runtime-work-waits")
        self._selection_worker, self._wait_worker = selection, wait_poll
        try:
            done, _ = await asyncio.wait(
                (selection, wait_poll), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                error = task.exception() if not task.cancelled() else None
                category = (
                    "CancelledError"
                    if task.cancelled()
                    else type(error).__name__
                    if error is not None
                    else "UnexpectedWorkerExit"
                )
                self._last_error = category
                if task is wait_poll:
                    self._last_wait_error = category
                logger.warning(
                    "work_scheduler_loop_stopped loop=%s category=%s", task.get_name(), category
                )
        finally:
            selection.cancel()
            wait_poll.cancel()
            joined = asyncio.gather(selection, wait_poll, return_exceptions=True)
            try:
                try:
                    await asyncio.shield(joined)
                except asyncio.CancelledError:
                    # close can arrive after an unexpected sibling exit has
                    # already cancelled selection. Join its original recovery
                    # without propagating a second cancellation into cleanup.
                    await joined
                    raise
            finally:
                self._selection_worker = self._wait_worker = None

    async def _selection_loop(self) -> None:
        while True:
            try:
                await self.dispatch_once()
            except Exception as exc:
                self._last_error = type(exc).__name__
                logger.warning("work_scheduler_failed category=%s", self._last_error)
            await asyncio.sleep(2)

    async def _wait_loop(self) -> None:
        while True:
            await self.poll_waits_once()
            await asyncio.sleep(2)

    async def poll_waits_once(self) -> None:
        try:
            await self._waits.deliver_due()
            self._last_wait_error = None
        except Exception as exc:
            self._last_wait_error = type(exc).__name__
            logger.warning("work_wait_poll_failed category=%s", self._last_wait_error)

    async def drain_once(self) -> None:
        """Dispatch one scan and wait for the scopes it started (tests/manual drains)."""
        started = await self.dispatch_once()
        if started:
            await asyncio.wait(started)

    async def dispatch_once(self) -> list[asyncio.Task[None]]:
        """Maintain, select and dispatch without waiting for any resume to finish."""
        from qq_ai_bot.runtime.execution_receipts import PROCESS_ID

        with self._timed("repair_inputs"):
            await self.repository.repair_abandoned_inputs(PROCESS_ID)
        with self._timed("wake_rollups"):
            await self.repository.wake_context_rollups()
        if time.monotonic() - self._last_reclaim > 600:
            with self._timed("reclaim"):
                await self.repository.reclaim_terminal()
            from qq_ai_bot.runtime.protocol_store import ProtocolStore

            with self._timed("protocol_cleanup"):
                await ProtocolStore(self.repository.database).cleanup()
            self._last_reclaim = time.monotonic()
        capacity = _MAX_IN_FLIGHT - len(self._in_flight)
        if not self._accepting or capacity <= 0:
            return []
        with self._timed("selection"):
            async with self.repository.database.sessions() as session:
                rows = (
                    (
                        await session.execute(
                            select(work)
                            .outerjoin(
                                scope,
                                scope.c.conversation_id == work.c.conversation_id,
                            )
                            .outerjoin(recovery, recovery.c.work_id == work.c.id)
                            .where(
                                or_(
                                    work.c.state.in_(("queued", "running")),
                                    (work.c.state == "suspended")
                                    & work.c.id.in_(
                                        select(deliveries.c.work_id).where(
                                            deliveries.c.kind == "notice",
                                            deliveries.c.state.in_(("planned", "blocked")),
                                        )
                                    ),
                                ),
                                or_(
                                    func.json_extract(work.c.source_json, "$.owner")
                                    == "plugin_invocation",
                                    func.json_extract(work.c.source_json, "$.origin").in_(
                                        ("user_message", "autonomous_group", "self_initiative")
                                    ),
                                ),
                                work.c.id.not_in(select(children.c.work_id)),
                                work.c.conversation_id.not_in(tuple(self._in_flight)),
                                or_(scope.c.owner.is_(None), scope.c.lease_until <= time.time()),
                                or_(
                                    recovery.c.work_id.is_(None),
                                    recovery.c.not_before <= time.time(),
                                ),
                            )
                            .order_by(work.c.updated)
                            .limit(_MAX_IN_FLIGHT)
                        )
                    )
                    .mappings()
                    .all()
                )
        batches: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            batches.setdefault(row["conversation_id"], []).append(dict(row))
        started: list[asyncio.Task[None]] = []
        for conversation_id, items in list(batches.items())[:capacity]:
            task = asyncio.create_task(
                self._run_scope(conversation_id, items), name=f"runtime-work:{conversation_id}"
            )
            self._in_flight[conversation_id] = task
            started.append(task)
        return started

    async def _run_scope(self, conversation_id: str, items: list[dict[str, Any]]) -> None:
        try:
            for item in items:
                try:
                    with self._timed("resume"):
                        category = await self._resume(item)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    category = type(exc).__name__
                    logger.warning("work_resume_failed category=%s", category)
                # Health reflects only this run's own outcome.
                self._last_error = category
        finally:
            self._in_flight.pop(conversation_id, None)
