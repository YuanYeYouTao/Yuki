"""Control intent, external execution, and completion; never retry unknown effects."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING

from sqlalchemy import case, select, update

from qq_ai_bot.control_plane.command_types import (
    ControlCommandError,
    ManagementActionPayload,
    bind_command_hash,
    failure_audit_target_type,
)
from qq_ai_bot.control_plane.commands import ControlCommand, ControlResult
from qq_ai_bot.control_plane.json_types import JsonObject
from qq_ai_bot.control_plane.operations import OperationRef, OperationStatus, StateEpoch
from qq_ai_bot.control_plane.principal import ControlPrincipal
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.persistence.control_management import ManagementFailure, ManagementUnavailable
from qq_ai_bot.persistence.models import AdminOperationEventModel
from qq_ai_bot.persistence.unit_of_work import next_updated_at
from qq_ai_bot.plugin_host.manager import PluginManagementRejected, PluginRevisionConflict

if TYPE_CHECKING:
    from qq_ai_bot.persistence.control_command import ControlCommandAdapter


def aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def control_operation(row: ControlCommandReceiptModel) -> OperationRef:
    statuses = {
        "running": OperationStatus.RUNNING,
        "unknown": OperationStatus.UNKNOWN,
        "succeeded": OperationStatus.SUCCEEDED,
        "failed": OperationStatus.FAILED,
    }
    return OperationRef(
        operation_id=f"control:{row.principal_id}:{row.request_id}",
        status=statuses[row.status],
        progress=1.0 if row.status in {"succeeded", "failed"} else None,
        state_epoch=StateEpoch.V2,
        error_category=row.problem_code,
        created_at=aware(row.created_at),
        updated_at=aware(row.updated_at),
    )


def pending_result(
    row: ControlCommandReceiptModel, audit: AdminOperationEventModel
) -> ControlResult:
    return ControlResult(
        success=False,
        resource_id=audit.target_id,
        revision=0,
        audit_id=str(audit.id),
        effective_state={"resource": audit.target_id, "status": row.status},
        operation=control_operation(row),
    )


class ExternalControlExecutor:
    """No second worker or durable payload. Caller executes its reserved request once."""

    def __init__(self, adapter: ControlCommandAdapter) -> None:
        self._adapter = adapter

    async def recover_interrupted(self) -> int:
        now = datetime.now(UTC)
        async with self._adapter._database.immediate_session() as session:
            result = await session.execute(
                update(ControlCommandReceiptModel)
                .where(
                    ControlCommandReceiptModel.status == "running",
                )
                .values(
                    status="unknown",
                    problem_code="process_restart",
                    updated_at=case(
                        (
                            ControlCommandReceiptModel.updated_at > now,
                            ControlCommandReceiptModel.updated_at,
                        ),
                        else_=now,
                    ),
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)

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
            existing = await adapter._load_receipt(session, principal, command)
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
                    adapter._require_audit_correlation(
                        audit,
                        principal=principal,
                        command=command,
                        operation=operation,
                        capability=capability,
                    )
                    return pending_result(existing, audit)
                replay = await adapter._replay_receipt(
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
            # Reserve the resource, not just the request: two opposite actions cannot race.
            busy = await session.scalar(
                select(ControlCommandReceiptModel.id)
                .join(
                    AdminOperationEventModel,
                    AdminOperationEventModel.id == ControlCommandReceiptModel.audit_id,
                )
                .where(
                    ControlCommandReceiptModel.status.in_(("running", "unknown")),
                    AdminOperationEventModel.operation == operation,
                    AdminOperationEventModel.target_id == target,
                )
                .limit(1)
            )
            if busy is not None:
                raise ControlCommandError(Problem(ProblemCode.PRECONDITION_FAILED))
            try:
                if preparation_error is not None:
                    raise preparation_error
                await adapter._management.validate_external(session, command, operation, parsed)
            except ManagementUnavailable as exc:
                raise ControlCommandError(Problem(ProblemCode.OPERATION_UNAVAILABLE)) from exc
            except ManagementFailure as exc:
                problem = await adapter._record_failure(
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
                now = datetime.now(UTC)
                audit = AdminOperationEventModel(
                    actor_user_id=principal.principal_id.text,
                    actor_principal_kind="control",
                    actor_principal_id=principal.principal_id.text,
                    control_request_id=command.request_id.text,
                    trigger_message_id="",
                    conversation_key="",
                    capability=capability,
                    operation=operation,
                    target_type=failure_audit_target_type(operation),
                    target_id=target,
                    before_json="{}",
                    after_json=json.dumps({"status": "running", "action": parsed.action}),
                    success=False,
                    error_category="in_progress",
                    duration_seconds=0,
                    created_at=now,
                )
                session.add(audit)
                await session.flush()
                session.add(
                    ControlCommandReceiptModel(
                        principal_id=principal.principal_id.text,
                        request_id=command.request_id.text,
                        payload_hash=bound,
                        status="running",
                        audit_id=audit.id,
                        operation_kind="control",
                        operation_ref=f"control:{principal.principal_id.text}:{command.request_id.text}",
                        created_at=now,
                        updated_at=now,
                    )
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
                reserved = await adapter._load_receipt(session, principal, command)
                if reserved is None or reserved.status != "running":
                    raise RuntimeError("control intent no longer owned")
                return await adapter._record_success(
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
                    reserved = await adapter._load_receipt(session, principal, command)
                    failure = await adapter._record_failure(
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
            async def mark_unknown() -> ControlResult:
                async with adapter._database.immediate_session() as session:
                    row = await adapter._load_receipt(session, principal, command)
                    if row is None or row.status != "running":
                        raise ControlCommandError(Problem(ProblemCode.STATE_MISMATCH))
                    row.status = "unknown"
                    row.problem_code = "execution_interrupted" if interrupted else "effect_unknown"
                    row.updated_at = next_updated_at(row.updated_at)
                    audit = await session.get(AdminOperationEventModel, row.audit_id)
                    if audit is None:
                        raise ControlCommandError(Problem(ProblemCode.STATE_MISMATCH))
                    event = AdminOperationEventModel(
                        actor_user_id=principal.principal_id.text,
                        actor_principal_kind="control",
                        actor_principal_id=principal.principal_id.text,
                        control_request_id=command.request_id.text,
                        trigger_message_id="",
                        conversation_key="",
                        capability=capability,
                        operation=operation,
                        target_type=audit.target_type,
                        target_id=audit.target_id,
                        before_json="{}",
                        after_json=json.dumps({"status": "unknown"}),
                        success=False,
                        error_category=row.problem_code,
                        duration_seconds=max(0, monotonic() - started),
                        created_at=aware(row.updated_at),
                    )
                    session.add(event)
                    await session.flush()
                    row.audit_id = event.id
                    return pending_result(row, event)

            result = await asyncio.shield(mark_unknown())
            if not isinstance(exc, Exception):
                raise
            return result
