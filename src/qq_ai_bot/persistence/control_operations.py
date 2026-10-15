"""Project original control requests and existing domain runs without executing them."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.authority.operations import (
    OperationKind,
    OperationRef,
)
from qq_ai_bot.admin.authority.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import ControlQueryError
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.domain.identity import PrincipalId, RequestId
from qq_ai_bot.memory.dream.db_models import MemoryDreamRunModel
from qq_ai_bot.persistence.command_receipts import control_operation, run_operation
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryRebuildRunModel


async def read_operation(
    database: Database, session: AsyncSession, operation_id: str
) -> OperationRef:
    del database
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
            kind = OperationKind(parts[0])
            model = MemoryRebuildRunModel if kind is OperationKind.REBUILD else MemoryDreamRunModel
            run = (
                await session.execute(
                    select(
                        model.public_id,
                        model.status,
                        model.created_at,
                        model.updated_at,
                        model.error_category,
                    ).where(model.public_id == public_id)
                )
            ).one_or_none()
            if run is not None:
                return run_operation(
                    kind,
                    run.public_id,
                    run.status,
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                    error_category=run.error_category,
                )
        else:
            raise ValueError("invalid operation ID")
    except (TypeError, ValueError) as exc:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
    raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
