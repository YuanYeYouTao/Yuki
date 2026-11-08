"""Atomic accounting shared by a root task and all its workers."""

from sqlalchemy import or_, select, true, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_schema_v1 import work
from yuki.runtime.work_tree import budget_root_id


class WorkBudgetExceeded(ValueError):
    """No provider request or tool may be dispatched after a failed reservation."""


async def limit(
    session: AsyncSession, root_id: str, *, models: int | None, tools: int | None
) -> None:
    """Record a caller's explicit total limits on a newly admitted root Work."""
    await session.execute(
        insert(budgets)
        .values(root_id=root_id, models=0, tools=0, model_limit=models, tool_limit=tools)
        .on_conflict_do_update(
            index_elements=[budgets.c.root_id],
            set_={"model_limit": models, "tool_limit": tools},
        )
    )


async def charge(session: AsyncSession, identity: str, *, models: int, tools: int) -> None:
    if models < 0 or tools < 0:
        raise ValueError("invalid_work_budget_charge")
    if not models and not tools:
        return
    root = await budget_root_id(session, identity)
    await session.execute(
        insert(budgets)
        .from_select(
            ("root_id", "models", "tools"),
            select(work.c.id, work.c.model_requests, work.c.tool_calls).where(work.c.id == root),
        )
        .on_conflict_do_nothing(index_elements=[budgets.c.root_id])
    )
    accepted = (
        await session.execute(
            update(budgets)
            .where(
                budgets.c.root_id == root,
                or_(
                    budgets.c.model_limit.is_(None),
                    budgets.c.models + models <= budgets.c.model_limit,
                )
                if models
                else true(),
                or_(
                    budgets.c.tool_limit.is_(None),
                    budgets.c.tools + tools <= budgets.c.tool_limit,
                )
                if tools
                else true(),
            )
            .values(models=budgets.c.models + models, tools=budgets.c.tools + tools)
            .returning(budgets.c.root_id)
        )
    ).first()
    if accepted is None:
        raise WorkBudgetExceeded("work_total_budget_exhausted")
