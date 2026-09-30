"""Run the participation library's synchronous store on one owning thread."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

from yuki_participation.controller import State
from yuki_participation.models import Scope
from yuki_participation.store import SnapshotStore

_T = TypeVar("_T")


class AsyncSnapshotStore:
    """Connection creation, reads, writes and close share the same worker thread.

    A queued operation remains real work when its caller is cancelled. Shutdown
    queues connection close after those operations; it never closes a busy connection.
    """

    def __init__(self) -> None:
        self._executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="participation-store"
        )
        self._store: SnapshotStore | None = None

    @classmethod
    async def open(cls, path: Path) -> AsyncSnapshotStore:
        instance = cls()

        def create() -> None:
            instance._store = SnapshotStore(path)

        try:
            await instance._run(create)
        except BaseException:
            await instance.close()
            raise
        return instance

    async def _run(self, operation: Callable[[], _T]) -> _T:
        if self._executor is None:
            raise RuntimeError("participation_store_closed")
        return await asyncio.shield(
            asyncio.get_running_loop().run_in_executor(self._executor, operation)
        )

    async def load(self, scope: Scope) -> tuple[int, str] | None:
        conversation_id, generation = scope.conversation_id, scope.generation

        def read() -> tuple[int, str] | None:
            if self._store is None:
                raise RuntimeError("participation_store_closed")
            loaded = self._store.load(Scope(conversation_id=conversation_id, generation=generation))
            return (loaded[0], loaded[1].model_dump_json()) if loaded is not None else None

        return await self._run(read)

    async def save(self, payload: str, *, expected_revision: int) -> int:
        def write() -> int:
            if self._store is None:
                raise RuntimeError("participation_store_closed")
            return self._store.save(
                State.model_validate_json(payload), expected_revision=expected_revision
            )

        return await self._run(write)

    async def close(self) -> None:
        if self._executor is None:
            return
        executor = self._executor

        def finish() -> None:
            if self._store is not None:
                self._store.close()
                self._store = None

        future = asyncio.get_running_loop().run_in_executor(executor, finish)
        cancelled = False
        try:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    cancelled = True
            future.result()
        finally:
            executor.shutdown(wait=False)
            self._executor = None
        if cancelled:
            raise asyncio.CancelledError
