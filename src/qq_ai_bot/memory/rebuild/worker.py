"""Idle-by-default worker for explicitly started memory rebuild runs."""

from __future__ import annotations

import asyncio
import logging

from qq_ai_bot.memory.enums import MemoryRebuildRunStatus
from qq_ai_bot.memory.rebuild.service import MemoryRebuildService

logger = logging.getLogger(__name__)


class MemoryRebuildWorker:
    """Poll only executing states; never starts or resumes a run."""

    def __init__(self, service: MemoryRebuildService, *, interval_seconds: float) -> None:
        self.service = service
        self.interval_seconds = interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        # A process boundary is an explicit pause boundary. No automatic resume.
        await self.service.repository.pause_after_restart()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="memory-rebuild-worker")

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
            if not self._stop.is_set():
                await self.process_once()

    async def process_once(self) -> int:
        run = await self.service.repository.get_executing_run()
        if run is None:
            return 0
        try:
            if run.status is MemoryRebuildRunStatus.EXTRACTING:
                return await self.service.process_extraction_once(run)
            if run.status is MemoryRebuildRunStatus.COMMITTING:
                return await self.service.process_commit_once(run)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning(
                "memory_rebuild_batch_failed run_id=%s error_category=%s",
                run.public_id,
                type(exc).__name__,
            )
        return 0
