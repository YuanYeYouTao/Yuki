"""Resolve the physical entry proved by an original internal event."""

from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.db_models import IdentityBindingModel, PresenceModel
from yuki.persistence.models import ChatEventModel


async def event_entry_matches(
    session: AsyncSession,
    conversation: CanonicalConversationModel,
    binding: IdentityBindingModel,
    presence: PresenceModel,
    source_event_id: int | None,
) -> bool:
    """The source event proves its account, receiving stage and Conversation."""
    if source_event_id is None or binding.platform != presence.platform:
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
    return True
