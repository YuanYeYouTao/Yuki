"""One bounded disposable consumer, including source validation and encoding."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import Context
from dataclasses import dataclass
from typing import Any

from qq_ai_bot.persistence.sqlite_diagnostics import TimingSummary

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Write:
    kind: str
    size: int
    commit: Callable[[], Awaitable[Any]]
    enqueued_at: float


class DiagnosticWriter:
    """Never carries a session, infers a turn, retries a commit, or owns effects."""

    def __init__(self, *, max_records: int = 256, max_bytes: int = 32 * 1024 * 1024) -> None:
        if max_records < 1 or max_bytes < 1:
            raise ValueError("invalid_diagnostic_capacity")
        self._queue: asyncio.Queue[_Write] = asyncio.Queue(max_records)
        self._max_bytes = max_bytes
        self._bytes = 0
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self.dropped = 0
        self.failures = 0
        self.committed = 0
        self._queue_wait = TimingSummary()
        self._commit_call = TimingSummary()
        self._phases = {
            name: TimingSummary()
            for name in (
                "snapshot_call",
                "source_validation",
                "encode_call_inclusive",
                "encode_execution",
                "diagnostic_write",
            )
        }

    def record_phase(self, phase: str, seconds: float) -> None:
        self._phases[phase].record(seconds)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("diagnostic_writer_already_started")
        self._closing = False
        self._task = asyncio.create_task(
            self._consume(), name="diagnostic-writer", context=Context()
        )

    def capacity(self) -> int:
        """Synchronous admission check; callers must not await before submit.

        This prevents copying a payload when the queue is already full. Bytes
        include the active item until its actual worker/commit has finished.
        """
        if self._task is None or self._task.done() or self._closing or self._queue.full():
            return 0
        return self._max_bytes - self._bytes

    def drop(self, kind: str) -> None:
        self.dropped += 1
        logger.warning(
            "diagnostic_dropped kind=%s coverage_incomplete=true total=%d "
            "payload_bytes=unknown payload_sha256=unknown",
            kind,
            self.dropped,
        )

    def submit(self, kind: str, size: int, commit: Callable[[], Awaitable[Any]]) -> bool:
        if size < 0:
            raise ValueError("invalid_diagnostic_size")
        if (
            self._task is None
            or self._task.done()
            or self._closing
            or self._queue.full()
            or self._bytes + size > self._max_bytes
        ):
            self.drop(kind)
            return False
        self._bytes += size
        self._queue.put_nowait(_Write(kind, size, commit, time.monotonic()))
        return True

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            started = time.monotonic()
            self._queue_wait.record(started - item.enqueued_at)
            try:
                await item.commit()
                self.committed += 1
            except asyncio.CancelledError:
                self.dropped += 1
                raise
            except Exception as exc:
                self.failures += 1
                logger.error(
                    "diagnostic_commit_failed kind=%s category=%s coverage_incomplete=true",
                    item.kind,
                    type(exc).__name__,
                )
                # Only the fixed category is retained. Worker exception cycles
                # must not keep a completed payload alive until a later GC pass.
                exc.__traceback__ = None
                exc.__context__ = None
                exc.__cause__ = None
            finally:
                self._commit_call.record(time.monotonic() - started)
                self._bytes -= item.size
                self._queue.task_done()
                # Awaiting the next get must not retain the previous payload.
                del item

    async def drain(self) -> None:
        await self._queue.join()

    async def close(self, *, drain_seconds: float = 2) -> None:
        self._closing = True
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self.drain(), timeout=drain_seconds)
        except TimeoutError:
            logger.warning(
                "diagnostic_shutdown_incomplete pending=%d bytes=%d",
                self._queue.qsize(),
                self._bytes,
            )
        finally:
            self._task.cancel()
            joined = asyncio.gather(self._task, return_exceptions=True)
            try:
                await asyncio.shield(joined)
            except asyncio.CancelledError:
                while not joined.done():
                    try:
                        await asyncio.shield(joined)
                    except asyncio.CancelledError:
                        continue
                raise
            finally:
                self._task = None
                while not self._queue.empty():
                    item = self._queue.get_nowait()
                    self._bytes -= item.size
                    self.dropped += 1
                    self._queue.task_done()

    async def health(self) -> dict[str, Any]:
        return dict(
            pending=self._queue.qsize(),
            bytes=self._bytes,
            dropped=self.dropped,
            failures=self.failures,
            committed=self.committed,
            queue_wait=self._queue_wait.snapshot(),
            commit_call=self._commit_call.snapshot(),
            phase_timings={name: timing.snapshot() for name, timing in self._phases.items()},
        )
