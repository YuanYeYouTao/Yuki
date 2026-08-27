"""Persistence query contract for temporary Rollup coverage holds."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession


class RollupCoverageHoldQuery(Protocol):
    """Return the earliest event that active work still needs in raw history."""

    async def earliest_source_event_id(
        self,
        session: AsyncSession,
        *,
        canonical_conversation_id: str,
    ) -> int | None: ...


__all__ = ["RollupCoverageHoldQuery"]
