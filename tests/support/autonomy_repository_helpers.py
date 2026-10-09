"""The dormant host protocol must survive competing proposers and process restarts."""

from dataclasses import dataclass

from qq_ai_bot.conversation.autonomy_binding import (
    AutonomyBinding,
    InitiativeSource,
    InitiativeSourceKind,
)
from qq_ai_bot.conversation.autonomy_repository import AutonomyRepository
from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
from qq_ai_bot.identity.canonical_repository import ensure_presence, ensure_space
from qq_ai_bot.persistence.database import Database


@dataclass(frozen=True)
class Scene:
    conversation: str
    space: str
    presence: str


async def _scene(database: Database) -> Scene:
    async with database.immediate_session() as session:
        space = await ensure_space(session, "2001")
        presence = await ensure_presence(session, "8000")
        conversation = await ensure_canonical_conversation(
            session, kind="space", primary_scope_key="group:8000:2001", space_id=space
        )
    return Scene(conversation.conversation_id, space, presence)


def _event(number: int = 1, revision: str = "v1") -> tuple[InitiativeSource, ...]:
    # Fixtures stand in for already host-resolved focus references. The repository is
    # deliberately not the future semantic/source authorization adapter.
    return (InitiativeSource(InitiativeSourceKind.EVENT, str(number), revision),)


async def _enable(repository: AutonomyRepository, scene: Scene) -> AutonomyBinding:
    current = await repository.ensure_binding(scene.conversation, 1)
    return await repository.transition(current, master_enabled=True, external_enabled=True)


async def _accept(repository, scene, binding, proposal="p1", sources=None):
    return await repository.accept_host_proposal(
        proposal_id=proposal,
        binding=binding,
        owner=binding.effective_owner,
        space_id=scene.space,
        presence_id=scene.presence,
        sources=sources or _event(),
        support_refs=("observation:1",),
    )
