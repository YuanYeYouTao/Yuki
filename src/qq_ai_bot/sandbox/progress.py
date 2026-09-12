"""Carry the original Agent's bounded execution usage into deferred sandbox work."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from qq_ai_bot.sandbox.continuations import SandboxContinuationRepository
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository

current_progress: ContextVar[TaskProgress | None] = ContextVar(
    "sandbox_task_progress", default=None
)
PROCESS_ID = str(uuid4())


class TaskProgress:
    def __init__(
        self,
        max_models: int,
        max_tools: int,
        *,
        max_messages: int = 10,
        previous: dict[str, Any] | None = None,
    ) -> None:
        previous = previous or {}
        self.group_id = previous.get("group_id") or str(uuid4())
        self.max_models = int(previous.get("max_models", max_models))
        self.max_tools = int(previous.get("max_tools", max_tools))
        self.base_models = int(previous.get("models_used", 0))
        self.base_tools = int(previous.get("tools_used", 0))
        self.models_used, self.tools_used = self.base_models, self.base_tools
        self.max_messages = int(previous.get("max_messages", max_messages))
        self.messages_used = int(previous.get("messages_used", 0))
        if (
            min(
                self.max_models,
                self.max_tools,
                self.max_messages,
                self.base_models,
                self.base_tools,
                self.messages_used,
            )
            < 0
            or self.base_models > self.max_models
            or self.base_tools > self.max_tools
            or self.messages_used > self.max_messages
        ):
            raise ValueError("invalid_task_progress")
        self.phase = "running"
        self._records: dict[str, SandboxTaskRepository] = {}
        self._staged: dict[str, SandboxTaskRepository] = {}
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "max_models": self.max_models,
            "max_tools": self.max_tools,
            "models_used": self.models_used,
            "tools_used": self.tools_used,
            "phase": self.phase,
            "owner": PROCESS_ID,
            "max_messages": self.max_messages,
            "messages_used": self.messages_used,
        }

    async def bind(self, repository: SandboxTaskRepository, request_id: str) -> None:
        async with self._lock:
            self._records[request_id] = repository
            await repository.checkpoint(request_id, self.snapshot())

    async def checkpoint(self, *, models: int, tools: int) -> None:
        async with self._lock:
            self.models_used = max(self.models_used, self.base_models + models)
            self.tools_used = max(self.tools_used, self.base_tools + tools)
            if self.models_used > self.max_models or self.tools_used > self.max_tools:
                raise ValueError("sandbox_task_budget_exhausted")
            await self._persist()

    def stage_observed(self, repository: SandboxTaskRepository, request_id: str) -> None:
        self._staged[request_id] = repository

    async def confirm_observed(self) -> None:
        async with self._lock:
            for request_id, repository in self._staged.items():
                await SandboxContinuationRepository(repository.database).observed(request_id)
            self._staged.clear()

    async def finish(self, phase: str) -> None:
        async with self._lock:
            self.phase = phase
            await self._persist()

    async def reserve_message(self) -> None:
        """Persist before an effect; transport uncertainty never refunds its quota."""
        async with self._lock:
            if self.messages_used >= self.max_messages:
                raise ValueError("sandbox_task_message_budget_exhausted")
            self.messages_used += 1
            await self._persist()

    async def _persist(self) -> None:
        for request_id, repository in self._records.items():
            await repository.checkpoint(request_id, self.snapshot())
