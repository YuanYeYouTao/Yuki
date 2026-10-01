"""One application-owned runtime shared by all conversations and durable work."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.runtime.activation_bindings import ActiveWorkBindings
from qq_ai_bot.services.agent_runner import AgentRunner
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.services.main_agent_turns import MainAgentTurnService


class RuntimeWorker(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def health(self) -> dict[str, object]: ...


class YukiRuntime:
    """Share execution resources, never a conversation's temporary context.

    Durable Work and domain receipts survive this object. Startup reconstructs
    activations through the registered bounded recovery workers; the activity
    index only assists inputs arriving from a different asyncio task.
    """

    def __init__(
        self,
        main_turns: MainAgentTurnService,
        runner: AgentRunner,
        bindings: ActiveWorkBindings,
    ) -> None:
        self.main_turns = main_turns
        self.runner = runner
        self.bindings = bindings
        self.executions = bindings.executions
        if main_turns.executions is not self.executions:
            raise ValueError("runtime_execution_owner_mismatch")
        self._workers = LifecycleRegistry()
        self._closing_owner: asyncio.Task[Any] | None = None
        self._closed = asyncio.Event()

    @property
    def contract(self) -> MainAgentContract:
        contract = self.runner.main_contract
        if contract is None:
            raise RuntimeError("main_agent_contract_unbound")
        return contract

    def register_worker(self, name: str, worker: RuntimeWorker) -> None:
        # Composition-time only: LifecycleRegistry rejects changes after start.
        self._workers.register(name, start=worker.start, close=worker.close, health=worker.health)

    async def start(self) -> None:
        if not self.executions.accepting:
            raise RuntimeError("yuki_runtime_closing")
        try:
            await self._workers.start()
        except BaseException:
            self.executions.stop_admission()
            await self.executions.drain()
            raise

    async def close(self) -> None:
        current = asyncio.current_task()
        if self._closing_owner is not None:
            if self._closing_owner is not current:
                await self._closed.wait()
            return
        self._closing_owner = current
        self.executions.stop_admission(current)
        try:
            try:
                await self._workers.close()
            finally:
                await self.executions.drain()
        finally:
            self._closed.set()

    async def health(self) -> dict[str, object]:
        return {
            "accepting": self.executions.accepting,
            "active_executions": self.executions.active_count,
            "workers": await self._workers.health(),
        }
