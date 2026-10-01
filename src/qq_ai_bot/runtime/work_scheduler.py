"""Bounded selection and maintenance for persisted root work."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol

from sqlalchemy import func, or_, select

from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.work_recovery_schema import deliveries, recovery
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import inputs, scope, work
from qq_ai_bot.runtime.work_wait import WorkWaitRepository
from qq_ai_bot.runtime.work_wait_schema import waits

logger = logging.getLogger(__name__)


class WorkResumeService(Protocol):
    last_error: str | None

    async def resume(self, item: dict[str, Any]) -> None: ...


class WorkScheduler:
    def __init__(
        self,
        repository: WorkRepository,
        resumer: WorkResumeService,
        *,
        chat_admission_enabled: bool,
    ) -> None:
        self.repository = repository
        self.resumer = resumer
        self._chat_admission_enabled = chat_admission_enabled
        self._waits = WorkWaitRepository(repository)
        self._worker: asyncio.Task[None] | None = None
        self._selection_worker: asyncio.Task[None] | None = None
        self._wait_worker: asyncio.Task[None] | None = None
        self._last_error: str | None = None
        self._last_wait_error: str | None = None
        self._last_reclaim = 0.0

    @property
    def running(self) -> bool:
        return self._worker is not None and not self._worker.done()

    async def start(self) -> None:
        # Existing accepted Work must recover even when optional chat admission is off.
        # SELF always uses durable Work, including the legacy participation proposer.
        if self._worker is None:
            from qq_ai_bot.runtime.execution_receipts import PROCESS_ID

            await self.repository.repair_abandoned_inputs(PROCESS_ID)
            self._worker = asyncio.create_task(self._loop(), name="runtime-work-scheduler")

    async def close(self) -> None:
        task, self._worker = self._worker, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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
            "wait_running": self._wait_worker is not None and not self._wait_worker.done(),
            "last_error_category": self._last_error,
            "wait_error_category": self._last_wait_error,
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
                await self.drain_once()
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
        from qq_ai_bot.runtime.execution_receipts import PROCESS_ID

        await self.repository.repair_abandoned_inputs(PROCESS_ID)
        await self.repository.wake_context_rollups()
        if time.monotonic() - self._last_reclaim > 600:
            await self.repository.reclaim_terminal()
            from qq_ai_bot.runtime.protocol_store import ProtocolStore

            await ProtocolStore(self.repository.database).cleanup()
            self._last_reclaim = time.monotonic()
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
                            or_(scope.c.owner.is_(None), scope.c.lease_until <= time.time()),
                            or_(recovery.c.work_id.is_(None), recovery.c.not_before <= time.time()),
                        )
                        .order_by(work.c.updated)
                        .limit(8)
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            await self.resumer.resume(dict(row))
            self._last_error = self.resumer.last_error
