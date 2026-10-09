"""Local-only Memory expiration, evidence cleanup and consistency maintenance."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.config import Settings
from qq_ai_bot.memory.enums import MemoryInvalidationReason
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import MemoryFact
from qq_ai_bot.memory.service import MemoryFactService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _MaintenanceRuntime:
    enabled: bool
    interval_seconds: float
    batch_limit: int


class MemoryMaintenanceWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        facts: MemoryFactService,
        runtime_config: RuntimeConfigService | None = None,
        metrics: MemoryLifecycleMetrics | None = None,
    ) -> None:
        self._settings = settings
        self._facts = facts
        self._runtime_config = runtime_config
        self.metrics = metrics or MemoryLifecycleMetrics()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._process_lock = asyncio.Lock()
        # Reconstructible scheduling position, not a completeness or business watermark.

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="memory-maintenance-worker")

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            runtime = await self._snapshot()
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=runtime.interval_seconds,
                )
            except TimeoutError:
                pass
            self._wake.clear()
            if not self._stop.is_set() and runtime.enabled:
                try:
                    await self.process_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "memory_maintenance_failed error_category=%s", type(exc).__name__
                    )

    async def process_once(self) -> int:
        async with self._process_lock:
            return await self._process_once_unlocked()

    async def _process_once_unlocked(self) -> int:
        runtime = await self._snapshot()
        if not runtime.enabled:
            return 0
        now = datetime.now(UTC)
        rows = await self._facts.repository.list_expired_candidates(
            now=now, limit=runtime.batch_limit
        )

        async def invalidate(owned: AsyncSession) -> int:
            await self._facts.prepare_evidence_write(
                tuple(candidate.id for candidate in rows), session=owned, targets=rows
            )
            return await self._invalidate_candidates(rows, now=now, session=owned)

        changed = await self._facts.repository.apply_evidence_write(invalidate)
        self.metrics.increment("maintenance_expired", changed)
        self.metrics.record_maintenance_success(now)
        return changed

    async def _invalidate_candidates(
        self,
        rows: tuple[MemoryFact, ...],
        *,
        now: datetime,
        session: AsyncSession,
    ) -> int:
        changed = 0
        for candidate in rows:
            fact = await self._facts.repository.get_fact(candidate.id, session=session)
            if fact is None:
                continue
            if fact.valid_until is None or fact.valid_until > now:
                continue
            if await self._facts.invalidate_fact(
                fact.id,
                reason=MemoryInvalidationReason.EXPIRED,
                actor_user_id=None,
                session=session,
            ):
                changed += 1
        return changed

    async def _snapshot(self) -> _MaintenanceRuntime:
        if self._runtime_config is not None:
            runtime = (await self._runtime_config.snapshot()).memory
            return _MaintenanceRuntime(
                enabled=runtime.maintenance_enabled,
                interval_seconds=runtime.maintenance_interval_seconds,
                batch_limit=runtime.maintenance_batch_limit,
            )
        return _MaintenanceRuntime(
            enabled=self._settings.memory_maintenance_enabled,
            interval_seconds=self._settings.memory_maintenance_interval_seconds,
            batch_limit=self._settings.memory_maintenance_batch_limit,
        )
