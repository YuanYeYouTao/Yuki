"""Trusted SELF execution scene. Controller epochs are never execution permissions."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.autonomy_db_models import InitiativeRunModel
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.db_models import CanonicalSpaceModel, PresenceModel
from yuki.persistence.database import Database
from yuki.persistence.unit_of_work import optional_session


async def validate_self_initiative(
    database: Database,
    run_id: str,
    *,
    conversation_id: str,
    space_id: str,
    presence_id: str,
    session: AsyncSession | None = None,
) -> None:
    """Recheck committed ownership and the current scene before an execution/effect.

    Switching the proposal owner does not revoke accepted work. Generation reset,
    disabled space/presence revoke new effects. Original Work execution retains
    its own lease and authority after the initiative owner settles.
    """
    async with optional_session(database, session, write=False) as session:
        run = (
            await session.execute(
                select(
                    InitiativeRunModel.conversation_id,
                    InitiativeRunModel.generation,
                    InitiativeRunModel.space_id,
                    InitiativeRunModel.presence_id,
                ).where(InitiativeRunModel.id == run_id)
            )
        ).one_or_none()
        if (
            run is None
            or run.conversation_id != conversation_id
            or run.space_id != space_id
            or run.presence_id != presence_id
        ):
            raise PermissionError("self_initiative_unavailable")
        conversation = await session.get(CanonicalConversationModel, conversation_id)
        space = await session.get(CanonicalSpaceModel, space_id)
        presence = await session.get(PresenceModel, presence_id)
        if (
            conversation is None
            or conversation.kind != "space"
            or conversation.generation != run.generation
            or conversation.space_id != space_id
            or space is None
            or not space.enabled
            or presence is None
            or not presence.enabled
        ):
            raise PermissionError("self_initiative_scene_changed")
