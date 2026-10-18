"""Management commands reuse domain services and the original receipt writer."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.authority.command_types import (
    CommandOperation,
)
from yuki.admin.authority.commands import ControlCommand, ControlResult
from yuki.admin.authority.principal import ControlPrincipal
from yuki.admin.authority.problems import ProblemCode
from yuki.admin.operations.base import CommandExecutor, fail, outcome, revision
from yuki.persistence.command_receipts import (
    CommandSuccess,
)
from yuki.persistence.unit_of_work import state_revision
from yuki.plugin_host.configuration_service import (
    PluginConfigurationError,
    PluginConfigurationService,
)
from yuki.plugin_host.db_models import PluginNotificationOutboxModel
from yuki.plugin_host.manager import (
    PluginManagementRejected,
    PluginManager,
    PluginRevisionConflict,
)
from yuki.plugin_host.notification_repository import PluginNotificationRepository
from yuki.plugin_host.ownership import PluginOwnershipError


class PluginOperations:
    def __init__(self, executor: CommandExecutor, plugins: PluginManager) -> None:
        self.executor = executor
        self.database = executor.database
        self.receipts = executor.receipts
        self.plugins = plugins

    async def plugin_action(
        self,
        principal: ControlPrincipal,
        plugin_id: str,
        action: str,
        command: ControlCommand,
        permissions: list[str] | None = None,
    ) -> ControlResult:
        operation = CommandOperation.PLUGIN_MUTATE.value

        async def effect() -> CommandSuccess:
            try:
                if action == "discover":
                    revision(0, command.expected_revision)
                    await self.plugins.discover()
                    return outcome("yuki", 1, "discovered", operation)
                if action == "approve":
                    record = await self.plugins.approve(
                        plugin_id,
                        actor_user_id=principal.principal_id.text,
                        expected_revision=command.expected_revision,
                        permissions=permissions,
                    )
                elif action in {"enable", "disable"}:
                    record = await getattr(self.plugins, action)(
                        plugin_id,
                        actor_user_id=principal.principal_id.text,
                        expected_revision=command.expected_revision,
                    )
                    if action == "enable" and record.status == "failed":
                        raise fail(ProblemCode.PRECONDITION_FAILED)
                else:
                    shown = await self.plugins.show(plugin_id)
                    if shown is None:
                        raise fail(ProblemCode.NOT_FOUND)
                    record = shown
                    revision(state_revision(record.updated_at), command.expected_revision)
                    report = await self.plugins.doctor(plugin_id)
                    result = outcome(
                        plugin_id,
                        state_revision(record.updated_at),
                        "healthy" if not report.problems else "unhealthy",
                        operation,
                    )
                    state = {
                        **result.effective_state,
                        "diagnostics": report.model_dump(mode="json"),
                    }
                    return CommandSuccess(
                        plugin_id, result.revision, "plugin", plugin_id, {}, state, state
                    )
                return outcome(
                    plugin_id, state_revision(record.updated_at), record.status, operation
                )
            except PluginRevisionConflict:
                raise fail(ProblemCode.VERSION_CONFLICT) from None
            except PluginManagementRejected:
                raise fail(ProblemCode.PRECONDITION_FAILED) from None

        return await self.executor.external_command(
            principal, command, operation, "control.plugin.mutate", plugin_id, action, effect
        )

    async def configure_plugin(
        self,
        principal: ControlPrincipal,
        plugin_id: str,
        command: ControlCommand,
        spec: dict[str, Any],
    ) -> ControlResult:
        operation = CommandOperation.PLUGIN_CONFIGURE.value

        async def effect() -> CommandSuccess:
            try:
                saved = await PluginConfigurationService(
                    self.database, self.plugins.configuration_schema
                ).save(plugin_id, command.expected_revision, spec)
            except PluginConfigurationError as exc:
                raise fail(ProblemCode(exc.category)) from None
            return outcome(plugin_id, saved, "saved", operation)

        return await self.executor.external_command(
            principal, command, operation, "control.plugin.config.mutate", plugin_id, "save", effect
        )

    async def retry_outbox(
        self, principal: ControlPrincipal, identity: int, command: ControlCommand
    ) -> ControlResult:
        operation = CommandOperation.PLUGIN_MUTATE.value

        async def mutate(session: AsyncSession) -> CommandSuccess:
            row = await session.get(PluginNotificationOutboxModel, identity)
            if row is None:
                raise fail(ProblemCode.NOT_FOUND)
            revision(state_revision(row.updated_at), command.expected_revision)
            if (
                row.status != "failed"
                or row.platform_message_id is not None
                or row.last_error_category
                not in {"bot_unavailable", "gateway_disconnected", "effect_gate_timeout"}
                or row.attempts >= row.max_attempts
            ):
                raise fail(ProblemCode.PRECONDITION_FAILED)
            try:
                await PluginNotificationRepository(self.database).retry_outbox(
                    identity, error_category="manual_retry", session=session
                )
            except PluginOwnershipError:
                raise fail(ProblemCode.STATE_MISMATCH) from None
            return outcome(str(identity), state_revision(row.updated_at), row.status, operation)

        return await self.executor.database_command(
            principal, command, operation, "control.plugin.mutate", str(identity), mutate
        )
