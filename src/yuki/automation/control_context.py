"""Resolve task authority from its permanent owner and original execution scene."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from yuki.automation.authority import PermissionLevel
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
)
from yuki.identity.authority import person_is_superuser
from yuki.identity.db_models import (
    CanonicalPersonModel,
    IdentityBindingModel,
)
from yuki.identity.person_identity import current_person_id


async def resolve_execution_identity(
    session: AsyncSession,
    *,
    owner_id: str,
    conversation_id: str | None = None,
    presence_id: str | None = None,
) -> tuple[str, PermissionLevel]:
    """Project the permanent owner within the original entry, not its latest send route."""
    person = await session.get(CanonicalPersonModel, owner_id)
    if person is None or not person.enabled:
        raise PermissionError("automation_owner_unavailable")
    conversation = (
        await session.get(CanonicalConversationModel, conversation_id) if conversation_id else None
    )
    root_id = await current_person_id(session, owner_id)
    root = await session.get(CanonicalPersonModel, root_id)
    if root is None or not root.enabled:
        raise PermissionError("automation_owner_unavailable")
    account = ""
    if conversation is not None and conversation.kind == "private":
        binding = await session.get(IdentityBindingModel, conversation.identity_binding_id)
        if binding is not None:
            account = binding.external_account_id
    permission = (
        PermissionLevel.SUPERUSER
        if await person_is_superuser(session, owner_id)
        else PermissionLevel.USER
    )
    return account, permission
