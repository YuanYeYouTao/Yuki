"""Application-owned execution tasks, without actor or conversation state."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class ActivationTasks:
    """Stop admission and join existing execution cleanup before resources close."""

    def __init__(self) -> None:
        self._accepting = True
        self._owners: dict[asyncio.Task[Any], int] = {}
        self._closing_owner: asyncio.Task[Any] | None = None

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def active_count(self) -> int:
        return len(self._owners)

    def stop_admission(self, closing_owner: asyncio.Task[Any] | None = None) -> None:
        self._accepting = False
        if closing_owner is not None:
            self._closing_owner = closing_owner

    @contextmanager
    def track(self) -> Iterator[None]:
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("runtime_execution_task_required")
        if not self._accepting:
            if owner in self._owners and owner is not self._closing_owner:
                # An admitted turn can return from I/O before worker shutdown
                # reaches drain. Preserve its retryable shutdown recovery path.
                raise asyncio.CancelledError("yuki_runtime_shutdown")
            raise RuntimeError("yuki_runtime_closing")
        self._owners[owner] = self._owners.get(owner, 0) + 1
        try:
            yield
        finally:
            depth = self._owners[owner] - 1
            if depth:
                self._owners[owner] = depth
            else:
                self._owners.pop(owner)

    async def drain(self) -> None:
        self.stop_admission()
        current = asyncio.current_task()
        pending = [task for task in self._owners if task is not current and not task.done()]
        for task in pending:
            task.cancel("yuki_runtime_shutdown")
        if pending:
            joined = asyncio.gather(*pending, return_exceptions=True)
            try:
                await asyncio.shield(joined)
            except asyncio.CancelledError:
                # Preserve shutdown cancellation after original recovery/release.
                # Cancelling the join would interrupt that cleanup a second time.
                await joined
                raise
