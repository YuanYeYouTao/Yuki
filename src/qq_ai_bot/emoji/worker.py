"""Restart-safe background worker for emoji analysis and preview repair."""

from __future__ import annotations

import asyncio
import logging
import uuid

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.emoji.classifier import EmojiClassifier
from qq_ai_bot.emoji.lifecycle import EmojiLifecycleService
from qq_ai_bot.emoji.repository import EmojiJob, EmojiRepository
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.persistence.worker_recovery import recover_database_loop

logger = logging.getLogger(__name__)


class EmojiWorker:
    def __init__(
        self,
        *,
        repository: EmojiRepository,
        classifier: EmojiClassifier,
        lifecycle: EmojiLifecycleService,
        storage: EmojiStorage,
        runtime_config: RuntimeConfigService,
    ) -> None:
        self._repository = repository
        self._classifier = classifier
        self._lifecycle = lifecycle
        self._storage = storage
        self._runtime_config = runtime_config
        self._worker_id = f"emoji-{uuid.uuid4()}"
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="emoji-worker")

    def wake(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        await recover_database_loop(self._run_queue, stop=self._stop, logger=logger)

    async def _run_queue(self) -> None:
        while not self._stop.is_set():
            snapshot = await self._runtime_config.snapshot()
            runtime = snapshot.emoji
            if runtime.enabled:
                jobs = await self._repository.claim_jobs(
                    worker_id=self._worker_id,
                    limit=runtime.worker_batch_size,
                    lease_seconds=runtime.worker_lease_seconds,
                )
                for job in jobs:
                    await self._process(job, snapshot)
                if jobs:
                    continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=runtime.worker_poll_seconds)
            except TimeoutError:
                pass

    async def _process(self, job: EmojiJob, runtime: RuntimeConfigSnapshot) -> None:
        emoji_runtime = runtime.emoji
        try:
            asset = await self._repository.get(job.emoji_id)
            if asset is None:
                await self._repository.complete_job(job.id)
                return
            if job.job_type == "rebuild_preview":
                if not asset.preview_relative_path:
                    raise RuntimeError("emoji preview path is missing")
                self._storage.restore_preview(asset.relative_path, asset.preview_relative_path)
            else:
                # RuntimeConfigSnapshot.emoji is deliberately passed as one immutable policy.
                analysis = await self._classifier.classify(
                    asset,
                    analysis_version=emoji_runtime.analysis_version,
                    max_frames=runtime.vision.gif_max_frames,
                    thinking_enabled=runtime.vision.thinking_enabled,
                    thinking_budget=runtime.vision.thinking_budget,
                )
                await self._lifecycle.apply_analysis(
                    asset,
                    analysis,
                    runtime=emoji_runtime,
                )
            await self._repository.complete_job(job.id)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "emoji_job_failed job_type=%s error_category=%s",
                job.job_type,
                type(exc).__name__,
            )
            await self._repository.fail_job(
                job.id,
                error_category=getattr(exc, "code", type(exc).__name__),
                max_attempts=emoji_runtime.worker_max_attempts,
                retry_delay_seconds=emoji_runtime.worker_retry_delay_seconds,
            )
