"""Retention for the retired web-search source index."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import WebSearchRunModel


class WebSearchSourceRepository:
    """Clean historical rows; new web results use the original tool receipt."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def cleanup_expired(
        self,
        *,
        retention_days: int,
        now: datetime | None = None,
        limit: int = 128,
    ) -> int:
        """Delete expired runs; source rows cascade through their foreign key."""

        cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
        if not 1 <= limit <= 128:
            raise ValueError("cleanup page must be between 1 and 128")
        async with self._database.sessions() as session:
            ids = tuple(
                await session.scalars(
                    select(WebSearchRunModel.id)
                    .where(WebSearchRunModel.created_at < cutoff)
                    .order_by(WebSearchRunModel.created_at, WebSearchRunModel.id)
                    .limit(limit)
                )
            )
        if not ids:
            return 0
        async with self._database.immediate_session() as session:
            result = await session.execute(
                delete(WebSearchRunModel).where(
                    WebSearchRunModel.id.in_(ids), WebSearchRunModel.created_at < cutoff
                )
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)
