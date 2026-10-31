"""Management commands reuse domain services and the original receipt writer."""

from __future__ import annotations

from typing import Any

from yuki.admin.authority.command_types import (
    CommandOperation,
)
from yuki.admin.authority.commands import ControlCommand, ControlResult
from yuki.admin.authority.principal import ControlPrincipal
from yuki.admin.authority.problems import ProblemCode
from yuki.admin.config_files import ConfigFileError, ConfigFileService
from yuki.admin.model_connections import (
    ModelDiscoverInput,
    ModelDiscoverResult,
    ModelTestInput,
    ModelTestResult,
    discover_models,
    test_connection,
)
from yuki.admin.operations.base import CommandExecutor, authorize_current, fail, outcome
from yuki.model_runtime.dispatch_guard import model_dispatch_guard
from yuki.model_runtime.repository import ModelInvocationRepository
from yuki.model_runtime.usage import ModelStatsView, ModelUsageQuery
from yuki.persistence.command_receipts import (
    CommandSuccess,
)


class ModelOperations:
    def __init__(self, executor: CommandExecutor, files: ConfigFileService) -> None:
        self.executor = executor
        self.database = executor.database
        self.receipts = executor.receipts
        self.files = files

    async def test(self, principal: ControlPrincipal, source: ModelTestInput) -> ModelTestResult:
        async def check() -> None:
            await authorize_current(self.database, principal, "control.config.file.mutate")

        await check()
        with model_dispatch_guard(check):
            return await test_connection(self.files, source)

    async def discover(
        self, principal: ControlPrincipal, source: ModelDiscoverInput
    ) -> ModelDiscoverResult:
        async def check() -> None:
            await authorize_current(self.database, principal, "control.config.file.mutate")

        await check()
        with model_dispatch_guard(check):
            return await discover_models(self.files, source)

    async def stats(self, principal: ControlPrincipal, query: ModelUsageQuery) -> ModelStatsView:
        await authorize_current(self.database, principal, "control.config.file.content.read")
        repository = ModelInvocationRepository(self.database)
        return await repository.usage(query)

    async def apply_models(
        self, principal: ControlPrincipal, command: ControlCommand, spec: dict[str, Any]
    ) -> ControlResult:
        operation = CommandOperation.CONFIG_FILE_SAVE.value

        async def effect() -> CommandSuccess:
            try:
                selected = spec
                if "document" not in selected:
                    current = await self.files.read()
                    selected = {"document": current["document"], **spec}
                saved = await self.files.save(command.expected_revision, selected)
            except ConfigFileError as exc:
                raise fail(ProblemCode(exc.category)) from None
            apply_mode = (await self.files.read())["apply_mode"]
            return outcome(
                "model_profiles",
                saved,
                "applied" if apply_mode == "hot_reload" else "saved_pending_restart",
                operation,
            )

        return await self.executor.external_command(
            principal,
            command,
            operation,
            "control.config.file.mutate",
            "model_profiles",
            "save",
            effect,
        )
