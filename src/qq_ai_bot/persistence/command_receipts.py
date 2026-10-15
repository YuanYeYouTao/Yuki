"""Original command receipts, audit correlation, and interrupted-intent recovery.

The existing receipt table and operation IDs remain the durable facts. This
repository does not execute domain commands or replay external effects.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.authority.command_types import (
    CACHEABLE_COMMAND_FAILURES,
    CommandOperation,
    ControlCommandError,
    project_replayed_result,
    require_cacheable_problem,
    require_receipt_operation_pair,
    success_audit_target_type,
    validate_failure_audit_after,
    validate_failure_audit_before,
    validate_success_audit_after,
    validate_success_audit_before,
)
from qq_ai_bot.admin.authority.commands import ControlCommand, ControlResult
from qq_ai_bot.admin.authority.json_types import JsonValue
from qq_ai_bot.admin.authority.operations import (
    OperationKind,
    OperationRef,
    OperationStatus,
    StateEpoch,
    _sanitize_error_category,
)
from qq_ai_bot.admin.authority.principal import ControlPrincipal
from qq_ai_bot.admin.authority.problems import Problem, ProblemCode
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.domain.identity import PrincipalId, RequestId
from qq_ai_bot.memory.dream.models import DreamRun
from qq_ai_bot.memory.rebuild.models import MemoryRebuildRun
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import AdminOperationEventModel
from qq_ai_bot.persistence.unit_of_work import next_updated_at


def _now() -> datetime:
    return datetime.now(UTC)


def _problem(code: ProblemCode) -> Problem:
    return Problem(code)


def _dump_json(value: Mapping[str, JsonValue]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class CommandSuccess:
    resource_id: str
    revision: int
    target_type: str
    target_id: str
    before: Mapping[str, JsonValue]
    after: Mapping[str, JsonValue]
    effective_state: Mapping[str, JsonValue]
    operation: OperationRef | None = None


def _duration(started: float) -> float:
    elapsed = monotonic() - started
    if elapsed < 0:
        return 0.0
    return elapsed


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


def _op_ref(
    operation_id: str,
    status: OperationStatus,
    *,
    created_at: datetime,
    updated_at: datetime,
    error_category: str | None = None,
) -> OperationRef:
    if status is OperationStatus.FAILED:
        try:
            error_category = _sanitize_error_category(error_category)
        except (TypeError, ValueError):
            error_category = "operation_failed"
    else:
        # Earlier diagnostics remain in the domain record, not the current outcome.
        error_category = None
    return OperationRef(
        operation_id=operation_id,
        status=status,
        progress=1.0
        if status in {OperationStatus.SUCCEEDED, OperationStatus.FAILED, OperationStatus.CANCELLED}
        else None,
        state_epoch=StateEpoch.V2,
        error_category=error_category,
        created_at=created_at,
        updated_at=updated_at,
    )


def _rebuild_status(value: str) -> OperationStatus:
    mapping = {
        "planned": OperationStatus.QUEUED,
        "extracting": OperationStatus.RUNNING,
        "extraction_paused": OperationStatus.BLOCKED,
        "review": OperationStatus.WAITING,
        "committing": OperationStatus.RUNNING,
        "commit_paused": OperationStatus.BLOCKED,
        "completed": OperationStatus.SUCCEEDED,
        "cancelled": OperationStatus.CANCELLED,
        "failed": OperationStatus.FAILED,
    }
    return mapping[value]


def _dream_status(value: str) -> OperationStatus:
    mapping = {
        "planned": OperationStatus.QUEUED,
        "running": OperationStatus.RUNNING,
        "partial_failed": OperationStatus.FAILED,
        "completed": OperationStatus.SUCCEEDED,
        "cancelled": OperationStatus.CANCELLED,
        "rolling_back": OperationStatus.RUNNING,
        "rolled_back": OperationStatus.CANCELLED,
    }
    return mapping[value]


def rebuild_operation(run: MemoryRebuildRun) -> OperationRef:
    return run_operation(
        OperationKind.REBUILD,
        run.public_id,
        run.status.value,
        created_at=run.created_at,
        updated_at=run.updated_at,
        error_category=run.error_category,
    )


def dream_operation(run: DreamRun) -> OperationRef:
    return run_operation(
        OperationKind.DREAM,
        run.public_id,
        run.status.value,
        created_at=run.created_at,
        updated_at=run.updated_at,
        error_category=run.error_category,
    )


def run_operation(
    kind: OperationKind,
    public_id: str,
    status: str,
    *,
    created_at: datetime,
    updated_at: datetime,
    error_category: str | None,
) -> OperationRef:
    return _op_ref(
        f"{kind.value}:{public_id}",
        _rebuild_status(status) if kind is OperationKind.REBUILD else _dream_status(status),
        created_at=created_at.replace(tzinfo=UTC)
        if created_at.tzinfo is None
        else created_at.astimezone(UTC),
        updated_at=updated_at.replace(tzinfo=UTC)
        if updated_at.tzinfo is None
        else updated_at.astimezone(UTC),
        error_category=error_category
        or ("rebuild_failed" if kind is OperationKind.REBUILD else "partial_failed"),
    )


class CommandReceiptRepository:
    """Persist and read command outcomes in the caller's original transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._after_audit_flush: Callable[[], None] | None = None

    async def record_personal_command(
        self,
        session: AsyncSession,
        *,
        principal_id: str,
        principal_kind: str,
        person_id: str,
        request_id: str,
        operation: str,
        operation_ref: str,
        payload_hash: str,
        resource_id: str,
        result: Mapping[str, JsonValue],
        trigger_message_id: str = "",
        conversation_key: str = "",
        problem_code: str | None = None,
    ) -> ControlCommandReceiptModel:
        """Write a personal command and its audit in the caller's short transaction.

        Authority is resolved by the caller. Credentials never enter this result;
        the original table remains the only command receipt store.
        """
        now = _now()
        audit = AdminOperationEventModel(
            actor_user_id=person_id,
            actor_principal_kind=principal_kind,
            actor_principal_id=principal_id,
            control_request_id=request_id,
            trigger_message_id=trigger_message_id,
            conversation_key=conversation_key,
            capability=operation,
            operation=operation,
            target_type="person",
            target_id=person_id,
            before_json="{}",
            after_json=_dump_json(result),
            success=problem_code is None,
            error_category=problem_code,
            duration_seconds=0,
            created_at=now,
        )
        session.add(audit)
        await session.flush()
        receipt = ControlCommandReceiptModel(
            principal_id=principal_id,
            request_id=request_id,
            payload_hash=payload_hash,
            status="failed" if problem_code else "succeeded",
            problem_code=problem_code,
            audit_id=audit.id,
            operation_kind=operation,
            operation_ref=operation_ref,
            result_resource_id=None if problem_code else resource_id,
            effective_state_json=None if problem_code else _dump_json(result),
            created_at=now,
            updated_at=now,
        )
        session.add(receipt)
        await session.flush()
        return receipt

    async def recover_interrupted_controls(self) -> int:
        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
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

    async def require_external_available(
        self, session: AsyncSession, operation: str, target: str
    ) -> None:
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

    async def reserve_external(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
        *,
        operation: str,
        capability: str,
        target_type: str,
        target: str,
        payload_hash: str,
        action: str,
    ) -> None:
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
            target_type=target_type,
            target_id=target,
            before_json="{}",
            after_json=json.dumps({"status": "running", "action": action}),
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
                payload_hash=payload_hash,
                status="running",
                audit_id=audit.id,
                operation_kind="control",
                operation_ref=f"control:{principal.principal_id.text}:{command.request_id.text}",
                created_at=now,
                updated_at=now,
            )
        )

    async def mark_unknown(
        self,
        principal: ControlPrincipal,
        command: ControlCommand,
        *,
        operation: str,
        capability: str,
        interrupted: bool,
        started: float,
    ) -> ControlResult:
        async with self._database.immediate_session() as session:
            row = await self.load_receipt(session, principal, command)
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

    async def load_receipt(
        self,
        session: AsyncSession,
        principal: ControlPrincipal,
        command: ControlCommand,
    ) -> ControlCommandReceiptModel | None:
        stmt = select(ControlCommandReceiptModel).where(
            ControlCommandReceiptModel.principal_id == principal.principal_id.text,
            ControlCommandReceiptModel.request_id == command.request_id.text,
        )
        rows = list(await session.scalars(stmt))
        if len(rows) > 1:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        return rows[0] if rows else None

    async def replay_receipt(
        self,
        session: AsyncSession,
        receipt: ControlCommandReceiptModel,
        bound_hash: str,
        *,
        principal: ControlPrincipal,
        command: ControlCommand,
        operation: str,
        capability: str,
        semantic_target_id: str,
        material: Mapping[str, JsonValue],
        failure_target_type: str,
    ) -> ControlResult | Problem:
        if type(receipt.payload_hash) is not str or len(receipt.payload_hash) != 64:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if receipt.payload_hash != bound_hash:
            return _problem(ProblemCode.IDEMPOTENCY_CONFLICT)
        self._require_receipt_lifecycle(
            receipt,
            principal=principal,
            command=command,
            operation=operation,
            material=material,
        )
        audit = await session.get(AdminOperationEventModel, receipt.audit_id)
        if audit is None:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if receipt.status == "succeeded":
            return await self._result_from_success_receipt(
                session,
                receipt,
                audit,
                operation=operation,
                capability=capability,
                principal=principal,
                command=command,
                semantic_target_id=semantic_target_id,
                material=material,
            )
        if receipt.status == "failed":
            self._require_failure_audit_chain(
                audit,
                receipt,
                principal=principal,
                command=command,
                operation=operation,
                capability=capability,
                semantic_target_id=semantic_target_id,
                failure_target_type=failure_target_type,
            )
            return _problem(require_cacheable_problem(receipt.problem_code))
        raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))

    def _require_receipt_lifecycle(
        self,
        receipt: ControlCommandReceiptModel,
        *,
        principal: ControlPrincipal,
        command: ControlCommand,
        operation: str,
        material: Mapping[str, JsonValue],
    ) -> None:
        try:
            stored_principal = PrincipalId.parse(receipt.principal_id).text
            stored_request = RequestId.parse(receipt.request_id).text
        except (TypeError, ValueError) as exc:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH)) from exc
        if stored_principal != principal.principal_id.text:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if stored_request != command.request_id.text:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if receipt.audit_id is None or type(receipt.audit_id) is not int or receipt.audit_id < 1:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if receipt.status == "succeeded":
            if receipt.problem_code is not None:
                raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
            if type(receipt.result_resource_id) is not str:
                raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
            if type(receipt.result_revision) is bool or type(receipt.result_revision) is not int:
                raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
            if receipt.result_revision < 1 or receipt.effective_state_json is None:
                raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
            try:
                require_receipt_operation_pair(
                    operation=operation,
                    material=material,
                    resource_id=receipt.result_resource_id,
                    kind=receipt.operation_kind,
                    ref=receipt.operation_ref,
                )
            except ControlCommandError as exc:
                raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH)) from exc
            return
        if receipt.status == "failed":
            if (
                receipt.result_resource_id is not None
                or receipt.result_revision is not None
                or receipt.effective_state_json is not None
                or receipt.operation_kind is not None
                or receipt.operation_ref is not None
            ):
                raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
            require_cacheable_problem(receipt.problem_code)
            return
        raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))

    def _require_audit_correlation(
        self,
        audit: AdminOperationEventModel,
        *,
        principal: ControlPrincipal,
        command: ControlCommand,
        operation: str,
        capability: str,
    ) -> None:
        if (
            audit.actor_principal_kind != "control"
            or audit.actor_principal_id != principal.principal_id.text
        ):
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if audit.control_request_id != command.request_id.text:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if audit.conversation_key != "":
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if audit.capability != capability or audit.operation != operation:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))

    def _load_audit_json(self, audit: AdminOperationEventModel) -> tuple[object, object]:
        try:
            return json.loads(audit.before_json), json.loads(audit.after_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH)) from exc

    async def _result_from_success_receipt(
        self,
        session: AsyncSession,
        receipt: ControlCommandReceiptModel,
        audit: AdminOperationEventModel,
        *,
        operation: str,
        capability: str,
        principal: ControlPrincipal,
        command: ControlCommand,
        semantic_target_id: str,
        material: Mapping[str, JsonValue],
    ) -> ControlResult:
        self._require_audit_correlation(
            audit,
            principal=principal,
            command=command,
            operation=operation,
            capability=capability,
        )
        if audit.success is not True or audit.error_category is not None:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if audit.target_type != success_audit_target_type(operation):
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if type(receipt.result_resource_id) is not str:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if audit.target_id != receipt.result_resource_id:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if operation == CommandOperation.PERSON_DISABLE.value:
            if receipt.result_resource_id != semantic_target_id:
                raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if type(receipt.result_revision) is bool or type(receipt.result_revision) is not int:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if type(receipt.effective_state_json) is not str or receipt.audit_id is None:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        before, after = self._load_audit_json(audit)
        try:
            validate_success_audit_before(before, operation=operation)
            after_state = validate_success_audit_after(
                after,
                operation=operation,
                resource_id=receipt.result_resource_id,
                revision=receipt.result_revision,
                semantic_target_id=semantic_target_id,
                material=material,
            )
            state = json.loads(receipt.effective_state_json)
            result = project_replayed_result(
                operation=operation,
                resource_id=receipt.result_resource_id,
                revision=receipt.result_revision,
                audit_id=str(receipt.audit_id),
                raw_state=state,
                semantic_target_id=semantic_target_id,
                material=material,
            )
        except ControlCommandError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH)) from exc
        if dict(result.effective_state) != after_state:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        return ControlResult(
            success=True,
            resource_id=result.resource_id,
            revision=result.revision,
            audit_id=result.audit_id,
            effective_state=result.effective_state,
            operation=None,
        )

    def _require_failure_audit_chain(
        self,
        audit: AdminOperationEventModel,
        receipt: ControlCommandReceiptModel,
        *,
        principal: ControlPrincipal,
        command: ControlCommand,
        operation: str,
        capability: str,
        semantic_target_id: str,
        failure_target_type: str,
    ) -> None:
        self._require_audit_correlation(
            audit,
            principal=principal,
            command=command,
            operation=operation,
            capability=capability,
        )
        if audit.success is not False or audit.error_category != receipt.problem_code:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        if audit.target_type != failure_target_type or audit.target_id != semantic_target_id:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        before, after = self._load_audit_json(audit)
        try:
            validate_failure_audit_before(before, operation=operation)
            validate_failure_audit_after(after, problem_code=str(receipt.problem_code))
        except ControlCommandError:
            raise

    async def record_success(
        self,
        session: AsyncSession,
        *,
        principal: ControlPrincipal,
        command: ControlCommand,
        operation: str,
        capability: str,
        payload_hash: str,
        success: CommandSuccess,
        semantic_target_id: str,
        material: Mapping[str, JsonValue],
        started: float,
        existing_receipt: ControlCommandReceiptModel | None = None,
    ) -> ControlResult:
        validate_success_audit_before(success.before, operation=operation)
        after_state = validate_success_audit_after(
            success.after,
            operation=operation,
            resource_id=success.resource_id,
            revision=success.revision,
            semantic_target_id=semantic_target_id,
            material=material,
        )
        projected_state = project_replayed_result(
            operation=operation,
            resource_id=success.resource_id,
            revision=success.revision,
            audit_id="0",
            raw_state=success.effective_state,
            semantic_target_id=semantic_target_id,
            material=material,
        )
        if dict(projected_state.effective_state) != after_state:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        audit = AdminOperationEventModel(
            actor_user_id=principal.principal_id.text,
            actor_principal_kind="control",
            actor_principal_id=principal.principal_id.text,
            control_request_id=command.request_id.text,
            trigger_message_id="",
            conversation_key="",
            capability=capability,
            operation=operation,
            target_type=success.target_type,
            target_id=success.target_id,
            before_json=_dump_json(success.before),
            after_json=_dump_json(success.after),
            success=True,
            error_category=None,
            duration_seconds=_duration(started),
            created_at=_now(),
        )
        session.add(audit)
        await session.flush()
        if self._after_audit_flush is not None:
            self._after_audit_flush()
        if audit.id is None:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        try:
            kind, ref = require_receipt_operation_pair(
                operation=operation,
                material=material,
                resource_id=success.resource_id,
                kind=(
                    None
                    if success.operation is None
                    else success.operation.operation_id.partition(":")[0]
                ),
                ref=None if success.operation is None else success.operation.operation_id,
            )
        except ControlCommandError as exc:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH)) from exc
        projected = ControlResult(
            success=True,
            resource_id=projected_state.resource_id,
            revision=projected_state.revision,
            audit_id=str(audit.id),
            effective_state=projected_state.effective_state,
            operation=success.operation,
        )
        stamp = _now()
        receipt = existing_receipt or ControlCommandReceiptModel(
            principal_id=principal.principal_id.text,
            request_id=command.request_id.text,
            payload_hash=payload_hash,
            created_at=stamp,
        )
        receipt.status = "succeeded"
        receipt.result_resource_id = projected.resource_id
        receipt.effective_state_json = _dump_json(dict(projected.effective_state))
        receipt.result_revision = projected.revision
        receipt.problem_code = None
        receipt.audit_id = audit.id
        receipt.operation_kind = kind
        receipt.operation_ref = ref
        receipt.updated_at = (
            next_updated_at(existing_receipt.updated_at, stamp) if existing_receipt else stamp
        )
        session.add(receipt)
        return projected

    async def record_failure(
        self,
        session: AsyncSession,
        *,
        principal: ControlPrincipal,
        command: ControlCommand,
        operation: str,
        capability: str,
        target_type: str,
        target_id: str,
        payload_hash: str,
        problem: Problem,
        before: Mapping[str, JsonValue],
        started: float,
        existing_receipt: ControlCommandReceiptModel | None = None,
    ) -> Problem:
        if problem.code not in CACHEABLE_COMMAND_FAILURES:
            raise ControlCommandError(_problem(ProblemCode.STATE_MISMATCH))
        safe_before = validate_failure_audit_before(dict(before), operation=operation)
        validate_failure_audit_after(
            {"problem": problem.code.value}, problem_code=problem.code.value
        )
        audit = AdminOperationEventModel(
            actor_user_id=principal.principal_id.text,
            actor_principal_kind="control",
            actor_principal_id=principal.principal_id.text,
            control_request_id=command.request_id.text,
            trigger_message_id="",
            conversation_key="",
            capability=capability,
            operation=operation,
            target_type=target_type,
            target_id=target_id,
            before_json=_dump_json(safe_before),
            after_json=_dump_json({"problem": problem.code.value}),
            success=False,
            error_category=problem.code.value,
            duration_seconds=_duration(started),
            created_at=_now(),
        )
        session.add(audit)
        await session.flush()
        if self._after_audit_flush is not None:
            self._after_audit_flush()
        stamp = _now()
        receipt = existing_receipt or ControlCommandReceiptModel(
            principal_id=principal.principal_id.text,
            request_id=command.request_id.text,
            payload_hash=payload_hash,
            created_at=stamp,
        )
        receipt.status = "failed"
        receipt.result_resource_id = None
        receipt.effective_state_json = None
        receipt.result_revision = None
        receipt.problem_code = problem.code.value
        receipt.audit_id = audit.id
        receipt.operation_kind = None
        receipt.operation_ref = None
        receipt.updated_at = (
            next_updated_at(existing_receipt.updated_at, stamp) if existing_receipt else stamp
        )
        session.add(receipt)
        return problem
