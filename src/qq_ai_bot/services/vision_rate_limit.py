"""Independent in-memory rate limiting for billable vision requests."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable


class VisionRateLimiter:
    """Bound billable vision calls without consuming the text-LLM limiter."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._user_windows: dict[str, deque[float]] = {}
        self._group_windows: dict[str, deque[float]] = {}
        self._next_sweep = self._clock() + 60.0
        self._lock = asyncio.Lock()

    async def allow(
        self,
        *,
        user_id: str,
        group_id: str | None,
        per_user_per_minute: int,
        per_group_per_minute: int,
    ) -> bool:
        """Reserve one provider request when both exact scopes have capacity."""

        async with self._lock:
            now = self._clock()
            cutoff = now - 60.0
            if now >= self._next_sweep:
                for windows in (self._user_windows, self._group_windows):
                    for key, window in tuple(windows.items()):
                        self._prune(window, cutoff)
                        if not window:
                            del windows[key]
                self._next_sweep = now + 60.0
            user_window = self._user_windows.get(user_id, deque())
            self._prune(user_window, cutoff)
            group_window = (
                self._group_windows.get(group_id, deque()) if group_id is not None else None
            )
            if group_window is not None:
                self._prune(group_window, cutoff)
            if len(user_window) >= per_user_per_minute:
                return False
            if group_window is not None and len(group_window) >= per_group_per_minute:
                return False
            user_window.append(now)
            self._user_windows[user_id] = user_window
            if group_id is not None and group_window is not None:
                group_window.append(now)
                self._group_windows[group_id] = group_window
            return True

    @staticmethod
    def _prune(window: deque[float], cutoff: float) -> None:
        while window and window[0] <= cutoff:
            window.popleft()
