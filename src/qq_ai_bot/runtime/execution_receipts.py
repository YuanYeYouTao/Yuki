"""Turn-local receipt staging; durable work alone owns budgets and lifecycle."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

PROCESS_ID = str(uuid4())


@dataclass
class ExecutionReceipts:
    staged: dict[str, Any] = field(default_factory=dict)

    async def confirm(self) -> None:
        from qq_ai_bot.sandbox.continuations import SandboxContinuationRepository

        for request_id, repository in tuple(self.staged.items()):
            await SandboxContinuationRepository(repository.database).observed(request_id)
            self.staged.pop(request_id, None)


current_receipts: ContextVar[ExecutionReceipts | None] = ContextVar(
    "execution_receipts", default=None
)
