"""Deterministic Memory V2 retrieval target resolution."""

from __future__ import annotations

from pydantic import ValidationError
from sqlalchemy import text

from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage
from yuki.memory.enums import (
    MemoryScopeType,
    MemoryTargetRole,
    SelfMemoryVisibility,
)
from yuki.memory.errors import MemoryRetrievalError
from yuki.memory.models import MemoryEntityTarget
from yuki.memory.read_scope import MemoryReadScopeResolver
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repositories import PeopleRepository


class MemoryTargetResolver:
    """Resolve only backend-authenticated current, mention, and reply identities."""

    def __init__(self, people: PeopleRepository) -> None:
        self._people = people
        self._read_scopes = MemoryReadScopeResolver(people.database)

    async def resolve(
        self,
        inbound: InboundMessage,
        *,
        include_self: bool = False,
    ) -> tuple[MemoryEntityTarget, ...]:
        person_id = inbound.person_id
        space_id = inbound.space_id
        try:
            targets: list[MemoryEntityTarget] = []
            if person_id is None:
                return ()
            if include_self:
                private = inbound.scope_type is ScopeType.PRIVATE
                targets.append(
                    MemoryEntityTarget(
                        role=MemoryTargetRole.CURRENT_SELF,
                        scope_type=MemoryScopeType.SELF,
                        visibility_type=SelfMemoryVisibility.PRIVATE
                        if private
                        else SelfMemoryVisibility.GROUP,
                        canonical_visibility_person_id=person_id if private else None,
                        canonical_visibility_space_id=None if private else space_id,
                        block_id="current_self",
                    )
                )
            group = inbound.scope_type is ScopeType.GROUP and space_id is not None
            async with self._people.database.read_session() as session:
                await session.execute(text("BEGIN"))
                candidates = list(inbound.mentioned_person_ids) if group else []
                if group and inbound.reply_to_event_id is not None:
                    reply = await session.get(ChatEventModel, inbound.reply_to_event_id)
                    if reply is not None and reply.author_person_id is not None:
                        candidates.append(reply.author_person_id)
                candidates = list(dict.fromkeys(item for item in candidates if item != person_id))
                scopes = await self._read_scopes.people(
                    person_id,
                    (person_id, *candidates),
                    group_id=space_id if group else None,
                    include_person_groups=group,
                    restrict_to_group=False,
                    session=session,
                )
            current = scopes.get(person_id)
            if current is not None:
                targets.extend(
                    target
                    if target.scope_type is MemoryScopeType.PERSON
                    else target.model_copy(update={"block_id": "current_person_in_group"})
                    for target in current.targets
                )
                if group and any(
                    target.scope_type is MemoryScopeType.PERSON_GROUP for target in current.targets
                ):
                    targets.append(
                        MemoryEntityTarget(
                            scope_type=MemoryScopeType.GROUP,
                            canonical_subject_space_id=space_id,
                            role=MemoryTargetRole.CURRENT_GROUP,
                            block_id="current_group",
                        )
                    )
            for candidate in candidates:
                resolved = scopes.get(candidate)
                if resolved is not None:
                    targets.extend(resolved.targets)
        except ValidationError as exc:
            raise MemoryRetrievalError("memory_target_invalid") from exc
        return tuple(targets)
