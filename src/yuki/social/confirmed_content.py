"""Content may be forgotten while its already dispatched effect is confirmed."""

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from yuki.persistence.models import ChatEventModel
from yuki.social.models import SocialTarget


async def content_sources_retained(
    session: AsyncSession,
    *,
    conversation_id: str,
    target: SocialTarget,
    event_ids: tuple[int | None, ...],
    person_id: str | None = None,
) -> bool:
    """Keep the confirmed receipt even when privacy no longer permits its body."""
    target_model = CanonicalPersonModel if target.kind == "person" else CanonicalSpaceModel
    retained = [
        select(CanonicalConversationModel.id)
        .where(CanonicalConversationModel.id == conversation_id)
        .exists(),
        select(target_model.id).where(target_model.id == str(target.id)).exists(),
    ]
    if person_id is not None:
        retained.append(
            select(CanonicalPersonModel.id).where(CanonicalPersonModel.id == person_id).exists()
        )
    retained.extend(
        select(ChatEventModel.id).where(ChatEventModel.id == event_id).exists()
        for event_id in dict.fromkeys(event_ids)
        if event_id is not None
    )
    return bool(await session.scalar(select(and_(*retained))))
