"""Resolve complete-v2 automation conversation without splitting on bot_user_id."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.automation.models import AutomationRecord
from qq_ai_bot.conversation.hydrate import (
    conversation_for_owner,
    require_primary_alias_for_conversation,
)
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.identity.runtime import identity_runtime_is_complete_v2


class AutomationBindError(RuntimeError):
    """Canonical conversation hydrate failed closed."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


async def bind_automation_conversation(
    session: AsyncSession,
    automation: AutomationRecord,
) -> tuple[str, str | None]:
    """Return (conversation_key, conversation_id).

    v1 keeps the per-task lock key. complete-v2 hydrates the XOR owner
    Conversation and its single frozen primary alias. Missing Conversation
    does not create a row and uses a stable ``person:{id}`` / ``space:{id}``
    key. An existing Conversation without exactly one primary alias is
    ``state_mismatch``. NULL/NULL or dual target is not a legacy fallback.
    """

    person_id = automation.canonical_target_person_id
    space_id = automation.canonical_target_space_id
    if not await identity_runtime_is_complete_v2(session):
        return f"automation:{automation.id}", None
    if person_id and space_id:
        raise AutomationBindError("state_mismatch")
    if not person_id and not space_id:
        raise AutomationBindError("target_missing")
    if person_id:
        existing = await conversation_for_owner(session, kind="private", person_id=person_id)
        if existing is None:
            return f"person:{person_id}", None
        return await _require_primary_alias(session, existing.id), existing.id
    existing = await conversation_for_owner(session, kind="space", space_id=space_id)
    if existing is None:
        return f"space:{space_id}", None
    return await _require_primary_alias(session, existing.id), existing.id


async def _require_primary_alias(session: AsyncSession, conversation_id: str) -> str:
    try:
        return await require_primary_alias_for_conversation(session, conversation_id)
    except IdentityDualWriteError as exc:
        raise AutomationBindError(exc.category) from exc
