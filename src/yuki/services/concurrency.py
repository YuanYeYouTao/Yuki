"""Single-process conversation locking and cancellable request ownership."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Collection, Coroutine
from contextlib import asynccontextmanager
from typing import Any, TypeVar
from weakref import WeakValueDictionary

T = TypeVar("T")


class RequestCancelledError(RuntimeError):
    """An in-flight LLM request was cancelled by `/ai stop`."""

    def __init__(self, message: str, *, diagnostics: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class ConcurrencyManager:
    """Serialize conversations; the model executor owns provider admission."""

    def __init__(self) -> None:
        # Holders and waiters keep strong references; idle conversations need none.
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._locks_guard = asyncio.Lock()
        self._active: dict[asyncio.Task[Any], tuple[str, asyncio.Task[Any]]] = {}
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
    ) -> T:
        """Track cancellation through executor admission and provider dispatch."""

        owner = asyncio.current_task()
        assert owner is not None
        task: asyncio.Task[T] = asyncio.create_task(operation())
        async with self._active_guard:
            self._active[owner] = (conversation_key, task)
        try:
            return await task
        except asyncio.CancelledError as exc:
            if not translate_cancellation or owner.cancelling():
                raise
            raise RequestCancelledError(
                "request cancelled", diagnostics=getattr(exc, "diagnostics", None)
            ) from exc
        finally:
            async with self._active_guard:
                self._active.pop(owner)

    async def cancel(self, conversation_key: str) -> bool:
        """Cancel only the active provider task for one conversation."""

        async with self._active_guard:
            task = self.request_task(conversation_key)
            if task is None or task.done():
                return False
            task.cancel()
            return True

    def request_task(self, conversation_key: str) -> asyncio.Task[Any] | None:
        """Read the most recently registered provider for the ordinary scope stop."""
        return next(
            (task for key, task in reversed(self._active.values()) if key == conversation_key),
            None,
        )

    def request_tasks(self, owners: Collection[asyncio.Task[Any]]) -> tuple[asyncio.Task[Any], ...]:
        """Capture providers by their original activation, including an obsolete lease."""
        return tuple(task for owner, (_key, task) in self._active.items() if owner in owners)

    def is_processing(self, conversation_key: str) -> bool:
        """Return whether this conversation currently has an active provider call."""

        task = self.request_task(conversation_key)
        return task is not None and not task.done()
