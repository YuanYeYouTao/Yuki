"""Numbered SQL windows; count the same filtered source, never hydrate prior pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from qq_ai_bot.control_plane.paging import PageRequest


@dataclass(frozen=True, slots=True)
class SqlWindow:
    statement: Select[Any]
    total: int | None


async def numbered_statement(
    session: AsyncSession,
    statement: Select[Any],
    request: PageRequest,
    *,
    order: tuple[ColumnElement[Any], ...],
) -> SqlWindow:
    if request.number is None:
        return SqlWindow(statement, None)
    source = statement.limit(None).offset(None)
    # Keep the original FROM/JOIN/WHERE, without projecting large private fields.
    # List statements here have one row per original record.
    count_stmt = source.order_by(None).with_only_columns(func.count(), maintain_column_froms=True)
    total = int(await session.scalar(count_stmt) or 0)
    source = source.order_by(None).order_by(*order)
    return SqlWindow(
        source.offset((request.number - 1) * request.limit).limit(request.limit + 1), total
    )
