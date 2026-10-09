"""Bounded child selection; worker execution belongs to the application service."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import or_, select

from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.work_recovery_schema import recovery
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import work

logger = logging.getLogger(__name__)


class ChildExecutor(Protocol):
    definitions: tuple[ChatTool, ...] | None

    async def prepare(self, *, admission_enabled: bool) -> None: ...
    async def cancel_commands(self) -> None: ...
    async def run(self, identity: str) -> str | None: ...


class SubagentScheduler:
    def __init__(
        self,
        repository: WorkRepository,
        children: SubagentRepository,
        executor: ChildExecutor,
        *,
        admission_enabled: bool,
        max_concurrency: int = 1,
    ) -> None:
        self.repository = repository
        self.children = children
        self.executor = executor
        self.admission_enabled = admission_enabled
        self.max_concurrency = max_concurrency
        if self.max_concurrency < 1:
            raise ValueError("invalid_subagent_concurrency")
        self.running: dict[str, asyncio.Task[None]] = {}
        self.task: asyncio.Task[None] | None = None
        self.last_error: str | None = None

    async def health(self) -> dict[str, Any]:
        return {
            "running": self.task is not None and not self.task.done(),
            "admission_enabled": self.admission_enabled,
            "last_error_category": self.last_error,
            "active_workers": len(self.running),
            "max_concurrency": self.max_concurrency,
            "tool_count": len(self.executor.definitions or ()),
        }

    async def start(self) -> None:
        if self.task is None:
            await self.executor.prepare(admission_enabled=self.admission_enabled)
            self.task = asyncio.create_task(self.loop(), name="subagent-scheduler")

    async def close(self) -> None:
        task, self.task = self.task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        active = tuple(self.running.values())
        for worker in active:
            worker.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        self.running.clear()

    async def _run(self, identity: str) -> None:
        try:
            category = await self.executor.run(identity)
            self.last_error = category if isinstance(category, str) else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = type(exc).__name__
            logger.warning("subagent_run_failed category=%s", self.last_error)
        finally:
            self.running.pop(identity, None)

    async def loop(self) -> None:
        while True:
            try:
                await self.children.maintain()
                await self.executor.cancel_commands()
                capacity = self.max_concurrency - len(self.running)
                if capacity <= 0:
                    await asyncio.sleep(1)
                    continue
                async with self.repository.database.sessions() as session:
                    ids = list(
                        await session.scalars(
                            select(children.c.work_id)
                            .join(work, work.c.id == children.c.work_id)
                            .outerjoin(recovery, recovery.c.work_id == work.c.id)
                            .where(
                                or_(
                                    recovery.c.work_id.is_(None),
                                    recovery.c.not_before <= datetime.now(UTC).timestamp(),
                                ),
                                work.c.state.in_(("queued", "running")),
                                children.c.archived_at.is_(None),
                                children.c.work_id.not_in(tuple(self.running)),
                            )
                            .order_by(work.c.updated)
                            .limit(capacity)
                        )
                    )
                for identity in ids:
                    self.running[identity] = asyncio.create_task(
                        self._run(identity), name=f"subagent:{identity}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                logger.warning("subagent_scheduler_failed category=%s", self.last_error)
            await asyncio.sleep(1)
