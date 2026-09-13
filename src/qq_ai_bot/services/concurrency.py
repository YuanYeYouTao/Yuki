"""Single-process per-conversation locking and global LLM concurrency."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, TypeVar
from weakref import WeakValueDictionary

T = TypeVar("T")


class RequestCancelledError(RuntimeError):
    """An in-flight LLM request was cancelled by `/ai stop`."""


class ConcurrencyManager:
    """Serialize conversations while allowing bounded cross-conversation work."""

    def __init__(self, global_limit: int) -> None:
        self._semaphore = asyncio.Semaphore(global_limit)
        self._background_semaphore = asyncio.Semaphore(max(1, global_limit - 1))
        # Holders and waiters keep strong references; idle conversations need none.
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._locks_guard = asyncio.Lock()
        self._active: dict[str, asyncio.Task[Any]] = {}
        self._active_guard = asyncio.Lock()

    async def _lock_for(self, conversation_key: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(conversation_key, asyncio.Lock())

    @asynccontextmanager
    async def conversation(self, conversation_key: str) -> AsyncIterator[None]:
        """Acquire the lock dedicated to one conversation."""

        lock = await self._lock_for(conversation_key)
        async with lock:
            yield

    async def run_llm(
        self,
        conversation_key: str,
        operation: Callable[[], Coroutine[Any, Any, T]],
        *,
        translate_cancellation: bool = True,
        background: bool = False,
    ) -> T:
        """Run one cancellable provider call under the global semaphore."""

        async with AsyncExitStack() as stack:
            if background:
                await stack.enter_async_context(self._background_semaphore)
            await stack.enter_async_context(self._semaphore)
            task: asyncio.Task[T] = asyncio.create_task(operation())
            async with self._active_guard:
                self._active[conversation_key] = task
            try:
                return await task
            except asyncio.CancelledError as exc:
                if not translate_cancellation:
                    raise
                raise RequestCancelledError("request cancelled") from exc
            finally:
                async with self._active_guard:
                    if self._active.get(conversation_key) is task:
                        self._active.pop(conversation_key, None)

    async def cancel(self, conversation_key: str) -> bool:
        """Cancel only the active provider task for one conversation."""

        async with self._active_guard:
            task = self._active.get(conversation_key)
            if task is None or task.done():
                return False
            task.cancel()
            return True

    def is_processing(self, conversation_key: str) -> bool:
        """Return whether this conversation currently has an active provider call."""

        task = self._active.get(conversation_key)
        return task is not None and not task.done()
