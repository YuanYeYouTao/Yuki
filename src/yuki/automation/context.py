"""Resolve an automation conversation without splitting on bot_user_id."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from yuki.automation.models import AutomationRecord
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.hydrate import (
    require_primary_alias_for_conversation,
)
from yuki.identity.errors import CanonicalIdentityError


class AutomationBindError(RuntimeError):
    """Canonical conversation hydrate failed closed."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


async def bind_automation_conversation(
    session: AsyncSession,
    automation: AutomationRecord,
) -> tuple[str, str | None]:
    """Use the saved source; a delivery choice or today's route cannot recreate it."""
    original_id = automation.canonical_conversation_id
    existing = await session.get(CanonicalConversationModel, original_id) if original_id else None
    if existing is None:
        raise AutomationBindError("automation_scene_unavailable")
    return await _require_primary_alias(session, existing.id), existing.id


async def _require_primary_alias(session: AsyncSession, conversation_id: str) -> str:
    try:
        return await require_primary_alias_for_conversation(session, conversation_id)
    except CanonicalIdentityError as exc:
        raise AutomationBindError(exc.category) from exc
