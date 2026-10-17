"""Bound native process admission across all Runner instances on one runtime loop."""

from __future__ import annotations

import asyncio
from weakref import WeakKeyDictionary


class WorkerCapacity:
    def __init__(self, maximum: int, foreground_reserved: int) -> None:
        if maximum < 1 or not 0 <= foreground_reserved < maximum:
            raise ValueError("code_worker_capacity_invalid")
        self.maximum = maximum
        self.foreground_reserved = foreground_reserved
        self.active = 0
        self.background = 0
        self.peak = 0
        self._condition = asyncio.Condition()

    async def acquire(self, *, background: bool, wait_seconds: float) -> None:
        async with asyncio.timeout(wait_seconds), self._condition:
            await self._condition.wait_for(
                lambda: (
                    self.active < self.maximum
                    and (
                        not background or self.background < self.maximum - self.foreground_reserved
                    )
                )
            )
            self.active += 1
            self.background += int(background)
            self.peak = max(self.peak, self.active)

    async def release(self, *, background: bool) -> None:
        async with self._condition:
            self.active -= 1
            self.background -= int(background)
            self._condition.notify_all()


# Yuki has one application event loop. Independent plugin Runner instances
# share its process capacity; tests with separate loops get separate resources.
_CAPACITY: WeakKeyDictionary[asyncio.AbstractEventLoop, WorkerCapacity] = WeakKeyDictionary()


def runtime_capacity(maximum: int, foreground_reserved: int) -> WorkerCapacity:
    loop = asyncio.get_running_loop()
    capacity = _CAPACITY.get(loop)
    if capacity is None:
        capacity = WorkerCapacity(maximum, foreground_reserved)
        _CAPACITY[loop] = capacity
    elif (capacity.maximum, capacity.foreground_reserved) != (maximum, foreground_reserved):
        raise ValueError("code_worker_capacity_requires_runtime_restart")
    return capacity
