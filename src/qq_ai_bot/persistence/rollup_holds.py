"""Cross-feature persistence projection for Rollup coverage holds."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_helpers import keeper_event_clause
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
        conversation = await session.get(CanonicalConversationModel, canonical_conversation_id)
        if conversation is None:
            return None
        # The worker rejects sources before /new or a later live human message.
        # Those already-obsolete jobs must not pin Rollup while awaiting a claim.
        last_human = await session.scalar(
            select(func.max(ChatEventModel.id)).where(
                ChatEventModel.canonical_conversation_id == canonical_conversation_id,
                ChatEventModel.id <= conversation.last_event_id,
                keeper_event_clause(),
                ChatEventModel.event_kind == "message",
                ChatEventModel.direction == "inbound",
                ChatEventModel.author_kind == AuthorKind.PERSON.value,
            )
        )
        floor = max(conversation.starts_after_event_id, int(last_human or 0))
        value = await session.scalar(
            select(func.min(PluginBackgroundTurnJobModel.source_event_id)).where(
                PluginBackgroundTurnJobModel.canonical_conversation_id == canonical_conversation_id,
                PluginBackgroundTurnJobModel.status.in_(_ACTIVE_WAKEUP_STATUSES),
                PluginBackgroundTurnJobModel.source_event_id > floor,
                PluginBackgroundTurnJobModel.source_event_id <= conversation.last_event_id,
            )
        )
        return int(value) if value is not None else None


__all__ = ["PersistentRollupCoverageHoldQuery"]
