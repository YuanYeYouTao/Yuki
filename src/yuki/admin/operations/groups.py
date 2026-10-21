"""Explicit shared Space admission changes."""

from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.authority.command_types import CommandOperation
from yuki.admin.authority.commands import ControlCommand, ControlResult
from yuki.admin.authority.principal import ControlPrincipal
from yuki.admin.authority.problems import ProblemCode
from yuki.admin.operations.base import CommandExecutor, fail, outcome, revision
from yuki.identity.db_models import CanonicalSpaceModel
from yuki.persistence.command_receipts import CommandSuccess
from yuki.persistence.repositories import GroupSettingsRepository


class GroupOperations:
    def __init__(self, executor: CommandExecutor, groups: GroupSettingsRepository) -> None:
        self.executor, self.groups = executor, groups

    async def set_enabled(
        self, principal: ControlPrincipal, identity: str, command: ControlCommand, *, enabled: bool
    ) -> ControlResult:
        operation = CommandOperation.GROUP_MUTATE.value

        async def mutate(session: AsyncSession) -> CommandSuccess:
            row = await session.get(CanonicalSpaceModel, identity)
            if row is None:
                raise fail(ProblemCode.NOT_FOUND)
            revision(row.revision, command.expected_revision)
            await self.groups.set_enabled(identity, enabled, session=session)
            await session.flush()
            return outcome(identity, row.revision, "enabled" if enabled else "disabled", operation)

        return await self.executor.database_command(
            principal, command, operation, "control.group.mutate", identity, mutate
        )
