"""Independent restart-safe document embedding worker."""

from __future__ import annotations

import asyncio
import logging
import time

from qq_ai_bot.memory.embedding.codec import Float32VectorCodec
from qq_ai_bot.memory.embedding.jobs import EmbeddingWrite, MemoryEmbeddingJobRepository
from qq_ai_bot.memory.embedding.metrics import MemoryEmbeddingMetrics
from qq_ai_bot.memory.embedding.provider import EmbeddingProvider, EmbeddingProviderError

logger = logging.getLogger(__name__)


class MemoryEmbeddingWorker:
    def __init__(
        self,
        *,
        provider: EmbeddingProvider,
        jobs: MemoryEmbeddingJobRepository,
        interval_seconds: float,
        claim_limit: int,
        retry_initial_seconds: float,
        codec: Float32VectorCodec | None = None,
        metrics: MemoryEmbeddingMetrics | None = None,
    ) -> None:
        self._provider = provider
        self._jobs = jobs
        self._interval_seconds = interval_seconds
        self._claim_limit = claim_limit
        self._retry_initial_seconds = retry_initial_seconds
        self._codec = codec or Float32VectorCodec()
        self.metrics = metrics or MemoryEmbeddingMetrics()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        async with self._start_lock:
            if self._task is None:
                await self._jobs.recover_interrupted()
                await self._jobs.reconcile()
                self._task = asyncio.create_task(self._run(), name="memory-embedding-worker")

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task

    async def schedule(self, fact_id: int) -> None:
        if await self._jobs.enqueue_fact(fact_id):
            self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                pass
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                logger.error(
                    "memory_embedding_worker_iteration_failed error_category=%s",
                    type(exc).__name__,
                )

    async def process_once(self) -> int:
        jobs = await self._jobs.claim(limit=self._claim_limit)
        if not jobs:
            return 0
        facts = await self._jobs.load_active_facts(jobs)
        valid_jobs = []
        texts = []
        for job in jobs:
            fact = facts.get(job.fact_id)
            if fact is None:
                await self._jobs.skip(job)
                continue
            current_hash = self._jobs.documents.content_hash_fields(
                kind=fact.kind,
                category=fact.category,
                memory_key=fact.memory_key,
                content=fact.content,
            )
            if current_hash != job.content_hash:
                await self._jobs.enqueue_fact(job.fact_id, force=True)
                continue
            valid_jobs.append(job)
            texts.append(
                self._jobs.documents.build_fields(
                    kind=fact.kind,
                    category=fact.category,
                    memory_key=fact.memory_key,
                    content=fact.content,
                )
            )
        if not valid_jobs:
            return 0
        started = time.perf_counter()
        try:
            result = await self._provider.embed_documents(tuple(texts))
            if len(result.vectors) != len(valid_jobs):
                raise EmbeddingProviderError(
                    "embedding_invalid_response",
                    "Embedding provider returned an invalid response.",
                    retryable=False,
                )
            writes = tuple(
                EmbeddingWrite(
                    job_id=job.id,
                    fact_id=job.fact_id,
                    content_hash=job.content_hash,
                    vector_blob=self._codec.encode(vector),
                    claimed_at=job.updated_at,
                )
                for job, vector in zip(valid_jobs, result.vectors, strict=True)
            )
            completed = await self._jobs.complete(writes)
            self.metrics.record_documents(
                input_count=result.usage.input_count,
                input_tokens=result.usage.input_tokens,
                latency=time.perf_counter() - started,
            )
            return completed
        except asyncio.CancelledError:
            raise
        except EmbeddingProviderError as exc:
            for job in valid_jobs:
                await self._jobs.fail(
                    job,
                    error_category=exc.code,
                    retryable=exc.retryable,
                    initial_delay_seconds=self._retry_initial_seconds,
                )
            return 0
        except ValueError:
            for job in valid_jobs:
                await self._jobs.fail(
                    job,
                    error_category="embedding_invalid_response",
                    retryable=False,
                    initial_delay_seconds=self._retry_initial_seconds,
                )
            return 0
