"""Keep the actual aiosqlite worker exit available after pool invalidation."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from typing import Any

import aiosqlite


class SQLiteConnection(aiosqlite.Connection):
    """One close request, one completion, including SQLAlchemy force termination.

    aiosqlite 0.22.1 stop() queues physical close and returns its future.
    SQLAlchemy's force-terminate path discards that future. Retaining it here
    lets the writer wait for the same operation without guessing from pool events.
    """

    def __init__(self, connector: Callable[[], sqlite3.Connection]) -> None:
        super().__init__(connector, iter_chunk_size=64)
        self.physical_exit: asyncio.Future[Any] | None = None

    def stop(self) -> asyncio.Future[Any] | None:
        if self.physical_exit is None:
            self.physical_exit = super().stop()
        return self.physical_exit

    async def close(self) -> None:
        # stop's close-and-stop function clears _connection only after close
        # succeeds. Cancellation must not cancel the physical completion future.
        completion = self.stop()
        if completion is not None:
            await asyncio.shield(completion)
