"""Optional v1 expand write of 0047 Memory owner columns.

v1 read, claim, partition, and unique keys stay conversation_key / hash / bot.
Missing or ambiguous Binding/SpaceBinding leaves owner NULL. This module never
creates people, groups, or memberships, and never swallows IntegrityError.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.dual_write import _binding_for, _space_binding_for
from qq_ai_bot.identity.sanitize import normalize_external_id
from qq_ai_bot.memory.partition import canonical_fact_owner_complete
from qq_ai_bot.persistence.models import (
    GroupModel,
    MemorySelfReflectionRunModel,
    MemorySelfReflectionStateModel,
    PersonModel,
)

logger = logging.getLogger(__name__)

MISSING_BINDING = "missing_binding"
AMBIGUOUS_OWNER = "ambiguous_owner"
REFLECTION_OWNER_UNIQUE = "reflection_owner_unique"
MIXED_DREAM_SOURCE = "mixed_dream_source"
INCOMPLETE_DREAM_SHAPE = "incomplete_dream_shape"

_UNRESOLVED_LOG_INTERVAL_SECONDS = 30.0
_last_unresolved_log: dict[str, float] = {}

OwnerPair = tuple[str | None, str | None]
DreamShape = tuple[str | None, str | None, str | None, str | None]


def reset_v1_owner_unresolved_log_for_tests() -> None:
    _last_unresolved_log.clear()


def log_v1_owner_unresolved(reason: str) -> None:
    """Content-free, rate-limited diagnostic. reason is a stable category."""

    now = time.monotonic()
    last = _last_unresolved_log.get(reason, 0.0)
    if now - last < _UNRESOLVED_LOG_INTERVAL_SECONDS:
        return
    _last_unresolved_log[reason] = now
    logger.info("c21_v1_owner_unresolved reason=%s", reason)


def _xor_pair(person_id: str | None, space_id: str | None) -> OwnerPair | None:
    if bool(person_id) == bool(space_id):
        return None
    return person_id, space_id


def _event_group_id(event: Any) -> str | None:
    group_id = getattr(event, "group_id", None)
    return str(group_id) if group_id else None


def _event_private_peer(event: Any) -> str | None:
    peer = getattr(event, "private_peer_user_id", None)
    if peer:
        return str(peer)
    direction = str(getattr(event, "direction", "") or "")
    sender = getattr(event, "sender_user_id", None)
    if direction == "inbound" and sender:
        return str(sender)
    return None


async def _unique_person_id(session: AsyncSession, external_id: str | None) -> OwnerPair | None:
    external = normalize_external_id(external_id) if external_id else None
    if external is None:
        log_v1_owner_unresolved(MISSING_BINDING)
        return None
    binding = await _binding_for(session, external)
    if binding is None:
        log_v1_owner_unresolved(MISSING_BINDING)
        return None
    people = await session.get(PersonModel, external)
    shadow = people.canonical_person_id if people is not None else None
    if shadow and shadow != binding.person_id:
        log_v1_owner_unresolved(AMBIGUOUS_OWNER)
        return None
    return binding.person_id, None


async def _unique_space_id(session: AsyncSession, external_id: str | None) -> OwnerPair | None:
    external = normalize_external_id(external_id) if external_id else None
    if external is None:
        log_v1_owner_unresolved(MISSING_BINDING)
        return None
    binding = await _space_binding_for(session, external)
    if binding is None:
        log_v1_owner_unresolved(MISSING_BINDING)
        return None
    group = await session.get(GroupModel, external)
    shadow = group.canonical_space_id if group is not None else None
    if shadow and shadow != binding.space_id:
        log_v1_owner_unresolved(AMBIGUOUS_OWNER)
        return None
    return None, binding.space_id


async def _conversation_owner(
    session: AsyncSession, conversation_id: str | None
) -> OwnerPair | None:
    if not conversation_id:
        return None
    conversation = await session.get(CanonicalConversationModel, conversation_id)
    if conversation is None:
        return None
    if conversation.kind == "private":
        return _xor_pair(conversation.person_id, None)
    if conversation.kind == "space":
        return _xor_pair(None, conversation.space_id)
    return None


async def optional_xor_owner_for_scope(
    session: AsyncSession,
    *,
    scope_type: str,
    group_id: str | None,
    private_peer_user_id: str | None,
) -> OwnerPair:
    """Resolve one Binding/SpaceBinding. Never creates rows. Never picks a winner."""

    if scope_type == "group":
        resolved = await _unique_space_id(session, group_id)
    else:
        resolved = await _unique_person_id(session, private_peer_user_id)
    if resolved is None:
        return None, None
    return resolved


async def optional_xor_owner_for_event(session: AsyncSession, event: Any | None) -> OwnerPair:
    """Optional Person XOR Space owner from existing Binding/SpaceBinding."""

    if event is None:
        log_v1_owner_unresolved(MISSING_BINDING)
        return None, None
    group_id = _event_group_id(event)
    scope_type = "group" if group_id else str(getattr(event, "scope_type", "") or "private")
    binding_owner = await optional_xor_owner_for_scope(
        session,
        scope_type=scope_type,
        group_id=group_id,
        private_peer_user_id=_event_private_peer(event),
    )
    if binding_owner == (None, None):
        return None, None
    conversation_owner = await _conversation_owner(
        session, getattr(event, "canonical_conversation_id", None)
    )
    if conversation_owner is not None and conversation_owner != binding_owner:
        log_v1_owner_unresolved(AMBIGUOUS_OWNER)
        return None, None
    return binding_owner


async def reflection_state_owner_taken(
    session: AsyncSession,
    person_id: str | None,
    space_id: str | None,
    *,
    exclude_state_id: int | None = None,
) -> bool:
    pair = _xor_pair(person_id, space_id)
    if pair is None:
        return False
    query = select(MemorySelfReflectionStateModel.id)
    if pair[0]:
        query = query.where(
            MemorySelfReflectionStateModel.canonical_person_id == pair[0],
            MemorySelfReflectionStateModel.canonical_space_id.is_(None),
        )
    else:
        query = query.where(
            MemorySelfReflectionStateModel.canonical_space_id == pair[1],
            MemorySelfReflectionStateModel.canonical_person_id.is_(None),
        )
    if exclude_state_id is not None:
        query = query.where(MemorySelfReflectionStateModel.id != exclude_state_id)
    return await session.scalar(query) is not None


async def reflection_run_owner_taken(
    session: AsyncSession,
    person_id: str | None,
    space_id: str | None,
    scheduled_slot: str,
    *,
    exclude_run_id: int | None = None,
) -> bool:
    pair = _xor_pair(person_id, space_id)
    if pair is None:
        return False
    query = select(MemorySelfReflectionRunModel.id)
    if pair[0]:
        query = query.where(
            MemorySelfReflectionRunModel.canonical_person_id == pair[0],
            MemorySelfReflectionRunModel.canonical_space_id.is_(None),
            MemorySelfReflectionRunModel.scheduled_slot == scheduled_slot,
        )
    else:
        query = query.where(
            MemorySelfReflectionRunModel.canonical_space_id == pair[1],
            MemorySelfReflectionRunModel.canonical_person_id.is_(None),
            MemorySelfReflectionRunModel.scheduled_slot == scheduled_slot,
        )
    if exclude_run_id is not None:
        query = query.where(MemorySelfReflectionRunModel.id != exclude_run_id)
    return await session.scalar(query) is not None


async def optional_reflection_state_owners(
    session: AsyncSession,
    *,
    scope_type: str,
    group_id: str | None,
    private_peer_user_id: str | None,
    exclude_state_id: int | None = None,
) -> OwnerPair:
    person_id, space_id = await optional_xor_owner_for_scope(
        session,
        scope_type=scope_type,
        group_id=group_id,
        private_peer_user_id=private_peer_user_id,
    )
    if person_id is None and space_id is None:
        return None, None
    if await reflection_state_owner_taken(
        session, person_id, space_id, exclude_state_id=exclude_state_id
    ):
        log_v1_owner_unresolved(REFLECTION_OWNER_UNIQUE)
        return None, None
    return person_id, space_id


async def optional_reflection_run_owners(
    session: AsyncSession,
    person_id: str | None,
    space_id: str | None,
    scheduled_slot: str,
) -> OwnerPair:
    if person_id is None and space_id is None:
        return None, None
    if await reflection_run_owner_taken(session, person_id, space_id, scheduled_slot):
        log_v1_owner_unresolved(REFLECTION_OWNER_UNIQUE)
        return None, None
    return person_id, space_id


def optional_dream_owner_from_facts(facts: tuple[Any, ...]) -> DreamShape | None:
    """Fill only when every source fact has a complete, identical canonical shape."""

    if not facts:
        log_v1_owner_unresolved(INCOMPLETE_DREAM_SHAPE)
        return None
    shapes: set[DreamShape] = set()
    for fact in facts:
        if not canonical_fact_owner_complete(fact):
            log_v1_owner_unresolved(INCOMPLETE_DREAM_SHAPE)
            return None
        shapes.add(
            (
                getattr(fact, "canonical_subject_person_id", None),
                getattr(fact, "canonical_subject_space_id", None),
                getattr(fact, "canonical_visibility_person_id", None),
                getattr(fact, "canonical_visibility_space_id", None),
            )
        )
    if len(shapes) != 1:
        log_v1_owner_unresolved(MIXED_DREAM_SOURCE)
        return None
    return next(iter(shapes))
