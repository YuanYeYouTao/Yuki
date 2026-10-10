"""Read the business Work tree from its single parent relation."""

from typing import Any

from sqlalchemy import Select, SQLColumnExpression, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.runtime.work_schema_v1 import work


def direct_children(work_id: str) -> Select[Any]:
    return select(work.c.id).where(work.c.parent_work_id == work_id)


def descendants(work_id: str, *, include_self: bool = False) -> Select[Any]:
    tree = select(work.c.id).where(work.c.id == work_id).cte("work_descendants", recursive=True)
    child = work.alias("child_work")
    tree = tree.union_all(select(child.c.id).join(tree, child.c.parent_work_id == tree.c.id))
    query = select(tree.c.id)
    return query if include_self else query.where(tree.c.id != work_id)


def _lineage(work_id: str | SQLColumnExpression[str | None]) -> Any:
    lineage = (
        select(work.c.id, work.c.parent_work_id, literal(0).label("depth"))
        .where(work.c.id == work_id)
        .correlate_except(work)
        .cte("work_ancestors", recursive=True, nesting=True)
    )
    parent = work.alias("parent_work")
    return lineage.union_all(
        select(parent.c.id, parent.c.parent_work_id, lineage.c.depth + 1).join(
            lineage, parent.c.id == lineage.c.parent_work_id
        )
    )


def rooted_tree() -> Any:
    tree = (
        select(work.c.id.label("work_id"), work.c.id.label("root_id"))
        .where(work.c.parent_work_id.is_(None))
        .cte("work_roots", recursive=True)
    )
    child = work.alias("rooted_child")
    return tree.union_all(
        select(child.c.id, tree.c.root_id).join(tree, child.c.parent_work_id == tree.c.work_id)
    )


def ancestors(
    work_id: str | SQLColumnExpression[str | None], *, include_self: bool = False
) -> Select[Any]:
    lineage = _lineage(work_id)
    query = select(lineage.c.id).order_by(lineage.c.depth)
    return query if include_self else query.where(lineage.c.depth > 0)


async def descendant_work_ids(
    session: AsyncSession, work_id: str, *, include_self: bool = False
) -> list[str]:
    return list(await session.scalars(descendants(work_id, include_self=include_self)))


async def ancestor_work_ids(session: AsyncSession, work_id: str) -> list[str]:
    return list(await session.scalars(ancestors(work_id)))


async def budget_root_id(session: AsyncSession, work_id: str) -> str:
    lineage = _lineage(work_id)
    return str(
        (
            await session.execute(select(lineage.c.id).where(lineage.c.parent_work_id.is_(None)))
        ).scalar_one()
    )
