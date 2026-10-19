"""Conditional receipt replacement inside the caller's already-authorized writer."""

import time
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.runtime.work_schema_v1 import effects


async def replace_effect_receipt(
    session: AsyncSession,
    key: str,
    work_id: str,
    previous_json: str,
    next_json: str,
    *conditions: Any,
) -> bool:
    changed = await session.scalar(
        update(effects)
        .where(
            effects.c.effect_key == key,
            effects.c.work_id == work_id,
            effects.c.receipt_json == previous_json,
            *conditions,
        )
        .values(receipt_json=next_json, updated=time.time())
        .returning(effects.c.effect_key)
    )
    if changed is not None:
        session.info.setdefault("work_step_hints", set()).add(work_id)
    return changed is not None
