"""Route child receipts to durable work; never start an independent Agent turn."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from qq_ai_bot.sandbox.continuations import SandboxContinuationRepository

if TYPE_CHECKING:
    from qq_ai_bot.container import ApplicationContainer

logger = logging.getLogger(__name__)


class SandboxContinuationWorker:
    def __init__(self, app: ApplicationContainer) -> None:
        self.app = app
        self.repository = SandboxContinuationRepository(app.database)
        self._worker: asyncio.Task[None] | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            await self.repository.retire_legacy()
            self._worker = asyncio.create_task(self._loop(), name="sandbox-continuations")

    async def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    async def health(self) -> dict[str, object]:
        return {
            "running": self._worker is not None and not self._worker.done(),
            "last_error_category": self._last_error,
        }

    async def _loop(self) -> None:
        while True:
            try:
                await self.drain_once()
                self._last_error = None
            except Exception as exc:
                category = type(exc).__name__
                if self._last_error != category:
                    logger.warning("sandbox_continuation_failed category=%s", category)
                self._last_error = category
            await asyncio.sleep(2)

    async def drain_once(self) -> None:
        for request_id in await self.repository.ready():
            try:
                await self._drain_request(request_id)
            except Exception as exc:
                self._last_error = type(exc).__name__
                logger.warning("sandbox_task_resume_failed category=%s", self._last_error)
            finally:
                await self.repository.rotate(request_id)

    async def _drain_request(self, request_id: str) -> None:
        from qq_ai_bot.runtime.work_repository import WorkRepository

        await WorkRepository(self.app.database).route_child_completion(request_id)
