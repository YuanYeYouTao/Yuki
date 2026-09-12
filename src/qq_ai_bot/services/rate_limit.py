"""Single-process sliding-window rate limiting."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Rate-limit decision for a user and optional group."""

    allowed: bool
    scope: str | None = None


class SlidingWindowRateLimiter:
    """Keep separate command/chat buckets for users and groups."""

    def __init__(
        self,
        *,
        per_user: int,
        per_group: int,
        window_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_user = per_user
        self._per_group = per_group
        self._window_seconds = window_seconds
        self._clock = clock
        self._buckets: dict[tuple[str, str, str], deque[float]] = {}
        self._next_sweep = self._clock() + window_seconds
        self._lock = asyncio.Lock()

    async def check(
        self,
        *,
        user_id: str,
        group_id: str | None,
        category: str,
    ) -> RateLimitResult:
        """Consume capacity atomically if both applicable scopes allow it."""

        async with self._lock:
            now = self._clock()
            if now >= self._next_sweep:
                for key, bucket in tuple(self._buckets.items()):
                    self._prune(bucket, now)
                    if not bucket:
                        del self._buckets[key]
                self._next_sweep = now + self._window_seconds
            user_key = (category, "user", user_id)
            user_bucket = self._buckets.get(user_key, deque())
            self._prune(user_bucket, now)
            if len(user_bucket) >= self._per_user:
                return RateLimitResult(False, "user")
            group_key: tuple[str, str, str] | None = None
            group_bucket: deque[float] | None = None
            if group_id is not None:
                group_key = (category, "group", group_id)
                group_bucket = self._buckets.get(group_key, deque())
                self._prune(group_bucket, now)
                if len(group_bucket) >= self._per_group:
                    return RateLimitResult(False, "group")
            user_bucket.append(now)
            self._buckets[user_key] = user_bucket
            if group_key is not None and group_bucket is not None:
                group_bucket.append(now)
                self._buckets[group_key] = group_bucket
            return RateLimitResult(True)

    def _prune(self, bucket: deque[float], now: float) -> None:
        cutoff = now - self._window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
