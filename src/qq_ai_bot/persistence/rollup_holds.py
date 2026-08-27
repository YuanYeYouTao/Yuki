"""Cross-feature persistence projection for Rollup coverage holds."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.plugin_host.db_models import PluginBackgroundTurnJobModel

_ACTIVE_WAKEUP_STATUSES = ("pending", "processing")


class PersistentRollupCoverageHoldQuery:
    """Read active wakeup sources without depending on plugin business services."""

    async def earliest_source_event_id(
        self,
        session: AsyncSession,
        *,
        canonical_conversation_id: str,
    ) -> int | None:
        value = await session.scalar(
            select(func.min(PluginBackgroundTurnJobModel.source_event_id)).where(
                PluginBackgroundTurnJobModel.canonical_conversation_id == canonical_conversation_id,
                PluginBackgroundTurnJobModel.status.in_(_ACTIVE_WAKEUP_STATUSES),
            )
        )
        return int(value) if value is not None else None


__all__ = ["PersistentRollupCoverageHoldQuery"]
