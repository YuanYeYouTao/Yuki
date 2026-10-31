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
from yuki.admin.operations.base import CommandExecutor, fail, outcome
from yuki.persistence.command_receipts import (
    CommandSuccess,
)


class ModelOperations:
    def __init__(self, executor: CommandExecutor, files: ConfigFileService) -> None:
        self.executor = executor
        self.database = executor.database
        self.receipts = executor.receipts
        self.files = files

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
