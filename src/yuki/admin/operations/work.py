"""Management commands reuse domain services and the original receipt writer."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.authority.command_types import (
    CommandOperation,
)
from yuki.admin.authority.commands import ControlCommand, ControlResult
from yuki.admin.authority.principal import ControlPrincipal
from yuki.admin.authority.problems import ProblemCode
from yuki.admin.operations.base import CommandExecutor, fail, outcome
from yuki.persistence.command_receipts import (
    CommandSuccess,
)
from yuki.runtime.work_management import WorkManagementError, manage_work


class WorkOperations:
    def __init__(self, executor: CommandExecutor) -> None:
        self.executor = executor
        self.database = executor.database
        self.receipts = executor.receipts

    async def work_action(
        self, principal: ControlPrincipal, identity: str, action: str, command: ControlCommand
    ) -> ControlResult:
        operation = CommandOperation.WORK_MUTATE.value

        async def mutate(session: AsyncSession) -> CommandSuccess:
            try:
                rev, state = await manage_work(session, identity, command.expected_revision, action)
            except WorkManagementError as exc:
                raise fail(ProblemCode(exc.code)) from None
            return outcome(identity, rev, state, operation)

        return await self.executor.database_command(
            principal, command, operation, "control.work.mutate", identity, mutate
        )
