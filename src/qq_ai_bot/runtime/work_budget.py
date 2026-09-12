"""Atomic accounting shared by a root task and all its workers."""

from sqlalchemy import select, true, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.runtime.subagent_schema import budgets, children
from qq_ai_bot.runtime.work_schema_v1 import work


class WorkBudgetExceeded(ValueError):
    """No provider request or tool may be dispatched after a failed reservation."""


async def charge(session: AsyncSession, identity: str, *, models: int, tools: int) -> None:
    if models < 0 or tools < 0:
        raise ValueError("invalid_work_budget_charge")
    if not models and not tools:
        return
    root = await session.scalar(select(children.c.root_id).where(children.c.work_id == identity))
    reserve = 8 if root is not None else 0
    root = root or identity
    current = (
        await session.execute(
            select(work.c.model_requests, work.c.tool_calls).where(work.c.id == root)
        )
    ).one()
    await session.execute(
        insert(budgets)
        .values(root_id=root, models=current.model_requests, tools=current.tool_calls)
        .on_conflict_do_nothing(index_elements=[budgets.c.root_id])
    )
    accepted = (
        await session.execute(
            update(budgets)
            .where(
                budgets.c.root_id == root,
                (budgets.c.models + models <= budgets.c.model_limit - reserve)
                if models
                else true(),
                (budgets.c.tools + tools <= budgets.c.tool_limit - reserve) if tools else true(),
            )
            .values(models=budgets.c.models + models, tools=budgets.c.tools + tools)
            .returning(budgets.c.root_id)
        )
    ).first()
    if accepted is None:
        raise WorkBudgetExceeded("work_total_budget_exhausted")
