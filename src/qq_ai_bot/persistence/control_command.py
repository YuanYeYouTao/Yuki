"""Persistence adapter for control-plane identity and route commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.authority.command_types import (
    CACHEABLE_COMMAND_FAILURES,
    YUKI_TARGET_TOKEN,
    AttachBindingPayload,
    AttachSpaceBindingPayload,
    CommandOperation,
    ControlCommandError,
    RegisterPresencePayload,
    RouteActionPayload,
    SetIngestPayload,
    SetRoutePayload,
    bind_command_hash,
    failure_audit_target_type,
    parse_attach_binding,
    parse_attach_space_binding,
    parse_config_write,
    parse_management_action,
    parse_register_presence,
    parse_route_action,
    parse_set_ingest,
    parse_set_route,
    require_command_target,
    require_empty_payload,
    success_audit_target_type,
)
from qq_ai_bot.admin.authority.commands import ControlCommand, ControlResult
from qq_ai_bot.admin.authority.json_types import JsonObject, JsonValue
from qq_ai_bot.admin.authority.operations import OperationRef
from qq_ai_bot.admin.authority.principal import ControlPrincipal
from qq_ai_bot.admin.authority.problems import Problem, ProblemCode
from qq_ai_bot.admin.config_files import ConfigFileService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.config import Settings
from qq_ai_bot.control_plane.query_types import (
    RouteKind,
    RouteReferenceState,
    classify_route_reference,
)
from qq_ai_bot.conversation.canonical_db_models import (
    PersonActiveRouteModel,
    SpaceActiveRouteModel,
    SpaceBindingIngestRouteModel,
)
from qq_ai_bot.domain.control import YukiControlTarget
from qq_ai_bot.domain.identity import (
    IdentityBindingId,
    PersonId,
    PresenceId,
    SpaceBindingId,
    SpaceId,
)
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.memory.embedding.runtime import MemoryEmbeddingRuntime
from qq_ai_bot.memory.maintenance import MemoryMaintenanceWorker
from qq_ai_bot.memory.rebuild.service import MemoryRebuildService
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.command_receipts import (
    CommandReceiptRepository,
)
from qq_ai_bot.persistence.command_receipts import (
    CommandSuccess as _Success,
)
from qq_ai_bot.persistence.control_external import ExternalControlExecutor
from qq_ai_bot.persistence.control_management import (
    ControlManagementGateway,
    ManagementFailure,
    ManagementMutation,
    ManagementUnavailable,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.manager import PluginManager
from qq_ai_bot.workspace.service import WorkspaceService

_INVALID_TARGET = "invalid"


class _CachedFailure(Exception):
    def __init__(self, problem: Problem, *, before: Mapping[str, JsonValue] | None = None) -> None:
        if type(problem) is not Problem:
            raise TypeError("problem must be Problem")
        self.problem = problem
        self.before = dict(before or {})
        super().__init__(problem.code.value)


def _now() -> datetime:
    return datetime.now(UTC)


def _problem(code: ProblemCode) -> Problem:
    return Problem(code)


def _fail(code: ProblemCode, *, before: Mapping[str, JsonValue] | None = None) -> _CachedFailure:
    return _CachedFailure(_problem(code), before=before)


def _require_parsed[T](value: T | None) -> T:
    if value is None:
        raise _fail(ProblemCode.VALIDATION_ERROR)
    return value


def _target_problem(target: object, expected: type[object] | None) -> Problem | None:
    if expected is None:
        return None
    try:
        require_command_target(target, expected)
    except ControlCommandError as exc:
        return exc.problem
    return None


def _route_owner_type(kind: RouteKind) -> type[object]:
    if kind is RouteKind.PERSON_ACTIVE:
        return PersonId
    if kind is RouteKind.SPACE_BINDING_INGEST:
        return SpaceBindingId
    return SpaceId


def _canonical_text(value: object) -> str:
    if type(value) is PersonId:
        return value.text
    if type(value) is SpaceId:
        return value.text
    if type(value) is IdentityBindingId:
        return value.text
    if type(value) is SpaceBindingId:
        return value.text
    if type(value) is PresenceId:
        return value.text
    if value is YukiControlTarget.PERMANENT_YUKI:
        return YUKI_TARGET_TOKEN
    return _INVALID_TARGET


def _require_target[T](
    value: object,
    expected: type[T],
    *,
    before: Mapping[str, JsonValue] | None = None,
) -> T:
    if type(value) is not expected:
        raise _fail(ProblemCode.VALIDATION_ERROR, before=before)
    return value


def _require_revision(
    actual: int, expected: int, *, before: Mapping[str, JsonValue] | None = None
) -> None:
    if actual != expected:
        raise _fail(ProblemCode.VERSION_CONFLICT, before=before)


def _new_uuid4() -> str:
    return str(uuid4())


def _integrity_text(exc: IntegrityError) -> str:
    origin = getattr(exc, "orig", None)
    return str(origin if origin is not None else exc).casefold()


def _map_integrity(exc: IntegrityError) -> ProblemCode | None:
    text = _integrity_text(exc)
    if "control_command_receipts" in text and "unique" in text:
        return None
    if "unique" in text and any(
        name in text
        for name in (
            "identity_bindings",
            "space_bindings",
            "presences",
            "uq_identity_bindings",
            "uq_space_bindings",
            "uq_presences",
        )
    ):
        return ProblemCode.BINDING_AMBIGUOUS
    if "unique" in text and any(
        name in text
        for name in (
            "person_active_routes",
            "space_binding_ingest_routes",
            "space_active_routes",
        )
    ):
        return ProblemCode.VERSION_CONFLICT
    if "mismatch" in text:
        return ProblemCode.ROUTE_AMBIGUOUS
    if "foreign key" in text:
        return ProblemCode.NOT_FOUND
    return ProblemCode.STATE_MISMATCH


class ControlCommandAdapter:
    """BEGIN IMMEDIATE writer for C11 identity and route commands."""

    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        workspace_service: WorkspaceService | None = None,
        rebuild_service: MemoryRebuildService | None = None,
        config_files: ConfigFileService | None = None,
        runtime_config: RuntimeConfigService | None = None,
        maintenance: MemoryMaintenanceWorker | None = None,
        embeddings: MemoryEmbeddingRuntime | None = None,
        automation: AutomationService | None = None,
        memories: MemoryFactService | None = None,
        plugins: PluginManager | None = None,
    ) -> None:
        if type(database) is not Database:
            raise TypeError("database must be Database")
        self._database = database
        self._memories = memories
        self._receipts = CommandReceiptRepository(database)
        self._management = ControlManagementGateway(
            database,
            settings=settings,
            config_files=config_files,
            runtime_config=runtime_config,
            maintenance=maintenance,
            embeddings=embeddings,
            automation=automation,
            memories=memories,
            plugins=plugins,
            workspace_service=workspace_service,
            rebuild_service=rebuild_service,
        )
        self._external = ExternalControlExecutor(self)

    async def recover_interrupted_controls(self) -> int:
        return await self._receipts.recover_interrupted_controls()

    async def enable_person(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parse_problem, material = _empty_payload(command)
        return await self._execute(
            principal,
            command,
            operation=CommandOperation.PERSON_ENABLE.value,
            capability="identity.person.enable",
            target_id=_canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=_target_problem(target, PersonId),
            mutate=lambda session: self._toggle_person(session, target, command, enabled=True),
        )

    async def disable_person(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parse_problem, material = _empty_payload(command)
        return await self._execute(
            principal,
            command,
            operation=CommandOperation.PERSON_DISABLE.value,
            capability="identity.person.disable",
            target_id=_canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=_target_problem(target, PersonId),
            mutate=lambda session: self._toggle_person(session, target, command, enabled=False),
        )

    async def attach_identity_binding(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parsed, material, parse_problem = _try_parse(command, parse_attach_binding)
        return await self._execute(
            principal,
            command,
            operation=CommandOperation.BINDING_ATTACH.value,
            capability="identity.binding.attach",
            target_id=_canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=_target_problem(target, PersonId),
            mutate=lambda session: self._attach_person_binding(session, target, command, parsed),
        )

    async def enable_space(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parse_problem, material = _empty_payload(command)
        return await self._execute(
            principal,
            command,
            operation=CommandOperation.SPACE_ENABLE.value,
            capability="identity.space.enable",
            target_id=_canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=_target_problem(target, SpaceId),
            mutate=lambda session: self._toggle_space(session, target, command, enabled=True),
        )

    async def disable_space(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parse_problem, material = _empty_payload(command)
        return await self._execute(
            principal,
            command,
            operation=CommandOperation.SPACE_DISABLE.value,
            capability="identity.space.disable",
            target_id=_canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=_target_problem(target, SpaceId),
            mutate=lambda session: self._toggle_space(session, target, command, enabled=False),
        )

    async def attach_space_binding(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parsed, material, parse_problem = _try_parse(command, parse_attach_space_binding)
        return await self._execute(
            principal,
            command,
            operation=CommandOperation.SPACE_BINDING_ATTACH.value,
            capability="identity.space.binding.attach",
            target_id=_canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=_target_problem(target, SpaceId),
            mutate=lambda session: self._attach_space_binding(session, target, command, parsed),
        )

    async def register_presence(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parsed, material, parse_problem = _try_parse(command, parse_register_presence)
        return await self._execute(
            principal,
            command,
            operation=CommandOperation.PRESENCE_REGISTER.value,
            capability="identity.presence.register",
            target_id=YUKI_TARGET_TOKEN,
            material=material,
            parse_problem=parse_problem,
            target_problem=_target_problem(target, YukiControlTarget),
            mutate=lambda session: self._register_presence(session, command, parsed),
        )

    async def start_presence(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parse_problem, material = _empty_payload(command)
        return await self._execute(
            principal,
            command,
            operation=CommandOperation.PRESENCE_START.value,
            capability="identity.presence.start",
            target_id=_canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=_target_problem(target, PresenceId),
            mutate=lambda session: self._set_presence_enabled(
                session, target, command, enabled=True
            ),
        )

    async def stop_presence(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parse_problem, material = _empty_payload(command)
        return await self._execute(
            principal,
            command,
            operation=CommandOperation.PRESENCE_STOP.value,
            capability="identity.presence.stop",
            target_id=_canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=_target_problem(target, PresenceId),
            mutate=lambda session: self._set_presence_enabled(
                session, target, command, enabled=False
            ),
        )

    async def set_presence_ingest(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parsed, material, parse_problem = _try_parse(command, parse_set_ingest)
        return await self._execute(
            principal,
            command,
            operation=CommandOperation.PRESENCE_SET_INGEST.value,
            capability="identity.presence.set_ingest",
            target_id=_canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=_target_problem(target, PresenceId),
            mutate=lambda session: self._set_presence_ingest(session, target, command, parsed),
        )

    async def set_route(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parsed, material, parse_problem = _try_parse(command, parse_set_route)
        operation = CommandOperation.ROUTE_SET.value
        expected: type[object] | None = None
        if parsed is not None:
            operation = f"{operation}.{parsed.kind.value}"
            expected = _route_owner_type(parsed.kind)
        return await self._execute(
            principal,
            command,
            operation=operation,
            capability="route.set",
            target_id=_canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=_target_problem(target, expected),
            mutate=lambda session: self._set_route(session, target, command, parsed),
        )

    async def pause_route(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parsed, material, parse_problem = _try_parse(command, parse_route_action)
        operation = CommandOperation.ROUTE_PAUSE.value
        expected: type[object] | None = None
        if parsed is not None:
            operation = f"{operation}.{parsed.kind.value}"
            expected = _route_owner_type(parsed.kind)
        return await self._execute(
            principal,
            command,
            operation=operation,
            capability="route.pause",
            target_id=_canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=_target_problem(target, expected),
            mutate=lambda session: self._pause_resume_route(
                session, target, command, parsed, paused=True
            ),
        )

    async def resume_route(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parsed, material, parse_problem = _try_parse(command, parse_route_action)
        operation = CommandOperation.ROUTE_RESUME.value
        expected: type[object] | None = None
        if parsed is not None:
            operation = f"{operation}.{parsed.kind.value}"
            expected = _route_owner_type(parsed.kind)
        return await self._execute(
            principal,
            command,
            operation=operation,
            capability="route.resume",
            target_id=_canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=_target_problem(target, expected),
            mutate=lambda session: self._pause_resume_route(
                session, target, command, parsed, paused=False
            ),
        )

    async def set_config(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parsed, material, parse_problem = _try_parse(
            command, lambda payload: parse_config_write(payload, require_value=True)
        )
        return await self._execute(
            principal,
            command,
            operation=CommandOperation.CONFIG_SET.value,
            capability="control.config.mutate",
            target_id=parsed.key if parsed is not None else _canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=None,
            mutate=lambda session: self._run_management(
                CommandOperation.CONFIG_SET.value,
                lambda: self._management.set_config(
                    session, principal, command, _require_parsed(parsed)
                ),
            ),
        )

    async def unset_config(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        parsed, material, parse_problem = _try_parse(
            command, lambda payload: parse_config_write(payload, require_value=False)
        )
        return await self._execute(
            principal,
            command,
            operation=CommandOperation.CONFIG_UNSET.value,
            capability="control.config.mutate",
            target_id=parsed.key if parsed is not None else _canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=None,
            mutate=lambda session: self._run_management(
                CommandOperation.CONFIG_UNSET.value,
                lambda: self._management.unset_config(
                    session, principal, command, _require_parsed(parsed)
                ),
            ),
        )

    async def mutate_memory(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        return await self._management_action(
            principal,
            target,
            command,
            operation=CommandOperation.MEMORY_MUTATE.value,
            capability="control.memory.mutate",
            invoke=self._management.mutate_memory,
        )

    async def rebuild_memory(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        return await self._management_action(
            principal,
            target,
            command,
            operation=CommandOperation.MEMORY_REBUILD.value,
            capability="control.memory.rebuild",
            invoke=self._management.rebuild_memory,
        )

    async def dream_memory(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        return await self._management_action(
            principal,
            target,
            command,
            operation=CommandOperation.MEMORY_DREAM.value,
            capability="control.memory.dream",
            invoke=self._management.dream_memory,
        )

    async def maintain_memory(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        return await self._management_action(
            principal,
            target,
            command,
            operation=CommandOperation.MEMORY_MAINTENANCE.value,
            capability="control.memory.maintenance",
        )

    async def mutate_automation(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        return await self._management_action(
            principal,
            target,
            command,
            operation=CommandOperation.AUTOMATION_MUTATE.value,
            capability="control.automation.mutate",
            invoke=self._management.mutate_automation,
        )

    async def save_config_file(
        self, principal: ControlPrincipal, target: object, command: ControlCommand
    ) -> ControlResult:
        if target is not YukiControlTarget.PERMANENT_YUKI:
            raise ControlCommandError(Problem(ProblemCode.VALIDATION_ERROR))
        return await self._management_action(
            principal,
            target,
            command,
            operation=CommandOperation.CONFIG_FILE_SAVE.value,
            capability="control.config.file.mutate",
        )

    async def mutate_plugin(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        return await self._management_action(
            principal,
            target,
            command,
            operation=CommandOperation.PLUGIN_MUTATE.value,
            capability="control.plugin.mutate",
            invoke=self._management.retry_plugin_notification,
        )

    async def configure_plugin(
        self, principal: ControlPrincipal, target: object, command: ControlCommand
    ) -> ControlResult:
        if target is not YukiControlTarget.PERMANENT_YUKI:
            raise ControlCommandError(Problem(ProblemCode.VALIDATION_ERROR))
        return await self._management_action(
            principal,
            target,
            command,
            operation=CommandOperation.PLUGIN_CONFIGURE.value,
            capability="control.plugin.config.mutate",
        )

    async def mutate_environment_file(
        self, principal: ControlPrincipal, target: object, command: ControlCommand
    ) -> ControlResult:
        return await self._management_action(
            principal,
            target,
            command,
            operation=CommandOperation.ENVIRONMENT_FILE_MUTATE.value,
            capability="control.environment.file.mutate",
        )

    async def mutate_environment_terminal(
        self, principal: ControlPrincipal, target: object, command: ControlCommand
    ) -> ControlResult:
        return await self._management_action(
            principal,
            target,
            command,
            operation=CommandOperation.TERMINAL_MUTATE.value,
            capability="control.terminal.mutate",
        )

    async def mutate_work(
        self, principal: ControlPrincipal, target: object, command: ControlCommand
    ) -> ControlResult:
        return await self._management_action(
            principal,
            target,
            command,
            operation=CommandOperation.WORK_MUTATE.value,
            capability="control.work.mutate",
            invoke=self._management.mutate_work,
        )

    async def mutate_emoji(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        return await self._management_action(
            principal,
            target,
            command,
            operation=CommandOperation.EMOJI_MUTATE.value,
            capability="control.emoji.mutate",
            invoke=lambda session, principal, command, parsed: self._management.mutate_emoji(
                session, command, parsed
            ),
        )

    async def retry_operation(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
    ) -> ControlResult:
        return await self._management_action(
            principal,
            target,
            command,
            operation=CommandOperation.OPERATION_RETRY.value,
            capability="control.operation.retry",
            invoke=self._management.retry_operation,
        )

    async def _management_action(
        self,
        principal: ControlPrincipal,
        target: object,
        command: ControlCommand,
        *,
        operation: str,
        capability: str,
        invoke: Callable[..., Awaitable[ManagementMutation]] | None = None,
    ) -> ControlResult:
        parsed, material, parse_problem = _try_parse(command, parse_management_action)
        if parsed is not None and (
            operation
            in {
                CommandOperation.ENVIRONMENT_FILE_MUTATE.value,
                CommandOperation.TERMINAL_MUTATE.value,
                CommandOperation.MEMORY_MAINTENANCE.value,
                CommandOperation.CONFIG_FILE_SAVE.value,
                CommandOperation.PLUGIN_CONFIGURE.value,
            }
            or (operation == CommandOperation.PLUGIN_MUTATE.value and parsed.action != "retry")
        ):
            return await self._external.execute(
                principal,
                command,
                operation=operation,
                capability=capability,
                parsed=parsed,
                material=material,
            )
        prepared = None
        if parsed is not None and operation in {
            CommandOperation.MEMORY_REBUILD.value,
            CommandOperation.MEMORY_DREAM.value,
        }:
            async with self._database.sessions() as reader:
                existing = await self._receipts.load_receipt(reader, principal, command)
            if existing is None:
                try:
                    prepared = await self._management.prepare(operation, parsed)
                except ManagementUnavailable as exc:
                    raise ControlCommandError(Problem(ProblemCode.OPERATION_UNAVAILABLE)) from exc
                except ManagementFailure as exc:
                    parse_problem = Problem(exc.code)
        return await self._execute(
            principal,
            command,
            operation=operation,
            capability=capability,
            target_id=parsed.resource_id if parsed is not None else _canonical_text(target),
            material=material,
            parse_problem=parse_problem,
            target_problem=None,
            mutate=lambda session: self._run_management(
                operation,
                lambda: _require_parsed(invoke)(
                    session,
                    principal,
                    command,
                    _require_parsed(parsed),
                    **({"prepared": prepared} if prepared is not None else {}),
                ),
            ),
        )

    async def _run_management(
        self,
        operation: str,
        invoke: Callable[[], Awaitable[ManagementMutation]],
    ) -> _Success:
        try:
            mutation = await invoke()
        except ManagementUnavailable as exc:
            raise ControlCommandError(Problem(ProblemCode.OPERATION_UNAVAILABLE)) from exc
        except ManagementFailure as exc:
            if exc.code is ProblemCode.OPERATION_UNAVAILABLE:
                raise ControlCommandError(Problem(exc.code)) from None
            raise _fail(exc.code) from None
        return self._management_success(
            mutation.resource_id,
            mutation.revision,
            mutation.status,
            operation=operation,
            op_ref=mutation.operation,
        )

    def _management_success(
        self,
        resource_id: str,
        revision: int,
        status: str,
        *,
        operation: str,
        op_ref: OperationRef | None = None,
    ) -> _Success:
        state: dict[str, JsonValue] = {
            "resource": resource_id,
            "revision": revision,
            "status": status,
        }
        return _Success(
            resource_id=resource_id,
            revision=revision,
            target_type=success_audit_target_type(operation),
            target_id=resource_id,
            before={},
            after=state,
            effective_state=state,
            operation=op_ref,
        )

    async def _execute(
        self,
        principal: ControlPrincipal,
        command: ControlCommand,
        *,
        operation: str,
        capability: str,
        target_id: str,
        material: JsonObject,
        parse_problem: Problem | None,
        target_problem: Problem | None,
        mutate: Callable[[AsyncSession], Awaitable[_Success]],
    ) -> ControlResult:
        bound_hash = bind_command_hash(
            operation=operation,
            target_id=target_id,
            expected_revision=command.expected_revision,
            payload=material,
        )
        started = monotonic()
        pending: Problem | None = None
        result: ControlResult | None = None
        validation = parse_problem or target_problem
        failure_target_type = failure_audit_target_type(operation)
        memory_snapshot = (
            operation == CommandOperation.MEMORY_MUTATE.value and self._memories is not None
        )

        async def execute(session: AsyncSession) -> None:
            nonlocal pending, result
            pending = None
            result = None
            existing = await self._receipts.load_receipt(session, principal, command)
            if existing is not None:
                replay = await self._receipts.replay_receipt(
                    session,
                    existing,
                    bound_hash,
                    principal=principal,
                    command=command,
                    operation=operation,
                    capability=capability,
                    semantic_target_id=target_id,
                    material=material,
                    failure_target_type=failure_target_type,
                )
                if type(replay) is ControlResult:
                    result = replay
                else:
                    pending = replay
            elif validation is not None:
                pending = await self._receipts.record_failure(
                    session,
                    principal=principal,
                    command=command,
                    operation=operation,
                    capability=capability,
                    target_type=failure_target_type,
                    target_id=target_id,
                    payload_hash=bound_hash,
                    problem=validation,
                    before={},
                    started=started,
                )
            else:
                try:
                    # A domain rejection may follow a partial database write.
                    # Roll it back before committing the original failure receipt.
                    async with session.begin_nested():
                        success = await mutate(session)
                except _CachedFailure as failure:
                    if failure.problem.code not in CACHEABLE_COMMAND_FAILURES:
                        raise ControlCommandError(failure.problem) from None
                    pending = await self._receipts.record_failure(
                        session,
                        principal=principal,
                        command=command,
                        operation=operation,
                        capability=capability,
                        target_type=failure_target_type,
                        target_id=target_id,
                        payload_hash=bound_hash,
                        problem=failure.problem,
                        before=failure.before,
                        started=started,
                    )
                else:
                    result = await self._receipts.record_success(
                        session,
                        principal=principal,
                        command=command,
                        operation=operation,
                        capability=capability,
                        payload_hash=bound_hash,
                        success=success,
                        semantic_target_id=target_id,
                        material=material,
                        started=started,
                    )

        try:
            if memory_snapshot and self._memories is not None:
                await self._memories.repository.apply_evidence_write(execute)
            else:
                async with self._database.immediate_session() as session:
                    await execute(session)
        except ControlCommandError:
            raise
        except IntegrityError as exc:
            return await self._recover_integrity(
                principal,
                command,
                bound_hash=bound_hash,
                operation=operation,
                capability=capability,
                target_id=target_id,
                material=material,
                exc=exc,
                started=started,
            )
        if pending is not None:
            raise ControlCommandError(pending)
        if result is None:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        return result

    async def _recover_integrity(
        self,
        principal: ControlPrincipal,
        command: ControlCommand,
        *,
        bound_hash: str,
        operation: str,
        capability: str,
        target_id: str,
        material: JsonObject,
        exc: IntegrityError,
        started: float,
    ) -> ControlResult:
        mapped = _map_integrity(exc)
        try:
            async with self._database.immediate_session() as session:
                existing = await self._receipts.load_receipt(session, principal, command)
                if existing is not None:
                    replay = await self._receipts.replay_receipt(
                        session,
                        existing,
                        bound_hash,
                        principal=principal,
                        command=command,
                        operation=operation,
                        capability=capability,
                        semantic_target_id=target_id,
                        material=material,
                        failure_target_type=failure_audit_target_type(operation),
                    )
                    if type(replay) is ControlResult:
                        return replay
                    raise ControlCommandError(replay)
                if mapped is None:
                    raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
                if mapped not in CACHEABLE_COMMAND_FAILURES:
                    raise ControlCommandError(_problem(mapped))
                problem = await self._receipts.record_failure(
                    session,
                    principal=principal,
                    command=command,
                    operation=operation,
                    capability=capability,
                    target_type=failure_audit_target_type(operation),
                    target_id=target_id,
                    payload_hash=bound_hash,
                    problem=_problem(mapped),
                    before={},
                    started=started,
                )
        except ControlCommandError:
            raise
        raise ControlCommandError(problem)

    async def _toggle_person(
        self,
        session: AsyncSession,
        target: object,
        command: ControlCommand,
        *,
        enabled: bool,
    ) -> _Success:
        person_id = _require_target(target, PersonId)
        row = await session.get(CanonicalPersonModel, person_id.text)
        if row is None:
            raise _fail(ProblemCode.NOT_FOUND)
        before = {"enabled": bool(row.enabled), "revision": int(row.revision)}
        _require_revision(int(row.revision), command.expected_revision, before=before)
        if bool(row.enabled) != enabled:
            row.enabled = enabled
            row.revision = int(row.revision) + 1
            row.updated_at = _now()
        after = {"enabled": bool(row.enabled), "revision": int(row.revision)}
        return _Success(
            resource_id=row.id,
            revision=int(row.revision),
            target_type="person",
            target_id=row.id,
            before=before,
            after=after,
            effective_state=after,
        )

    async def _toggle_space(
        self,
        session: AsyncSession,
        target: object,
        command: ControlCommand,
        *,
        enabled: bool,
    ) -> _Success:
        space_id = _require_target(target, SpaceId)
        row = await session.get(CanonicalSpaceModel, space_id.text)
        if row is None:
            raise _fail(ProblemCode.NOT_FOUND)
        before = {"enabled": bool(row.enabled), "revision": int(row.revision)}
        _require_revision(int(row.revision), command.expected_revision, before=before)
        if bool(row.enabled) != enabled:
            row.enabled = enabled
            row.revision = int(row.revision) + 1
            row.updated_at = _now()
        after = {"enabled": bool(row.enabled), "revision": int(row.revision)}
        return _Success(
            resource_id=row.id,
            revision=int(row.revision),
            target_type="space",
            target_id=row.id,
            before=before,
            after=after,
            effective_state=after,
        )

    async def _attach_person_binding(
        self,
        session: AsyncSession,
        target: object,
        command: ControlCommand,
        parsed: AttachBindingPayload | None,
    ) -> _Success:
        if parsed is None:
            raise _fail(ProblemCode.VALIDATION_ERROR)
        person_id = _require_target(target, PersonId)
        person = await session.get(CanonicalPersonModel, person_id.text)
        if person is None:
            raise _fail(ProblemCode.NOT_FOUND)
        before = {"enabled": bool(person.enabled), "revision": int(person.revision)}
        _require_revision(int(person.revision), command.expected_revision, before=before)
        payload = parsed
        await self._reject_account_attach(
            session,
            platform=payload.platform,
            external_account_id=payload.external_account_id,
            owner_id=person.id,
        )
        stamp = _now()
        binding_id = _new_uuid4()
        session.add(
            IdentityBindingModel(
                id=binding_id,
                person_id=person.id,
                platform=payload.platform,
                external_account_id=payload.external_account_id,
                display_name=payload.display_name,
                status="active",
                revision=1,
                first_seen_at=stamp,
                last_seen_at=stamp,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        person.revision = int(person.revision) + 1
        person.updated_at = stamp
        after: dict[str, JsonValue] = {
            "binding_id": binding_id,
            "person_id": person.id,
            "platform": payload.platform,
            "status": "active",
            "revision": 1,
            "owner_revision": int(person.revision),
        }
        return _Success(
            resource_id=binding_id,
            revision=1,
            target_type="identity_binding",
            target_id=binding_id,
            before=before,
            after=after,
            effective_state={
                "binding_id": binding_id,
                "person_id": person.id,
                "platform": payload.platform,
                "status": "active",
                "revision": 1,
            },
        )

    async def _attach_space_binding(
        self,
        session: AsyncSession,
        target: object,
        command: ControlCommand,
        parsed: AttachSpaceBindingPayload | None,
    ) -> _Success:
        if parsed is None:
            raise _fail(ProblemCode.VALIDATION_ERROR)
        space_id = _require_target(target, SpaceId)
        space = await session.get(CanonicalSpaceModel, space_id.text)
        if space is None:
            raise _fail(ProblemCode.NOT_FOUND)
        before = {"enabled": bool(space.enabled), "revision": int(space.revision)}
        _require_revision(int(space.revision), command.expected_revision, before=before)
        payload = parsed
        await self._reject_space_attach(
            session,
            platform=payload.platform,
            external_space_id=payload.external_space_id,
            owner_id=space.id,
        )
        stamp = _now()
        binding_id = _new_uuid4()
        session.add(
            SpaceBindingModel(
                id=binding_id,
                space_id=space.id,
                platform=payload.platform,
                external_space_id=payload.external_space_id,
                display_name=payload.display_name,
                status="active",
                revision=1,
                first_seen_at=stamp,
                last_seen_at=stamp,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        space.revision = int(space.revision) + 1
        space.updated_at = stamp
        after: dict[str, JsonValue] = {
            "binding_id": binding_id,
            "space_id": space.id,
            "platform": payload.platform,
            "status": "active",
            "revision": 1,
            "owner_revision": int(space.revision),
        }
        return _Success(
            resource_id=binding_id,
            revision=1,
            target_type="space_binding",
            target_id=binding_id,
            before=before,
            after=after,
            effective_state={
                "binding_id": binding_id,
                "space_id": space.id,
                "platform": payload.platform,
                "status": "active",
                "revision": 1,
            },
        )

    async def _register_presence(
        self,
        session: AsyncSession,
        command: ControlCommand,
        parsed: RegisterPresencePayload | None,
    ) -> _Success:
        if parsed is None:
            raise _fail(ProblemCode.VALIDATION_ERROR)
        if command.expected_revision != 0:
            raise _fail(ProblemCode.VERSION_CONFLICT)
        payload = parsed
        await self._reject_presence_register(
            session,
            platform=payload.platform,
            external_account_id=payload.external_account_id,
        )
        stamp = _now()
        presence_id = _new_uuid4()
        session.add(
            PresenceModel(
                id=presence_id,
                platform=payload.platform,
                external_account_id=payload.external_account_id,
                enabled=True,
                ingest_eligible=True,
                revision=1,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        after: dict[str, JsonValue] = {
            "presence_id": presence_id,
            "platform": payload.platform,
            "enabled": True,
            "ingest_eligible": True,
            "revision": 1,
        }
        return _Success(
            resource_id=presence_id,
            revision=1,
            target_type="presence",
            target_id=presence_id,
            before={},
            after=after,
            effective_state=after,
        )

    async def _set_presence_enabled(
        self,
        session: AsyncSession,
        target: object,
        command: ControlCommand,
        *,
        enabled: bool,
    ) -> _Success:
        presence_id = _require_target(target, PresenceId)
        row = await session.get(PresenceModel, presence_id.text)
        if row is None:
            raise _fail(ProblemCode.NOT_FOUND)
        if command.expected_revision < 1:
            raise _fail(ProblemCode.VERSION_CONFLICT)
        before = {
            "enabled": bool(row.enabled),
            "ingest_eligible": bool(row.ingest_eligible),
            "revision": int(row.revision),
        }
        _require_revision(int(row.revision), command.expected_revision, before=before)
        if bool(row.enabled) != enabled:
            row.enabled = enabled
            row.revision = int(row.revision) + 1
            row.updated_at = _now()
        after: dict[str, JsonValue] = {
            "presence_id": row.id,
            "platform": row.platform,
            "enabled": bool(row.enabled),
            "ingest_eligible": bool(row.ingest_eligible),
            "revision": int(row.revision),
        }
        return _Success(
            resource_id=row.id,
            revision=int(row.revision),
            target_type="presence",
            target_id=row.id,
            before=before,
            after=after,
            effective_state=after,
        )

    async def _set_presence_ingest(
        self,
        session: AsyncSession,
        target: object,
        command: ControlCommand,
        parsed: SetIngestPayload | None,
    ) -> _Success:
        if parsed is None:
            raise _fail(ProblemCode.VALIDATION_ERROR)
        presence_id = _require_target(target, PresenceId)
        row = await session.get(PresenceModel, presence_id.text)
        if row is None:
            raise _fail(ProblemCode.NOT_FOUND)
        if command.expected_revision < 1:
            raise _fail(ProblemCode.VERSION_CONFLICT)
        before = {
            "enabled": bool(row.enabled),
            "ingest_eligible": bool(row.ingest_eligible),
            "revision": int(row.revision),
        }
        _require_revision(int(row.revision), command.expected_revision, before=before)
        if bool(row.ingest_eligible) != parsed.ingest_eligible:
            row.ingest_eligible = parsed.ingest_eligible
            row.revision = int(row.revision) + 1
            row.updated_at = _now()
        after: dict[str, JsonValue] = {
            "presence_id": row.id,
            "platform": row.platform,
            "enabled": bool(row.enabled),
            "ingest_eligible": bool(row.ingest_eligible),
            "revision": int(row.revision),
        }
        return _Success(
            resource_id=row.id,
            revision=int(row.revision),
            target_type="presence",
            target_id=row.id,
            before=before,
            after=after,
            effective_state=after,
        )

    async def _set_route(
        self,
        session: AsyncSession,
        target: object,
        command: ControlCommand,
        parsed: SetRoutePayload | None,
    ) -> _Success:
        if parsed is None:
            raise _fail(ProblemCode.VALIDATION_ERROR)
        if parsed.kind is RouteKind.PERSON_ACTIVE:
            return await self._set_person_active_route(session, target, command, parsed)
        if parsed.kind is RouteKind.SPACE_BINDING_INGEST:
            return await self._set_ingest_route(session, target, command, parsed)
        return await self._set_space_active_route(session, target, command, parsed)

    async def _set_person_active_route(
        self,
        session: AsyncSession,
        target: object,
        command: ControlCommand,
        parsed: SetRoutePayload,
    ) -> _Success:
        person_id = _require_target(target, PersonId)
        if parsed.identity_binding_id is None or parsed.presence_id is None:
            raise _fail(ProblemCode.VALIDATION_ERROR)
        binding = await session.get(IdentityBindingModel, parsed.identity_binding_id)
        presence = await session.get(PresenceModel, parsed.presence_id)
        person = await session.get(CanonicalPersonModel, person_id.text)
        if person is None or binding is None or presence is None:
            raise _fail(ProblemCode.NOT_FOUND)
        if binding.person_id != person.id or binding.platform != presence.platform:
            raise _fail(ProblemCode.ROUTE_AMBIGUOUS)
        if not bool(person.enabled) or binding.status != "active" or not bool(presence.enabled):
            raise _fail(ProblemCode.PRECONDITION_FAILED)
        row = await session.get(PersonActiveRouteModel, person.id)
        stamp = _now()
        if row is None:
            if command.expected_revision != 0:
                raise _fail(ProblemCode.VERSION_CONFLICT)
            session.add(
                PersonActiveRouteModel(
                    person_id=person.id,
                    identity_binding_id=binding.id,
                    presence_id=presence.id,
                    route_generation=1,
                    paused=parsed.paused,
                    revision=1,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            revision = 1
            generation = 1
            paused = bool(parsed.paused)
            before: dict[str, JsonValue] = {}
        else:
            before = {
                "paused": bool(row.paused),
                "revision": int(row.revision),
                "route_generation": int(row.route_generation),
            }
            _require_revision(int(row.revision), command.expected_revision, before=before)
            unchanged = (
                row.identity_binding_id == binding.id
                and row.presence_id == presence.id
                and bool(row.paused) is bool(parsed.paused)
            )
            if not unchanged:
                row.identity_binding_id = binding.id
                row.presence_id = presence.id
                row.paused = bool(parsed.paused)
                row.revision = int(row.revision) + 1
                row.route_generation = int(row.route_generation) + 1
                row.updated_at = stamp
            revision = int(row.revision)
            generation = int(row.route_generation)
            paused = bool(row.paused)
        reference = classify_route_reference(
            expected_owner_id=person.id,
            actual_owner_id=binding.person_id,
            binding_platform=binding.platform,
            presence_platform=presence.platform,
        )
        after = _route_state(
            kind=RouteKind.PERSON_ACTIVE,
            owner_id=person.id,
            binding_id=binding.id,
            presence_id=presence.id,
            paused=paused,
            revision=revision,
            route_generation=generation,
            reference_state=reference,
        )
        return _Success(
            resource_id=person.id,
            revision=revision,
            target_type="person_active_route",
            target_id=person.id,
            before=before,
            after=after,
            effective_state=after,
        )

    async def _set_ingest_route(
        self,
        session: AsyncSession,
        target: object,
        command: ControlCommand,
        parsed: SetRoutePayload,
    ) -> _Success:
        binding_id = _require_target(target, SpaceBindingId)
        if parsed.ingest_presence_id is None:
            raise _fail(ProblemCode.VALIDATION_ERROR)
        binding = await session.get(SpaceBindingModel, binding_id.text)
        presence = await session.get(PresenceModel, parsed.ingest_presence_id)
        if binding is None or presence is None:
            raise _fail(ProblemCode.NOT_FOUND)
        if binding.platform != presence.platform:
            raise _fail(ProblemCode.ROUTE_AMBIGUOUS)
        if (
            binding.status != "active"
            or not bool(presence.enabled)
            or not bool(presence.ingest_eligible)
        ):
            raise _fail(ProblemCode.PRECONDITION_FAILED)
        row = await session.get(SpaceBindingIngestRouteModel, binding.id)
        stamp = _now()
        if row is None:
            if command.expected_revision != 0:
                raise _fail(ProblemCode.VERSION_CONFLICT)
            session.add(
                SpaceBindingIngestRouteModel(
                    space_binding_id=binding.id,
                    ingest_presence_id=presence.id,
                    route_generation=1,
                    paused=parsed.paused,
                    revision=1,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            revision = 1
            generation = 1
            paused = bool(parsed.paused)
            before: dict[str, JsonValue] = {}
        else:
            before = {
                "paused": bool(row.paused),
                "revision": int(row.revision),
                "route_generation": int(row.route_generation),
            }
            _require_revision(int(row.revision), command.expected_revision, before=before)
            unchanged = row.ingest_presence_id == presence.id and bool(row.paused) is bool(
                parsed.paused
            )
            if not unchanged:
                row.ingest_presence_id = presence.id
                row.paused = bool(parsed.paused)
                row.revision = int(row.revision) + 1
                row.route_generation = int(row.route_generation) + 1
                row.updated_at = stamp
            revision = int(row.revision)
            generation = int(row.route_generation)
            paused = bool(row.paused)
        reference = classify_route_reference(
            expected_owner_id=None,
            actual_owner_id=binding.space_id,
            binding_platform=binding.platform,
            presence_platform=presence.platform,
        )
        after = _route_state(
            kind=RouteKind.SPACE_BINDING_INGEST,
            owner_id=binding.id,
            binding_id=binding.id,
            presence_id=presence.id,
            paused=paused,
            revision=revision,
            route_generation=generation,
            reference_state=reference,
        )
        return _Success(
            resource_id=binding.id,
            revision=revision,
            target_type="space_binding_ingest_route",
            target_id=binding.id,
            before=before,
            after=after,
            effective_state=after,
        )

    async def _set_space_active_route(
        self,
        session: AsyncSession,
        target: object,
        command: ControlCommand,
        parsed: SetRoutePayload,
    ) -> _Success:
        space_id = _require_target(target, SpaceId)
        if parsed.space_binding_id is None or parsed.presence_id is None:
            raise _fail(ProblemCode.VALIDATION_ERROR)
        binding = await session.get(SpaceBindingModel, parsed.space_binding_id)
        presence = await session.get(PresenceModel, parsed.presence_id)
        space = await session.get(CanonicalSpaceModel, space_id.text)
        if space is None or binding is None or presence is None:
            raise _fail(ProblemCode.NOT_FOUND)
        if binding.space_id != space.id or binding.platform != presence.platform:
            raise _fail(ProblemCode.ROUTE_AMBIGUOUS)
        if not bool(space.enabled) or binding.status != "active" or not bool(presence.enabled):
            raise _fail(ProblemCode.PRECONDITION_FAILED)
        row = await session.get(SpaceActiveRouteModel, space.id)
        stamp = _now()
        if row is None:
            if command.expected_revision != 0:
                raise _fail(ProblemCode.VERSION_CONFLICT)
            session.add(
                SpaceActiveRouteModel(
                    space_id=space.id,
                    space_binding_id=binding.id,
                    presence_id=presence.id,
                    route_generation=1,
                    paused=parsed.paused,
                    revision=1,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            revision = 1
            generation = 1
            paused = bool(parsed.paused)
            before: dict[str, JsonValue] = {}
        else:
            before = {
                "paused": bool(row.paused),
                "revision": int(row.revision),
                "route_generation": int(row.route_generation),
            }
            _require_revision(int(row.revision), command.expected_revision, before=before)
            unchanged = (
                row.space_binding_id == binding.id
                and row.presence_id == presence.id
                and bool(row.paused) is bool(parsed.paused)
            )
            if not unchanged:
                row.space_binding_id = binding.id
                row.presence_id = presence.id
                row.paused = bool(parsed.paused)
                row.revision = int(row.revision) + 1
                row.route_generation = int(row.route_generation) + 1
                row.updated_at = stamp
            revision = int(row.revision)
            generation = int(row.route_generation)
            paused = bool(row.paused)
        reference = classify_route_reference(
            expected_owner_id=space.id,
            actual_owner_id=binding.space_id,
            binding_platform=binding.platform,
            presence_platform=presence.platform,
        )
        after = _route_state(
            kind=RouteKind.SPACE_ACTIVE,
            owner_id=space.id,
            binding_id=binding.id,
            presence_id=presence.id,
            paused=paused,
            revision=revision,
            route_generation=generation,
            reference_state=reference,
        )
        return _Success(
            resource_id=space.id,
            revision=revision,
            target_type="space_active_route",
            target_id=space.id,
            before=before,
            after=after,
            effective_state=after,
        )

    async def _pause_resume_route(
        self,
        session: AsyncSession,
        target: object,
        command: ControlCommand,
        parsed: RouteActionPayload | None,
        *,
        paused: bool,
    ) -> _Success:
        if parsed is None:
            raise _fail(ProblemCode.VALIDATION_ERROR)
        if parsed.kind is RouteKind.PERSON_ACTIVE:
            return await self._pause_person_active(session, target, command, paused=paused)
        if parsed.kind is RouteKind.SPACE_BINDING_INGEST:
            return await self._pause_ingest(session, target, command, paused=paused)
        return await self._pause_space_active(session, target, command, paused=paused)

    async def _pause_person_active(
        self,
        session: AsyncSession,
        target: object,
        command: ControlCommand,
        *,
        paused: bool,
    ) -> _Success:
        person_id = _require_target(target, PersonId)
        row = await session.get(PersonActiveRouteModel, person_id.text)
        if row is None:
            raise _fail(ProblemCode.NOT_FOUND)
        person = await session.get(CanonicalPersonModel, row.person_id)
        binding = await session.get(IdentityBindingModel, row.identity_binding_id)
        presence = await session.get(PresenceModel, row.presence_id)
        if person is None or binding is None or presence is None:
            raise _fail(ProblemCode.STATE_MISMATCH)
        if binding.person_id != person.id or binding.platform != presence.platform:
            raise _fail(ProblemCode.ROUTE_AMBIGUOUS)
        if not paused:
            if not bool(person.enabled) or binding.status != "active" or not bool(presence.enabled):
                raise _fail(ProblemCode.PRECONDITION_FAILED)
        before = {
            "paused": bool(row.paused),
            "revision": int(row.revision),
            "route_generation": int(row.route_generation),
        }
        _require_revision(int(row.revision), command.expected_revision, before=before)
        if bool(row.paused) is not paused:
            row.paused = paused
            row.revision = int(row.revision) + 1
            row.route_generation = int(row.route_generation) + 1
            row.updated_at = _now()
        reference = classify_route_reference(
            expected_owner_id=person.id,
            actual_owner_id=binding.person_id,
            binding_platform=binding.platform,
            presence_platform=presence.platform,
        )
        after = _route_state(
            kind=RouteKind.PERSON_ACTIVE,
            owner_id=person.id,
            binding_id=binding.id,
            presence_id=presence.id,
            paused=bool(row.paused),
            revision=int(row.revision),
            route_generation=int(row.route_generation),
            reference_state=reference,
        )
        return _Success(
            resource_id=person.id,
            revision=int(row.revision),
            target_type="person_active_route",
            target_id=person.id,
            before=before,
            after=after,
            effective_state=after,
        )

    async def _pause_ingest(
        self,
        session: AsyncSession,
        target: object,
        command: ControlCommand,
        *,
        paused: bool,
    ) -> _Success:
        binding_id = _require_target(target, SpaceBindingId)
        row = await session.get(SpaceBindingIngestRouteModel, binding_id.text)
        if row is None:
            raise _fail(ProblemCode.NOT_FOUND)
        binding = await session.get(SpaceBindingModel, row.space_binding_id)
        presence = await session.get(PresenceModel, row.ingest_presence_id)
        if binding is None or presence is None:
            raise _fail(ProblemCode.STATE_MISMATCH)
        if binding.platform != presence.platform:
            raise _fail(ProblemCode.ROUTE_AMBIGUOUS)
        if not paused and (
            binding.status != "active"
            or not bool(presence.enabled)
            or not bool(presence.ingest_eligible)
        ):
            raise _fail(ProblemCode.PRECONDITION_FAILED)
        before = {
            "paused": bool(row.paused),
            "revision": int(row.revision),
            "route_generation": int(row.route_generation),
        }
        _require_revision(int(row.revision), command.expected_revision, before=before)
        if bool(row.paused) is not paused:
            row.paused = paused
            row.revision = int(row.revision) + 1
            row.route_generation = int(row.route_generation) + 1
            row.updated_at = _now()
        reference = classify_route_reference(
            expected_owner_id=None,
            actual_owner_id=binding.space_id,
            binding_platform=binding.platform,
            presence_platform=presence.platform,
        )
        after = _route_state(
            kind=RouteKind.SPACE_BINDING_INGEST,
            owner_id=binding.id,
            binding_id=binding.id,
            presence_id=presence.id,
            paused=bool(row.paused),
            revision=int(row.revision),
            route_generation=int(row.route_generation),
            reference_state=reference,
        )
        return _Success(
            resource_id=binding.id,
            revision=int(row.revision),
            target_type="space_binding_ingest_route",
            target_id=binding.id,
            before=before,
            after=after,
            effective_state=after,
        )

    async def _pause_space_active(
        self,
        session: AsyncSession,
        target: object,
        command: ControlCommand,
        *,
        paused: bool,
    ) -> _Success:
        space_id = _require_target(target, SpaceId)
        row = await session.get(SpaceActiveRouteModel, space_id.text)
        if row is None:
            raise _fail(ProblemCode.NOT_FOUND)
        space = await session.get(CanonicalSpaceModel, row.space_id)
        binding = await session.get(SpaceBindingModel, row.space_binding_id)
        presence = await session.get(PresenceModel, row.presence_id)
        if space is None or binding is None or presence is None:
            raise _fail(ProblemCode.STATE_MISMATCH)
        if binding.space_id != space.id or binding.platform != presence.platform:
            raise _fail(ProblemCode.ROUTE_AMBIGUOUS)
        if not paused:
            if not bool(space.enabled) or binding.status != "active" or not bool(presence.enabled):
                raise _fail(ProblemCode.PRECONDITION_FAILED)
        before = {
            "paused": bool(row.paused),
            "revision": int(row.revision),
            "route_generation": int(row.route_generation),
        }
        _require_revision(int(row.revision), command.expected_revision, before=before)
        if bool(row.paused) is not paused:
            row.paused = paused
            row.revision = int(row.revision) + 1
            row.route_generation = int(row.route_generation) + 1
            row.updated_at = _now()
        reference = classify_route_reference(
            expected_owner_id=space.id,
            actual_owner_id=binding.space_id,
            binding_platform=binding.platform,
            presence_platform=presence.platform,
        )
        after = _route_state(
            kind=RouteKind.SPACE_ACTIVE,
            owner_id=space.id,
            binding_id=binding.id,
            presence_id=presence.id,
            paused=bool(row.paused),
            revision=int(row.revision),
            route_generation=int(row.route_generation),
            reference_state=reference,
        )
        return _Success(
            resource_id=space.id,
            revision=int(row.revision),
            target_type="space_active_route",
            target_id=space.id,
            before=before,
            after=after,
            effective_state=after,
        )

    async def _reject_account_attach(
        self,
        session: AsyncSession,
        *,
        platform: str,
        external_account_id: str,
        owner_id: str,
    ) -> None:
        binding = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == platform,
                IdentityBindingModel.external_account_id == external_account_id,
            )
        )
        if binding is not None:
            if binding.person_id == owner_id:
                raise _fail(ProblemCode.PRECONDITION_FAILED)
            raise _fail(ProblemCode.BINDING_AMBIGUOUS)
        presence = await session.scalar(
            select(PresenceModel).where(
                PresenceModel.platform == platform,
                PresenceModel.external_account_id == external_account_id,
            )
        )
        if presence is not None:
            raise _fail(ProblemCode.PRECONDITION_FAILED)

    async def _reject_space_attach(
        self,
        session: AsyncSession,
        *,
        platform: str,
        external_space_id: str,
        owner_id: str,
    ) -> None:
        binding = await session.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.platform == platform,
                SpaceBindingModel.external_space_id == external_space_id,
            )
        )
        if binding is not None:
            if binding.space_id == owner_id:
                raise _fail(ProblemCode.PRECONDITION_FAILED)
            raise _fail(ProblemCode.BINDING_AMBIGUOUS)

    async def _reject_presence_register(
        self,
        session: AsyncSession,
        *,
        platform: str,
        external_account_id: str,
    ) -> None:
        presence = await session.scalar(
            select(PresenceModel).where(
                PresenceModel.platform == platform,
                PresenceModel.external_account_id == external_account_id,
            )
        )
        binding = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == platform,
                IdentityBindingModel.external_account_id == external_account_id,
            )
        )
        if presence is not None and binding is not None:
            raise _fail(ProblemCode.BINDING_AMBIGUOUS)
        if binding is not None:
            raise _fail(ProblemCode.PRECONDITION_FAILED)
        if presence is not None:
            raise _fail(ProblemCode.PRECONDITION_FAILED)


def _empty_payload(command: ControlCommand) -> tuple[Problem | None, JsonObject]:
    try:
        require_empty_payload(command.payload)
    except ControlCommandError as exc:
        return exc.problem, command.payload
    return None, {}


def _try_parse[T](
    command: ControlCommand,
    parser: Callable[[object], T],
) -> tuple[T | None, JsonObject, Problem | None]:
    try:
        parsed = parser(command.payload)
    except ControlCommandError as exc:
        return None, command.payload, exc.problem
    material = getattr(parsed, "material", None)
    if not callable(material):
        return None, command.payload, _problem(ProblemCode.VALIDATION_ERROR)
    return parsed, material(), None


def _reject_populated_merge(*, other_owner_exists: bool) -> None:
    """C11 has no merge API. Two populated owners fail closed."""

    if other_owner_exists:
        raise _fail(ProblemCode.POPULATED_MERGE_FORBIDDEN)


def _route_state(
    *,
    kind: RouteKind,
    owner_id: str,
    binding_id: str,
    presence_id: str,
    paused: bool,
    revision: int,
    route_generation: int,
    reference_state: RouteReferenceState,
) -> dict[str, JsonValue]:
    return {
        "kind": kind.value,
        "owner_id": owner_id,
        "binding_id": binding_id,
        "presence_id": presence_id,
        "paused": paused,
        "revision": revision,
        "route_generation": route_generation,
        "reference_state": reference_state.value,
    }
