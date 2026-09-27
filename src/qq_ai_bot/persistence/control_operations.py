"""Project original control requests and existing domain runs without executing them."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.control_plane.operations import (
    OperationRef,
    OperationStatus,
    StateEpoch,
    _sanitize_error_category,
)
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import ControlQueryError
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.domain.identity import PrincipalId, RequestId
from qq_ai_bot.memory.dream.models import DreamRun
from qq_ai_bot.memory.dream.repository import DreamRepository
from qq_ai_bot.memory.rebuild.models import MemoryRebuildRun
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.persistence.database import Database


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
    status = _rebuild_status(run.status.value)
    return _op_ref(
        f"rebuild:{run.public_id}",
        status,
        created_at=run.created_at,
        updated_at=run.updated_at,
        error_category=run.error_category or "rebuild_failed",
    )


def dream_operation(run: DreamRun) -> OperationRef:
    status = _dream_status(run.status.value)
    return _op_ref(
        f"dream:{run.public_id}",
        status,
        created_at=run.created_at,
        updated_at=run.updated_at,
        error_category=run.error_category or "partial_failed",
    )


async def read_operation(
    database: Database, session: AsyncSession, operation_id: str
) -> OperationRef:
    parts = operation_id.split(":") if type(operation_id) is str else []
    try:
        if len(parts) == 3 and parts[0] == "control":
            principal = PrincipalId.parse(parts[1])
            request = RequestId.parse(parts[2])
            row = await session.scalar(
                select(ControlCommandReceiptModel).where(
                    ControlCommandReceiptModel.principal_id == principal.text,
                    ControlCommandReceiptModel.request_id == request.text,
                )
            )
            if row is not None:
                from qq_ai_bot.persistence.control_external import control_operation

                return control_operation(row)
        elif len(parts) == 2 and parts[0] in {"rebuild", "dream"}:
            public_id = RequestId.parse(parts[1]).text
            if parts[0] == "rebuild":
                rebuild = await MemoryRebuildRepository(database).get_run(
                    public_id, session=session
                )
                if rebuild is not None:
                    return rebuild_operation(rebuild)
            else:
                dream = await DreamRepository(database).get_run(public_id, session=session)
                if dream is not None:
                    return dream_operation(dream)
        else:
            raise ValueError("invalid operation ID")
    except (TypeError, ValueError) as exc:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
    raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
