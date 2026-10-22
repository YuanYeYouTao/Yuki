"""Runtime configuration writes keep their original domain writer and change history."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.authority.command_types import CommandOperation
from yuki.admin.authority.commands import ControlCommand, ControlResult
from yuki.admin.authority.json_types import JsonValue
from yuki.admin.authority.principal import ControlPrincipal
from yuki.admin.authority.problems import ProblemCode
from yuki.admin.config_service import ConfigRollbackPreparationChanged, RuntimeConfigService
from yuki.admin.control_resolution import audit_ref_from_actor
from yuki.admin.models import AdminActor, ConfigChangeResult
from yuki.admin.operations.base import CommandExecutor, authorize_current, fail
from yuki.identity.person_identity import current_person_id
from yuki.persistence.command_receipts import CommandSuccess
from yuki.persistence.database import write_failure_committed
from yuki.persistence.models import AdminOperationEventModel, RuntimeConfigOverrideModel


class ConfigOperations:
    def __init__(self, executor: CommandExecutor, config: RuntimeConfigService) -> None:
        self.executor, self.config = executor, config

    async def revision(
        self, principal: ControlPrincipal, key: str, *, scope_type: str, scope_id: str
    ) -> int:
        await authorize_current(self.executor.database, principal, "control.config.read")
        async with self.executor.database.read_session() as session:
            stmt = select(RuntimeConfigOverrideModel.version).where(
                RuntimeConfigOverrideModel.config_key == key,
                RuntimeConfigOverrideModel.scope_type == scope_type,
            )
            if scope_type == "user":
                stmt = stmt.where(
                    RuntimeConfigOverrideModel.canonical_person_id
                    == await current_person_id(session, scope_id)
                )
            elif scope_type == "group":
                stmt = stmt.where(RuntimeConfigOverrideModel.canonical_space_id == scope_id)
            return await session.scalar(stmt) or 0

    async def rollback_revision(self, principal: ControlPrincipal, change_id: int) -> int:
        await authorize_current(self.executor.database, principal, "control.config.read")
        async with self.executor.database.read_session() as session:
            row = await session.get(AdminOperationEventModel, change_id)
            if row is None or row.capability != "runtime_config":
                raise fail(ProblemCode.NOT_FOUND)
            if not row.success or row.operation not in {"set_override", "delete_override"}:
                # A rejected history entry has no target revision. The original
                # rollback writer returns its not_rollbackable domain result.
                return 0
            before = json.loads(row.before_json)
        return await self.revision(
            principal,
            before.get("key", row.target_id),
            scope_type=before["scope_type"],
            scope_id=before["scope_id"],
        )

    async def _change(
        self,
        principal: ControlPrincipal,
        command: ControlCommand,
        *,
        actor: AdminActor,
        action: str,
        key: str = "",
        value: object = None,
        scope_type: str = "global",
        scope_id: str = "",
        change_id: int = 0,
    ) -> ControlResult:
        operation = CommandOperation.CONFIG_MUTATE.value
        target = f"rollback:{change_id}" if action == "rollback" else key

        async def mutate(session: AsyncSession) -> CommandSuccess:
            kwargs: dict[str, Any] = {
                "actor_user_id": principal.person_id.text
                if principal.person_id
                else principal.principal_id.text,
                "trigger_message_id": actor.trigger_message_id,
                "conversation_key": actor.conversation_key,
                "audit_ref": audit_ref_from_actor(actor),
                "expected_version": command.expected_revision,
                "session": session,
            }
            result: ConfigChangeResult
            if action == "set":
                result = await self.config.set_override(
                    key, value, scope_type=scope_type, scope_id=scope_id, **kwargs
                )
            elif action == "unset":
                result = await self.config.delete_override(
                    key, scope_type=scope_type, scope_id=scope_id, **kwargs
                )
            else:
                result = await self.config.rollback(change_id, prepared=prepared_rollback, **kwargs)
            if not result.success:
                code = (
                    ProblemCode.VERSION_CONFLICT
                    if result.error_category == "version_conflict"
                    else ProblemCode.VALIDATION_ERROR
                )
                raise fail(code)
            rev = result.version or result.change_id or 1
            change: dict[str, JsonValue] = {
                "key": result.key,
                "scope_type": result.scope_type.value,
                "scope_id": result.scope_id,
                "change_id": result.change_id,
                "version": result.version,
                "apply_mode": result.apply_mode.value if result.apply_mode else None,
                "pending_restart": result.pending_restart,
            }
            state: dict[str, JsonValue] = {
                "resource": target,
                "revision": rev,
                "status": "applied",
                "change": change,
            }
            return CommandSuccess(target, rev, "config", target, {}, state, state)

        while True:
            prepared_rollback = (
                await self.config.prepare_rollback(change_id) if action == "rollback" else None
            )
            try:
                return await self.executor.database_command(
                    principal, command, operation, "control.config.mutate", target, mutate
                )
            except ConfigRollbackPreparationChanged as exc:
                if write_failure_committed(exc) is not False:
                    raise
                continue

    async def set(
        self,
        principal: ControlPrincipal,
        command: ControlCommand,
        *,
        actor: AdminActor,
        key: str,
        value: object,
        scope_type: str,
        scope_id: str,
    ) -> ControlResult:
        return await self._change(
            principal,
            command,
            actor=actor,
            action="set",
            key=key,
            value=value,
            scope_type=scope_type,
            scope_id=scope_id,
        )

    async def unset(
        self,
        principal: ControlPrincipal,
        command: ControlCommand,
        *,
        actor: AdminActor,
        key: str,
        scope_type: str,
        scope_id: str,
    ) -> ControlResult:
        return await self._change(
            principal,
            command,
            actor=actor,
            action="unset",
            key=key,
            scope_type=scope_type,
            scope_id=scope_id,
        )

    async def rollback(
        self,
        principal: ControlPrincipal,
        command: ControlCommand,
        *,
        actor: AdminActor,
        change_id: int,
    ) -> ControlResult:
        return await self._change(
            principal, command, actor=actor, action="rollback", change_id=change_id
        )
