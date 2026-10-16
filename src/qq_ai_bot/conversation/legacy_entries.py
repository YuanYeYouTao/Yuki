"""Read real pre-entry-split private provenance without changing its owner."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.identity.db_models import IdentityBindingModel, PresenceModel
from qq_ai_bot.persistence.models import ChatEventModel


async def legacy_private_entry_matches(
    session: AsyncSession,
    conversation: CanonicalConversationModel,
    binding: IdentityBindingModel,
    presence: PresenceModel,
    source_event_id: int | None,
) -> bool:
    """An original event and its actual old QQ locator prove the retained entry."""
    if source_event_id is None or presence.platform != "qq":
        return False
    source = await session.get(ChatEventModel, source_event_id)
    if (
        source is None
        or source.canonical_conversation_id != conversation.id
        or source.ingress_presence_id != presence.id
        or source.bot_user_id != presence.external_account_id
        or source.private_peer_user_id != binding.external_account_id
    ):
        return False
    return (
        await session.scalar(
            select(ConversationLegacyAliasModel.id).where(
                ConversationLegacyAliasModel.conversation_id == conversation.id,
                ConversationLegacyAliasModel.scope_key
                == f"bot:{presence.external_account_id}:private:{binding.external_account_id}",
            )
        )
        is not None
    )
