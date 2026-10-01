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
    last_error: str | None
    definitions: tuple[ChatTool, ...] | None

    async def prepare(self, *, admission_enabled: bool) -> None: ...
    async def cancel_commands(self) -> None: ...
    async def run(self, identity: str) -> None: ...


class SubagentScheduler:
    def __init__(
        self,
        repository: WorkRepository,
        children: SubagentRepository,
        executor: ChildExecutor,
        *,
        admission_enabled: bool,
        global_llm_concurrency: int,
    ) -> None:
        self.repository = repository
        self.children = children
        self.executor = executor
        self.admission_enabled = admission_enabled
        self.global_llm_concurrency = global_llm_concurrency
        self.task: asyncio.Task[None] | None = None
        self.last_error: str | None = None

    async def health(self) -> dict[str, Any]:
        return {
            "running": self.task is not None and not self.task.done(),
            "admission_enabled": self.admission_enabled,
            "last_error_category": self.last_error,
            "tool_count": len(self.executor.definitions or ()),
        }

    async def start(self) -> None:
        if self.admission_enabled and self.global_llm_concurrency < 2:
            raise ValueError("subagents_require_foreground_model_slot")
        if self.task is None:
            await self.executor.prepare(admission_enabled=self.admission_enabled)
            self.task = asyncio.create_task(self.loop(), name="subagent-scheduler")

    async def close(self) -> None:
        task, self.task = self.task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def loop(self) -> None:
        while True:
            try:
                await self.children.maintain()
                await self.executor.cancel_commands()
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
                            )
                            .order_by(work.c.updated)
                            .limit(8)
                        )
                    )
                for identity in ids:
                    await self.executor.run(identity)
                    self.last_error = self.executor.last_error
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                logger.warning("subagent_scheduler_failed category=%s", self.last_error)
            await asyncio.sleep(1)
