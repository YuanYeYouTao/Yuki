"""Trusted SELF execution scene. Controller epochs are never execution permissions."""

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.autonomy_binding import AcceptedInitiative
from qq_ai_bot.conversation.autonomy_repository import AutonomyRepository
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.db_models import CanonicalSpaceModel, PresenceModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.unit_of_work import optional_session


async def validate_self_initiative(
    database: Database,
    run_id: str,
    *,
    conversation_id: str,
    space_id: str,
    presence_id: str,
    session: AsyncSession | None = None,
) -> AcceptedInitiative:
    """Recheck committed ownership and the current scene before an execution/effect.

    Switching the proposal owner does not revoke accepted work. Generation reset,
    disabled space/presence revoke new effects. Original Work execution retains
    its own lease and authority after the initiative owner settles.
    """
    run = await AutonomyRepository(database).get_run(run_id, session=session)
    if (
        run is None
        or run.conversation_id != conversation_id
        or run.space_id != space_id
        or run.presence_id != presence_id
    ):
        raise PermissionError("self_initiative_unavailable")
    async with optional_session(database, session, write=False) as session:
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
            or presence.platform != "qq"
        ):
            raise PermissionError("self_initiative_scene_changed")
    return run
