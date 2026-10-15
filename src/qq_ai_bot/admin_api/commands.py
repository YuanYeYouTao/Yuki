"""Management commands reuse domain services and the original receipt writer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.authority.command_types import (
    CACHEABLE_COMMAND_FAILURES,
    CommandOperation,
    ControlCommandError,
    bind_command_hash,
    failure_audit_target_type,
    parse_attach_binding,
    success_audit_target_type,
)
from qq_ai_bot.admin.authority.commands import ControlCommand, ControlResult
from qq_ai_bot.admin.authority.json_types import JsonValue
from qq_ai_bot.admin.authority.principal import ControlPrincipal
from qq_ai_bot.admin.authority.problems import Problem, ProblemCode
from qq_ai_bot.admin.config_files import ConfigFileError, ConfigFileService
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from qq_ai_bot.persistence.command_receipts import (
    CommandReceiptRepository,
    CommandSuccess,
    pending_result,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import AdminOperationEventModel
from qq_ai_bot.persistence.unit_of_work import next_updated_at, state_revision
from qq_ai_bot.plugin_host.configuration_service import (
    PluginConfigurationError,
    PluginConfigurationService,
)
from qq_ai_bot.plugin_host.db_models import PluginNotificationOutboxModel
from qq_ai_bot.plugin_host.manager import (
    PluginManagementRejected,
    PluginManager,
    PluginRevisionConflict,
)
from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from qq_ai_bot.plugin_host.ownership import PluginOwnershipError
from qq_ai_bot.runtime.work_management import WorkManagementError, manage_work


def fail(code: ProblemCode) -> ControlCommandError:
    return ControlCommandError(Problem(code))


def authorize(principal: ControlPrincipal, capability: str) -> None:
    if not principal.allows(capability):
        raise fail(ProblemCode.CAPABILITY_DENIED)


def revision(actual: int, expected: int) -> None:
    if actual != expected:
        raise fail(ProblemCode.VERSION_CONFLICT)


def outcome(resource: str, rev: int, status: str, operation: str) -> CommandSuccess:
    state: dict[str, JsonValue] = {"resource": resource, "revision": rev, "status": status}
    return CommandSuccess(
        resource, rev, success_audit_target_type(operation), resource, {}, state, state
    )


class AdminCommands:
    def __init__(
        self, database: Database, plugins: PluginManager, files: ConfigFileService
    ) -> None:
        self.database = database
        self.plugins = plugins
        self.files = files
        self.receipts = CommandReceiptRepository(database)

    async def _replay(
        self,
        session: AsyncSession,
        row: ControlCommandReceiptModel,
        principal: ControlPrincipal,
        command: ControlCommand,
        operation: str,
        capability: str,
        target: str,
        bound: str,
    ) -> ControlResult:
        if row.payload_hash != bound:
            raise fail(ProblemCode.IDEMPOTENCY_CONFLICT)
        if row.status in {"running", "unknown"}:
            audit = await session.get(AdminOperationEventModel, row.audit_id)
            if audit is None or audit.target_id != target:
                raise fail(ProblemCode.STATE_MISMATCH)
            self.receipts._require_audit_correlation(
                audit,
                principal=principal,
                command=command,
                operation=operation,
                capability=capability,
            )
            return pending_result(row, audit)
        result = await self.receipts.replay_receipt(
            session,
            row,
            bound,
            principal=principal,
            command=command,
            operation=operation,
            capability=capability,
            semantic_target_id=target,
            material=command.payload,
            failure_target_type=failure_audit_target_type(operation),
        )
        if isinstance(result, Problem):
            raise ControlCommandError(result)
        return result

    async def _database_command(
        self,
        principal: ControlPrincipal,
        command: ControlCommand,
        operation: str,
        capability: str,
        target: str,
        mutate: Callable[[AsyncSession], Awaitable[CommandSuccess]],
    ) -> ControlResult:
        authorize(principal, capability)
        started = monotonic()
        bound = bind_command_hash(
            operation=operation,
            target_id=target,
            expected_revision=command.expected_revision,
            payload=command.payload,
        )
        problem = None
        result = None
        async with self.database.immediate_session() as session:
            old = await self.receipts.load_receipt(session, principal, command)
            if old is not None:
                return await self._replay(
                    session, old, principal, command, operation, capability, target, bound
                )
            try:
                async with session.begin_nested():
                    success = await mutate(session)
            except ControlCommandError as exc:
                if exc.problem.code not in CACHEABLE_COMMAND_FAILURES:
                    raise
                problem = await self.receipts.record_failure(
                    session,
                    principal=principal,
                    command=command,
                    operation=operation,
                    capability=capability,
                    target_type=failure_audit_target_type(operation),
                    target_id=target,
                    payload_hash=bound,
                    problem=exc.problem,
                    before={},
                    started=started,
                )
            else:
                result = await self.receipts.record_success(
                    session,
                    principal=principal,
                    command=command,
                    operation=operation,
                    capability=capability,
                    payload_hash=bound,
                    success=success,
                    semantic_target_id=target,
                    material=command.payload,
                    started=started,
                )
        if problem is not None:
            raise ControlCommandError(problem)
        assert result is not None
        return result

    async def _external_command(
        self,
        principal: ControlPrincipal,
        command: ControlCommand,
        operation: str,
        capability: str,
        target: str,
        action: str,
        effect: Callable[[], Awaitable[CommandSuccess]],
    ) -> ControlResult:
        authorize(principal, capability)
        started = monotonic()
        bound = bind_command_hash(
            operation=operation,
            target_id=target,
            expected_revision=command.expected_revision,
            payload=command.payload,
        )
        async with self.database.immediate_session() as session:
            old = await self.receipts.load_receipt(session, principal, command)
            if old is not None:
                return await self._replay(
                    session, old, principal, command, operation, capability, target, bound
                )
            await self.receipts.require_external_available(session, operation, target)
            await self.receipts.reserve_external(
                session,
                principal,
                command,
                operation=operation,
                capability=capability,
                target_type=failure_audit_target_type(operation),
                target=target,
                payload_hash=bound,
                action=action,
            )
        try:
            success = await effect()
            async with self.database.immediate_session() as session:
                reserved = await self.receipts.load_receipt(session, principal, command)
                return await self.receipts.record_success(
                    session,
                    principal=principal,
                    command=command,
                    operation=operation,
                    capability=capability,
                    payload_hash=bound,
                    success=success,
                    semantic_target_id=target,
                    material=command.payload,
                    started=started,
                    existing_receipt=reserved,
                )
        except BaseException as exc:
            if (
                isinstance(exc, ControlCommandError)
                and exc.problem.code in CACHEABLE_COMMAND_FAILURES
            ):
                async with self.database.immediate_session() as session:
                    reserved = await self.receipts.load_receipt(session, principal, command)
                    await self.receipts.record_failure(
                        session,
                        principal=principal,
                        command=command,
                        operation=operation,
                        capability=capability,
                        target_id=target,
                        target_type=failure_audit_target_type(operation),
                        payload_hash=bound,
                        problem=exc.problem,
                        before={},
                        started=started,
                        existing_receipt=reserved,
                    )
                raise
            # A committed result takes precedence when only its acknowledgement was lost.
            async with self.database.sessions() as session:
                old = await self.receipts.load_receipt(session, principal, command)
                if old is not None and old.status == "succeeded":
                    result = await self._replay(
                        session, old, principal, command, operation, capability, target, bound
                    )
                    if not isinstance(exc, Exception):
                        raise
                    return result
            result = await asyncio.shield(
                self.receipts.mark_unknown(
                    principal,
                    command,
                    operation=operation,
                    capability=capability,
                    interrupted=isinstance(exc, asyncio.CancelledError),
                    started=started,
                )
            )
            if not isinstance(exc, Exception):
                raise
            return result

    async def create_person(
        self, principal: ControlPrincipal, command: ControlCommand
    ) -> ControlResult:
        from uuid import uuid4

        operation = CommandOperation.PERSON_CREATE.value

        async def mutate(session: AsyncSession) -> CommandSuccess:
            revision(0, command.expected_revision)
            now = datetime.now(UTC)
            person = CanonicalPersonModel(id=str(uuid4()), created_at=now, updated_at=now)
            session.add(person)
            await session.flush()
            return outcome(person.id, person.revision, "created", operation)

        return await self._database_command(
            principal, command, operation, operation, "yuki", mutate
        )

    async def disable_person(
        self, principal: ControlPrincipal, identity: str, command: ControlCommand
    ) -> ControlResult:
        operation = CommandOperation.PERSON_DISABLE.value

        async def mutate(session: AsyncSession) -> CommandSuccess:
            person = await session.get(CanonicalPersonModel, identity)
            if person is None:
                raise fail(ProblemCode.NOT_FOUND)
            revision(person.revision, command.expected_revision)
            before: dict[str, JsonValue] = {"enabled": person.enabled, "revision": person.revision}
            if person.enabled:
                person.enabled = False
                person.revision += 1
                person.updated_at = next_updated_at(person.updated_at)
            after: dict[str, JsonValue] = {"enabled": person.enabled, "revision": person.revision}
            return CommandSuccess(
                identity, person.revision, "person", identity, before, after, after
            )

        return await self._database_command(
            principal, command, operation, operation, identity, mutate
        )

    async def attach_binding(
        self, principal: ControlPrincipal, identity: str, command: ControlCommand
    ) -> ControlResult:
        from uuid import uuid4

        operation = CommandOperation.BINDING_ATTACH.value
        parsed = parse_attach_binding(command.payload)

        async def mutate(session: AsyncSession) -> CommandSuccess:
            person = await session.get(CanonicalPersonModel, identity)
            if person is None:
                raise fail(ProblemCode.NOT_FOUND)
            revision(person.revision, command.expected_revision)
            existing = await session.scalar(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.platform == parsed.platform,
                    IdentityBindingModel.external_account_id == parsed.external_account_id,
                )
            )
            presence = await session.scalar(
                select(PresenceModel.id).where(
                    PresenceModel.platform == parsed.platform,
                    PresenceModel.external_account_id == parsed.external_account_id,
                )
            )
            if existing is not None:
                raise fail(
                    ProblemCode.PRECONDITION_FAILED
                    if existing.person_id == identity
                    else ProblemCode.BINDING_AMBIGUOUS
                )
            if presence is not None:
                raise fail(ProblemCode.PRECONDITION_FAILED)
            before: dict[str, JsonValue] = {"enabled": person.enabled, "revision": person.revision}
            now = datetime.now(UTC)
            binding = IdentityBindingModel(
                id=str(uuid4()),
                person_id=identity,
                platform=parsed.platform,
                external_account_id=parsed.external_account_id,
                display_name=parsed.display_name,
                status="active",
                revision=1,
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(binding)
            person.revision += 1
            person.updated_at = next_updated_at(person.updated_at, now)
            state: dict[str, JsonValue] = {
                "binding_id": binding.id,
                "person_id": identity,
                "platform": binding.platform,
                "status": binding.status,
                "revision": 1,
            }
            return CommandSuccess(
                binding.id,
                1,
                "identity_binding",
                binding.id,
                before,
                {**state, "owner_revision": person.revision},
                state,
            )

        return await self._database_command(
            principal, command, operation, operation, identity, mutate
        )

    async def disable_binding(
        self, principal: ControlPrincipal, identity: str, command: ControlCommand
    ) -> ControlResult:
        operation = CommandOperation.BINDING_DISABLE.value

        async def mutate(session: AsyncSession) -> CommandSuccess:
            binding = await session.get(IdentityBindingModel, identity)
            if binding is None:
                raise fail(ProblemCode.NOT_FOUND)
            revision(binding.revision, command.expected_revision)
            if binding.status != "disabled":
                binding.status = "disabled"
                binding.revision += 1
                binding.updated_at = next_updated_at(binding.updated_at)
            return outcome(identity, binding.revision, "disabled", operation)

        return await self._database_command(
            principal, command, operation, operation, identity, mutate
        )

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
                    return outcome(
                        plugin_id,
                        state_revision(record.updated_at),
                        "healthy" if not report.problems else "unhealthy",
                        operation,
                    )
                return outcome(
                    plugin_id, state_revision(record.updated_at), record.status, operation
                )
            except PluginRevisionConflict:
                raise fail(ProblemCode.VERSION_CONFLICT) from None
            except PluginManagementRejected:
                raise fail(ProblemCode.PRECONDITION_FAILED) from None

        return await self._external_command(
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

        return await self._external_command(
            principal, command, operation, "control.plugin.config.mutate", plugin_id, "save", effect
        )

    async def apply_models(
        self, principal: ControlPrincipal, command: ControlCommand, spec: dict[str, Any]
    ) -> ControlResult:
        operation = CommandOperation.CONFIG_FILE_SAVE.value

        async def effect() -> CommandSuccess:
            try:
                selected = spec
                if "document" not in selected:
                    current = await self.files.read("model_profiles")
                    selected = {"document": current["document"], **spec}
                saved = await self.files.save("model_profiles", command.expected_revision, selected)
            except ConfigFileError as exc:
                raise fail(ProblemCode(exc.category)) from None
            return outcome(
                "model_profiles",
                saved,
                "applied" if self.files.model_hot_reload_enabled else "saved_pending_restart",
                operation,
            )

        return await self._external_command(
            principal,
            command,
            operation,
            "control.config.file.mutate",
            "model_profiles",
            "save",
            effect,
        )

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

        return await self._database_command(
            principal, command, operation, "control.work.mutate", identity, mutate
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

        return await self._database_command(
            principal, command, operation, "control.plugin.mutate", str(identity), mutate
        )
