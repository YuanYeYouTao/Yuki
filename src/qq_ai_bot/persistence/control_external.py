"""Control intent, external execution, and completion; never retry unknown effects."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING

from qq_ai_bot.admin.authority.command_types import (
    ControlCommandError,
    ManagementActionPayload,
    bind_command_hash,
    failure_audit_target_type,
)
from qq_ai_bot.admin.authority.commands import ControlCommand, ControlResult
from qq_ai_bot.admin.authority.json_types import JsonObject
from qq_ai_bot.admin.authority.principal import ControlPrincipal
from qq_ai_bot.admin.authority.problems import Problem, ProblemCode
from qq_ai_bot.persistence.command_receipts import pending_result
from qq_ai_bot.persistence.control_management import ManagementFailure, ManagementUnavailable
from qq_ai_bot.persistence.models import AdminOperationEventModel
from qq_ai_bot.plugin_host.manager import PluginManagementRejected, PluginRevisionConflict

if TYPE_CHECKING:
    from qq_ai_bot.persistence.control_command import ControlCommandAdapter


class ExternalControlExecutor:
    """No second worker or durable payload. Caller executes its reserved request once."""

    def __init__(self, adapter: ControlCommandAdapter) -> None:
        self._adapter = adapter

    async def execute(
        self,
        principal: ControlPrincipal,
        command: ControlCommand,
        *,
        operation: str,
        capability: str,
        parsed: ManagementActionPayload,
        material: JsonObject,
    ) -> ControlResult:
        adapter = self._adapter
        target = parsed.resource_id
        bound = bind_command_hash(
            operation=operation,
            target_id=target,
            expected_revision=command.expected_revision,
            payload=material,
        )
        started = monotonic()
        problem: Problem | None = None
        prepared: object = None
        preparation_error: ManagementFailure | ManagementUnavailable | None = None
        try:
            prepared = adapter._management.prepare_external(command, operation, parsed)
        except (ManagementFailure, ManagementUnavailable) as exc:
            # Replay takes precedence over today's admission schema and size limits.
            preparation_error = exc
        async with adapter._database.immediate_session() as session:
            existing = await adapter._receipts.load_receipt(session, principal, command)
            if existing is not None:
                if existing.payload_hash != bound:
                    raise ControlCommandError(Problem(ProblemCode.IDEMPOTENCY_CONFLICT))
                if existing.status in {"running", "unknown"}:
                    audit = await session.get(AdminOperationEventModel, existing.audit_id)
                    if (
                        audit is None
                        or audit.target_id != target
                        or existing.operation_kind != "control"
                        or existing.operation_ref
                        != f"control:{principal.principal_id.text}:{command.request_id.text}"
                    ):
                        raise ControlCommandError(Problem(ProblemCode.STATE_MISMATCH))
                    adapter._receipts._require_audit_correlation(
                        audit,
                        principal=principal,
                        command=command,
                        operation=operation,
                        capability=capability,
                    )
                    return pending_result(existing, audit)
                replay = await adapter._receipts.replay_receipt(
                    session,
                    existing,
                    bound,
                    principal=principal,
                    command=command,
                    operation=operation,
                    capability=capability,
                    semantic_target_id=target,
                    material=material,
                    failure_target_type=failure_audit_target_type(operation),
                )
                if type(replay) is ControlResult:
                    return replay
                raise ControlCommandError(replay)
            await adapter._receipts.require_external_available(session, operation, target)
            try:
                if preparation_error is not None:
                    raise preparation_error
                await adapter._management.validate_external(session, command, operation, parsed)
            except ManagementUnavailable as exc:
                raise ControlCommandError(Problem(ProblemCode.OPERATION_UNAVAILABLE)) from exc
            except ManagementFailure as exc:
                problem = await adapter._receipts.record_failure(
                    session,
                    principal=principal,
                    command=command,
                    operation=operation,
                    capability=capability,
                    target_type=failure_audit_target_type(operation),
                    target_id=target,
                    payload_hash=bound,
                    problem=Problem(exc.code),
                    before={},
                    started=started,
                )
            else:
                await adapter._receipts.reserve_external(
                    session,
                    principal,
                    command,
                    operation=operation,
                    capability=capability,
                    target_type=failure_audit_target_type(operation),
                    target=target,
                    payload_hash=bound,
                    action=parsed.action,
                )
        if problem is not None:
            raise ControlCommandError(problem)
        # No session survives this boundary. Domain managers own their short transactions.
        try:
            mutation = await adapter._management.execute_external(
                principal, command, operation, parsed, prepared=prepared
            )
            success = adapter._management_success(
                mutation.resource_id,
                mutation.revision,
                mutation.status,
                operation=operation,
                op_ref=mutation.operation,
            )
            async with adapter._database.immediate_session() as session:
                reserved = await adapter._receipts.load_receipt(session, principal, command)
                if reserved is None or reserved.status != "running":
                    raise RuntimeError("control intent no longer owned")
                return await adapter._receipts.record_success(
                    session,
                    principal=principal,
                    command=command,
                    operation=operation,
                    capability=capability,
                    payload_hash=bound,
                    success=success,
                    semantic_target_id=target,
                    material=material,
                    started=started,
                    existing_receipt=reserved,
                )
        except BaseException as exc:
            if isinstance(
                exc,
                (
                    PluginRevisionConflict,
                    PluginManagementRejected,
                    ManagementFailure,
                ),
            ):
                async with adapter._database.immediate_session() as session:
                    reserved = await adapter._receipts.load_receipt(session, principal, command)
                    failure = await adapter._receipts.record_failure(
                        session,
                        principal=principal,
                        command=command,
                        operation=operation,
                        capability=capability,
                        payload_hash=bound,
                        target_type=failure_audit_target_type(operation),
                        target_id=target,
                        problem=Problem(
                            exc.code
                            if isinstance(exc, ManagementFailure)
                            else (
                                ProblemCode.VERSION_CONFLICT
                                if isinstance(exc, PluginRevisionConflict)
                                else ProblemCode.PRECONDITION_FAILED
                            )
                        ),
                        before={},
                        started=started,
                        existing_receipt=reserved,
                    )
                raise ControlCommandError(failure) from None
            interrupted = isinstance(exc, asyncio.CancelledError)

            # Includes a final commit failure: the domain effect may already have happened.
            result = await asyncio.shield(
                adapter._receipts.mark_unknown(
                    principal,
                    command,
                    operation=operation,
                    capability=capability,
                    interrupted=interrupted,
                    started=started,
                )
            )
            if not isinstance(exc, Exception):
                raise
            return result
