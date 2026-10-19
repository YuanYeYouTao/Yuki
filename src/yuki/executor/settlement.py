"""Local completion facts share the original Work effect and its first acceptance."""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.executor.db_models import ExecutorOperationModel
from yuki.executor.service import TERMINAL, operation_evidence
from yuki.runtime.effect_settlement import replace_effect_receipt
from yuki.runtime.work_schema_v1 import effects


def merge_operation(receipt: dict[str, Any], row: ExecutorOperationModel) -> dict[str, Any]:
    original = receipt.get("outcome", {})
    return {**receipt, "outcome": {**original, **operation_evidence(row)}}


async def first_acceptance(
    session: AsyncSession, key: str, work_id: str, candidate: dict[str, Any]
) -> dict[str, Any]:
    """Called only under the actual Host acceptance writer, never before it."""
    row = await session.scalar(
        select(ExecutorOperationModel).where(
            ExecutorOperationModel.tool_call_key == key,
            ExecutorOperationModel.work_id == work_id,
        )
    )
    return merge_operation(candidate, row) if row is not None and row.capability else candidate


async def resolve_operation_effect(session: AsyncSession, row: ExecutorOperationModel) -> None:
    if row.state not in TERMINAL or row.work_id is None:
        return
    original = (
        await session.execute(
            select(effects.c.receipt_json).where(
                effects.c.effect_key == row.tool_call_key,
                effects.c.work_id == row.work_id,
                effects.c.state == "accepted",
            )
        )
    ).scalar_one_or_none()
    if original is None:
        return  # The Host's first acceptance will read this op under its writer.
    receipt = json.loads(original)
    outcome = receipt.get("outcome", {})
    if not outcome.get("pending") or outcome.get("op_id") != row.op_id:
        return
    from yuki.runtime.work_repository import encode_json

    await replace_effect_receipt(
        session,
        row.tool_call_key,
        row.work_id,
        original,
        encode_json(merge_operation(receipt, row)),
        effects.c.state == "accepted",
    )
