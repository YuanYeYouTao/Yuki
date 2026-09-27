"""Project original control requests and existing domain runs without executing them."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.control_plane.operations import OperationRef
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import ControlQueryError
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.domain.identity import PrincipalId, RequestId
from qq_ai_bot.memory.dream.repository import DreamRepository
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.persistence.control_external import control_operation
from qq_ai_bot.persistence.control_management import _dream_status, _op_ref, _rebuild_status
from qq_ai_bot.persistence.database import Database


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
                return control_operation(row)
        elif len(parts) == 2 and parts[0] in {"rebuild", "dream"}:
            public_id = RequestId.parse(parts[1]).text
            if parts[0] == "rebuild":
                rebuild = await MemoryRebuildRepository(database).get_run(
                    public_id, session=session
                )
                if rebuild is not None:
                    status = _rebuild_status(rebuild.status.value)
                    return _op_ref(
                        operation_id,
                        status,
                        created_at=rebuild.created_at,
                        updated_at=rebuild.updated_at,
                        progress=1.0
                        if rebuild.status.value in {"completed", "cancelled", "failed"}
                        else None,
                        error_category=rebuild.error_category if status.value == "failed" else None,
                    )
            else:
                dream = await DreamRepository(database).get_run(public_id, session=session)
                if dream is not None:
                    status = _dream_status(dream.status.value)
                    return _op_ref(
                        operation_id,
                        status,
                        created_at=dream.created_at,
                        updated_at=dream.updated_at,
                        progress=1.0
                        if dream.status.value in {"completed", "cancelled", "rolled_back"}
                        else None,
                        error_category=(dream.error_category or "partial_failed")
                        if status.value == "failed"
                        else None,
                    )
        else:
            raise ValueError("invalid operation ID")
    except (TypeError, ValueError) as exc:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
    raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
