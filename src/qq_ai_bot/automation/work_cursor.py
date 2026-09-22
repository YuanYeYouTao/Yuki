"""Persist the owning automation step; only Agent steps may resume after dispatch."""

import json
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.work_recovery_schema import invocations
from qq_ai_bot.runtime.work_repository import bounded_json


async def load(database: Database, run_id: int, script_hash: str) -> tuple[str, dict[str, Any]]:
    async with database.sessions() as session:
        row = (
            (await session.execute(select(invocations).where(invocations.c.run_id == run_id)))
            .mappings()
            .first()
        )
    if row is None:
        return "new", {}
    payload = json.loads(row["payload_json"])
    if row["script_hash"] != script_hash:
        # The caller may retain prior accounting, but must not execute this cursor.
        return "changed", payload
    return row["phase"], payload


async def save(
    database: Database,
    run_id: int,
    script_hash: str,
    phase: str,
    payload: dict[str, Any],
    *,
    expected_owner: str | None = None,
) -> None:
    values = dict(
        run_id=run_id,
        script_hash=script_hash,
        phase=phase,
        payload_json=bounded_json(payload, 1024 * 1024),
        updated=time.time(),
    )
    async with database.immediate_session() as session:
        if expected_owner is not None:
            from qq_ai_bot.automation.executor import AutomationExecutionError
            from qq_ai_bot.persistence.models import AutomationModel, AutomationRunModel

            owner = await session.scalar(
                select(AutomationModel.claimed_by)
                .join(AutomationRunModel, AutomationRunModel.automation_id == AutomationModel.id)
                .where(AutomationRunModel.id == run_id, AutomationModel.script_hash == script_hash)
            )
            if owner != expected_owner:
                raise AutomationExecutionError("automation_lease_lost")
        await session.execute(
            insert(invocations)
            .values(**values)
            .on_conflict_do_update(index_elements=[invocations.c.run_id], set_=values)
        )
