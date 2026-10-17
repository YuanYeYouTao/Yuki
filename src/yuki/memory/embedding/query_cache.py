"""Bounded in-process cache for privacy-preserving query embeddings."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from yuki.memory.embedding.models import EmbeddingBatchResult


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    value: EmbeddingBatchResult
    expires_at: float


class QueryEmbeddingCache:
    """Cache exact normalized queries without retaining their plaintext."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("query embedding cache TTL must be positive")
        if max_entries <= 0:
            raise ValueError("query embedding cache size must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[EmbeddingBatchResult]] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        *,
        profile_fingerprint: str,
        query_text: str,
        factory: Callable[[], Awaitable[EmbeddingBatchResult]],
    ) -> tuple[EmbeddingBatchResult, bool]:
        """Return ``(result, cache_hit)`` and coalesce concurrent identical requests."""

        key = self._key(profile_fingerprint, query_text)
        owner = False
        async with self._lock:
            now = self._clock()
            self._purge_expired(now)
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                return entry.value, True
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._produce_and_store(key=key, factory=factory),
                    name="memory-query-embedding",
                )
                self._inflight[key] = task
                owner = True
        value = await asyncio.shield(task)
        return value, not owner

    async def _produce_and_store(
        self,
        *,
        key: str,
        factory: Callable[[], Awaitable[EmbeddingBatchResult]],
    ) -> EmbeddingBatchResult:
        try:
            value = await factory()
            async with self._lock:
                self._entries[key] = _CacheEntry(
                    value=value,
                    expires_at=self._clock() + self._ttl_seconds,
                )
                self._entries.move_to_end(key)
                while len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)
            return value
        finally:
            async with self._lock:
                current = asyncio.current_task()
                if self._inflight.get(key) is current:
                    self._inflight.pop(key, None)

    @staticmethod
    def _key(profile_fingerprint: str, query_text: str) -> str:
        payload = f"{profile_fingerprint}\0{query_text}".encode("utf-8", errors="replace")
        return hashlib.sha256(payload).hexdigest()

    def _purge_expired(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)
