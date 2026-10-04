"""Atomic accounting shared by a root task and all its workers."""

from sqlalchemy import func, or_, select, true, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.work_budget_schema import automation_budgets, budgets
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
            select(
                work.c.model_requests,
                work.c.tool_calls,
                func.json_extract(work.c.source_json, "$.owner").label("source_owner"),
                func.json_extract(work.c.source_json, "$.automation_run_id").label("run_id"),
            ).where(work.c.id == root)
        )
    ).one()
    await session.execute(
        insert(budgets)
        .values(
            root_id=root,
            models=current.model_requests,
            tools=current.tool_calls,
            model_limit=None,
            tool_limit=None,
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
                    budgets.c.models + models <= budgets.c.model_limit - reserve,
                )
                if models
                else true(),
                or_(
                    budgets.c.tool_limit.is_(None),
                    budgets.c.tools + tools <= budgets.c.tool_limit - reserve,
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
    if current.source_owner == "automation" and isinstance(current.run_id, int):
        await charge_automation_run(session, current.run_id, models=models, tools=tools)


async def charge_automation_run(
    session: AsyncSession, run_id: int, *, models: int, tools: int
) -> None:
    run_budgets = automation_budgets
    if models < 0 or tools < 0:
        raise ValueError("invalid_work_budget_charge")
    if not models and not tools:
        return
    await session.execute(
        insert(run_budgets)
        .values(run_id=run_id, model_limit=None, tool_limit=None)
        .on_conflict_do_nothing()
    )
    accepted = await session.scalar(
        update(run_budgets)
        .where(
            run_budgets.c.run_id == run_id,
            or_(
                run_budgets.c.model_limit.is_(None),
                run_budgets.c.models + models <= run_budgets.c.model_limit,
            ),
            or_(
                run_budgets.c.tool_limit.is_(None),
                run_budgets.c.tools + tools <= run_budgets.c.tool_limit,
            ),
        )
        .values(models=run_budgets.c.models + models, tools=run_budgets.c.tools + tools)
        .returning(run_budgets.c.run_id)
    )
    if accepted is None:
        raise WorkBudgetExceeded("automation_run_total_budget_exhausted")
