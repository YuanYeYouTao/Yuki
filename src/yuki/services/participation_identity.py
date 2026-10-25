"""One current identity read for a participation decision; never a persisted owner map."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from yuki.identity.person_identity import current_person_ids
from yuki.persistence.unit_of_work import optional_session

if TYPE_CHECKING:
    from yuki.services.semantic_participation import SemanticParticipationService, _Session


def actor_ids(
    item: _Session,
    additional: Iterable[str] = (),
    *,
    include_continuation_targets: bool = False,
) -> set[str]:
    """Collect controller actors before entering a database writer."""
    ids = set(additional)
    ids.update(item.controller.participation_actor_ids())
    for event in item.controller.state.events.values():
        if event.kind == "human":
            ids.update((event.author, event.target))
            resolved = item.controller.resolved_unit(event)
            if resolved is not None:
                ids.add(resolved.target)
    for boundary in item.controller.state.boundaries.values():
        ids.update((boundary.author, boundary.target))
    if include_continuation_targets:
        # Current SELF units and semantic targets can outlive their raw human
        # source. Include them before the writer's fixed-ID identity lookup.
        for unit in item.controller._participation_checkpoint().units.values():
            ids.update((unit.binding.actor, unit.binding.unit.target))
            if unit.review is not None and unit.review.resolved_unit is not None:
                ids.add(unit.review.resolved_unit.target)
        ids.update(proposal.target_hint for proposal in item.controller.state.proposals.values())
        for candidate in item.controller.state.candidates.values():
            ids.update((candidate.event.author, candidate.event.target))
    return ids


async def actor_identity(
    service: SemanticParticipationService,
    item: _Session,
    additional: Iterable[str] = (),
    *,
    session: AsyncSession | None = None,
) -> Callable[[str], str]:
    ids = actor_ids(item, additional)
    if not ids:
        return lambda actor: actor
    async with optional_session(service.database, session, write=False) as reader:
        roots = await current_person_ids(reader, ids)
    return lambda actor: roots.get(actor, actor)
