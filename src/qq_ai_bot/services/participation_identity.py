"""One current identity read for a participation decision; never a persisted owner map."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.person_identity import current_person_ids
from qq_ai_bot.persistence.unit_of_work import optional_session

if TYPE_CHECKING:
    from qq_ai_bot.services.semantic_participation import SemanticParticipationService, _Session


async def actor_identity(
    service: SemanticParticipationService,
    item: _Session,
    additional: Iterable[str] = (),
    *,
    session: AsyncSession | None = None,
) -> Callable[[str], str]:
    ids = set(additional)
    for unit in item.controller.participating_units(time.time()):
        if unit.binding.actor != "SELF":
            ids.update((unit.binding.actor, unit.unit.target))
    async with optional_session(service.database, session, write=False) as reader:
        roots = await current_person_ids(reader, ids)
    return lambda actor: roots.get(actor, actor)
