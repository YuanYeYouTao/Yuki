"""Content may be forgotten while its already dispatched effect is confirmed."""

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.social.models import SocialTarget


async def content_sources_retained(
    session: AsyncSession,
    *,
    conversation_id: str,
    target: SocialTarget,
    event_ids: tuple[int | None, ...],
    person_id: str | None = None,
) -> bool:
    """Keep the confirmed receipt even when privacy no longer permits its body."""
    if await session.get(CanonicalConversationModel, conversation_id) is None:
        return False
    if (
        await session.get(
            CanonicalPersonModel if target.kind == "person" else CanonicalSpaceModel,
            str(target.id),
        )
        is None
    ):
        return False
    if person_id is not None and await session.get(CanonicalPersonModel, person_id) is None:
        return False
    for event_id in dict.fromkeys(event_ids):
        if event_id is not None and await session.get(ChatEventModel, event_id) is None:
            return False
    return True
