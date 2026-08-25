"""Deterministic Memory V2 retrieval target resolution."""

from __future__ import annotations

from pydantic import ValidationError

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.memory.enums import (
    MemoryScopeType,
    MemoryTargetRole,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.errors import MemoryRetrievalError
from qq_ai_bot.memory.models import MemoryEntityTarget
from qq_ai_bot.persistence.repositories import PeopleRepository


class MemoryTargetResolver:
    """Resolve only backend-authenticated current, mention, and reply identities."""

    def __init__(self, people: PeopleRepository) -> None:
        self._people = people

    async def resolve(
        self,
        inbound: InboundMessage,
        *,
        max_referenced: int,
        include_self: bool = False,
    ) -> tuple[MemoryEntityTarget, ...]:
        user_id = inbound.sender.user_id
        try:
            targets = []
            if include_self:
                is_private = inbound.scope_type is ScopeType.PRIVATE or inbound.group_id is None
                targets.append(
                    MemoryEntityTarget(
                        role=MemoryTargetRole.CURRENT_SELF,
                        scope_type=MemoryScopeType.SELF,
                        visibility_type=(
                            SelfMemoryVisibility.PRIVATE
                            if is_private
                            else SelfMemoryVisibility.GROUP
                        ),
                        visibility_user_id=user_id if is_private else None,
                        visibility_group_id=None if is_private else inbound.group_id,
                        block_id="current_self",
                    )
                )
            targets.append(
                MemoryEntityTarget(
                    role=MemoryTargetRole.CURRENT_PERSON,
                    scope_type=MemoryScopeType.PERSON,
                    subject_user_id=user_id,
                    block_id="current_person",
                )
            )
        except ValidationError as exc:
            raise MemoryRetrievalError("memory_target_invalid") from exc
        if inbound.scope_type is ScopeType.PRIVATE or inbound.group_id is None:
            return tuple(targets)

        group_id = inbound.group_id
        try:
            targets.extend(
                (
                    MemoryEntityTarget(
                        role=MemoryTargetRole.CURRENT_PERSON_GROUP,
                        scope_type=MemoryScopeType.PERSON_GROUP,
                        subject_user_id=user_id,
                        group_id=group_id,
                        block_id="current_person_in_group",
                    ),
                    MemoryEntityTarget(
                        role=MemoryTargetRole.CURRENT_GROUP,
                        scope_type=MemoryScopeType.GROUP,
                        group_id=group_id,
                        block_id="current_group",
                    ),
                )
            )
        except ValidationError as exc:
            raise MemoryRetrievalError("memory_target_invalid") from exc
        candidates = list(
            await self._people.person_reference_ids(
                tuple(
                    item
                    for item in (*inbound.mentioned_user_ids, inbound.reply_sender_user_id)
                    if item
                ),
                speaker_user_id=user_id,
                bot_user_id=inbound.bot_user_id,
            )
        )
        members = (
            await self._people.members_in_group(tuple(candidates), group_id)
            if candidates
            else frozenset()
        )
        referenced_count = 0
        for candidate in candidates:
            if candidate not in members:
                continue
            try:
                targets.append(
                    MemoryEntityTarget(
                        role=MemoryTargetRole.REFERENCED_PERSON_GROUP,
                        scope_type=MemoryScopeType.PERSON_GROUP,
                        subject_user_id=candidate,
                        group_id=group_id,
                        block_id=f"referenced_person_group:{candidate}:{group_id}",
                    )
                )
            except ValidationError as exc:
                raise MemoryRetrievalError("memory_target_invalid") from exc
            referenced_count += 1
            if referenced_count >= max_referenced:
                break
        return tuple(targets)
