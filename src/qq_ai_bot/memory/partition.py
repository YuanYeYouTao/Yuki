"""Single transport-neutral canonical Memory partition helper.

Partitions are only person:{UUID4} or space:{UUID4}, resolved from an active
IdentityBinding / SpaceBinding.
Conversation UUID, ConversationScope bot:* keys, and Presence QQ are never
partition values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.canonical_repository import IDENTITY_PLATFORM, optional_external_id
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)

PERSON_PREFIX = "person:"
SPACE_PREFIX = "space:"
_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class MemoryPartitionResolutionError(Exception):
    """Fail-closed Memory owner resolution. reason is a stable category."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class MemoryPartition:
    value: str
    person_id: str | None
    space_id: str | None


@dataclass(frozen=True, slots=True)
class MemoryFactCanonicalOwners:
    subject_person_id: str | None
    subject_space_id: str | None
    visibility_person_id: str | None
    visibility_space_id: str | None


def is_uuid4(value: str) -> bool:
    if not _UUID4_RE.fullmatch(value):
        return False
    try:
        return UUID(value).version == 4
    except ValueError:
        return False


def format_canonical_memory_partition(
    *,
    person_id: str | None = None,
    space_id: str | None = None,
) -> str:
    if bool(person_id) == bool(space_id):
        raise MemoryPartitionResolutionError("owner_shape")
    owner = person_id or space_id
    assert owner is not None
    if not is_uuid4(owner):
        raise MemoryPartitionResolutionError("malformed_uuid")
    return f"{PERSON_PREFIX}{person_id}" if person_id else f"{SPACE_PREFIX}{space_id}"


def parse_canonical_memory_partition(value: str) -> MemoryPartition:
    raw = str(value or "").strip()
    if not raw:
        raise MemoryPartitionResolutionError("missing_owner")
    if raw.startswith(("private:", "group:", "bot:")):
        raise MemoryPartitionResolutionError("legacy_or_conversation_key")
    if is_uuid4(raw):
        raise MemoryPartitionResolutionError("conversation_uuid")
    if raw.startswith(PERSON_PREFIX):
        person_id = raw[len(PERSON_PREFIX) :]
        if not is_uuid4(person_id):
            raise MemoryPartitionResolutionError("malformed_uuid")
        return MemoryPartition(
            value=f"{PERSON_PREFIX}{person_id}", person_id=person_id, space_id=None
        )
    if raw.startswith(SPACE_PREFIX):
        space_id = raw[len(SPACE_PREFIX) :]
        if not is_uuid4(space_id):
            raise MemoryPartitionResolutionError("malformed_uuid")
        return MemoryPartition(value=f"{SPACE_PREFIX}{space_id}", person_id=None, space_id=space_id)
    raise MemoryPartitionResolutionError("malformed_partition")


def require_xor_scope_owner(
    *,
    group_id: str | None,
    private_peer_user_id: str | None,
) -> tuple[str | None, str | None]:
    if bool(group_id) == bool(private_peer_user_id):
        raise MemoryPartitionResolutionError("owner_shape")
    return group_id, private_peer_user_id


async def resolve_active_person_id(session: AsyncSession, user_id: str | None) -> str:
    external = optional_external_id(user_id)
    if external is None:
        raise MemoryPartitionResolutionError("missing_owner")
    bindings = list(
        await session.scalars(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == IDENTITY_PLATFORM,
                IdentityBindingModel.external_account_id == external,
                IdentityBindingModel.status == "active",
            )
        )
    )
    if not bindings:
        raise MemoryPartitionResolutionError("missing_owner")
    person_ids = {row.person_id for row in bindings}
    if len(person_ids) != 1:
        raise MemoryPartitionResolutionError("ambiguous_owner")
    person = await session.get(CanonicalPersonModel, bindings[0].person_id)
    if person is None or not person.enabled:
        raise MemoryPartitionResolutionError("missing_owner")
    return person.id


async def resolve_active_space_id(session: AsyncSession, group_id: str | None) -> str:
    external = optional_external_id(group_id)
    if external is None:
        raise MemoryPartitionResolutionError("missing_owner")
    bindings = list(
        await session.scalars(
            select(SpaceBindingModel).where(
                SpaceBindingModel.platform == IDENTITY_PLATFORM,
                SpaceBindingModel.external_space_id == external,
                SpaceBindingModel.status == "active",
            )
        )
    )
    if not bindings:
        raise MemoryPartitionResolutionError("missing_owner")
    space_ids = {row.space_id for row in bindings}
    if len(space_ids) != 1:
        raise MemoryPartitionResolutionError("ambiguous_owner")
    space = await session.get(CanonicalSpaceModel, bindings[0].space_id)
    if space is None or not space.enabled:
        raise MemoryPartitionResolutionError("missing_owner")
    return space.id


async def _reject_conversation_uuid(session: AsyncSession, owner_id: str) -> None:
    if await session.get(CanonicalConversationModel, owner_id) is not None:
        raise MemoryPartitionResolutionError("conversation_uuid")


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


def _event_scope_owners(event: Any) -> tuple[str | None, str | None]:
    group_id = _event_group_id(event)
    return group_id, None if group_id else _event_private_peer(event)


async def resolve_canonical_memory_partition_for_event(
    session: AsyncSession,
    event: Any,
) -> MemoryPartition:
    group_id, peer = _event_scope_owners(event)
    if group_id:
        space_id = await resolve_active_space_id(session, group_id)
        await _reject_conversation_uuid(session, space_id)
        return MemoryPartition(
            value=format_canonical_memory_partition(space_id=space_id),
            person_id=None,
            space_id=space_id,
        )
    person_id = await resolve_active_person_id(session, peer)
    await _reject_conversation_uuid(session, person_id)
    return MemoryPartition(
        value=format_canonical_memory_partition(person_id=person_id),
        person_id=person_id,
        space_id=None,
    )


async def resolve_memory_partition_for_event(
    session: AsyncSession,
    event: Any,
) -> MemoryPartition:
    return await resolve_canonical_memory_partition_for_event(session, event)


async def resolve_canonical_memory_partition_from_scope(
    session: AsyncSession,
    *,
    group_id: str | None,
    private_peer_user_id: str | None,
) -> MemoryPartition:
    group_id, private_peer_user_id = require_xor_scope_owner(
        group_id=group_id,
        private_peer_user_id=private_peer_user_id,
    )
    if group_id:
        space_id = await resolve_active_space_id(session, group_id)
        await _reject_conversation_uuid(session, space_id)
        return MemoryPartition(
            value=format_canonical_memory_partition(space_id=space_id),
            person_id=None,
            space_id=space_id,
        )
    person_id = await resolve_active_person_id(session, private_peer_user_id)
    await _reject_conversation_uuid(session, person_id)
    return MemoryPartition(
        value=format_canonical_memory_partition(person_id=person_id),
        person_id=person_id,
        space_id=None,
    )


async def resolve_memory_partition_from_scope(
    session: AsyncSession,
    *,
    group_id: str | None,
    private_peer_user_id: str | None,
) -> MemoryPartition:
    return await resolve_canonical_memory_partition_from_scope(
        session,
        group_id=group_id,
        private_peer_user_id=private_peer_user_id,
    )


async def resolve_optional_active_person_id(
    session: AsyncSession,
    user_id: str | None,
) -> str | None:
    if user_id is None or not str(user_id).strip():
        return None
    return await resolve_active_person_id(session, user_id)


async def resolve_optional_active_space_id(
    session: AsyncSession,
    group_id: str | None,
) -> str | None:
    if group_id is None or not str(group_id).strip():
        return None
    return await resolve_active_space_id(session, group_id)


async def resolve_fact_canonical_owners(
    session: AsyncSession,
    fact: Any,
) -> MemoryFactCanonicalOwners:
    scope_type = str(getattr(fact, "scope_type", "") or "")
    scope_value = getattr(scope_type, "value", scope_type)
    visibility_type = getattr(fact, "visibility_type", None)
    visibility_value = getattr(visibility_type, "value", visibility_type)
    if scope_value == "self":
        if visibility_value in {None, "global"}:
            return MemoryFactCanonicalOwners(None, None, None, None)
        if visibility_value == "private":
            person_id = await resolve_active_person_id(
                session, getattr(fact, "visibility_user_id", None)
            )
            return MemoryFactCanonicalOwners(None, None, person_id, None)
        if visibility_value == "group":
            space_id = await resolve_active_space_id(
                session, getattr(fact, "visibility_group_id", None)
            )
            return MemoryFactCanonicalOwners(None, None, None, space_id)
        raise MemoryPartitionResolutionError("self_visibility_shape")
    if scope_value == "person":
        person_id = await resolve_active_person_id(session, getattr(fact, "subject_user_id", None))
        return MemoryFactCanonicalOwners(person_id, None, None, None)
    if scope_value == "group":
        space_id = await resolve_active_space_id(session, getattr(fact, "group_id", None))
        return MemoryFactCanonicalOwners(None, space_id, None, None)
    if scope_value == "person_group":
        person_id = await resolve_active_person_id(session, getattr(fact, "subject_user_id", None))
        space_id = await resolve_active_space_id(session, getattr(fact, "group_id", None))
        return MemoryFactCanonicalOwners(person_id, space_id, None, None)
    raise MemoryPartitionResolutionError("fact_scope_shape")


def require_xor_memory_owner(
    person_id: str | None,
    space_id: str | None,
) -> tuple[str | None, str | None]:
    if bool(person_id) == bool(space_id):
        raise MemoryPartitionResolutionError("owner_shape")
    return person_id, space_id


def canonical_fact_owner_complete(fact: Any) -> bool:
    """True only for the four-state canonical Memory fact owner shape."""

    scope_type = str(getattr(fact.scope_type, "value", fact.scope_type) or "")
    visibility_type = getattr(fact, "visibility_type", None)
    visibility_value = getattr(visibility_type, "value", visibility_type)
    subject_person = getattr(fact, "canonical_subject_person_id", None)
    subject_space = getattr(fact, "canonical_subject_space_id", None)
    visibility_person = getattr(fact, "canonical_visibility_person_id", None)
    visibility_space = getattr(fact, "canonical_visibility_space_id", None)
    extra_subjects = (subject_person, subject_space)
    extra_visibility = (visibility_person, visibility_space)
    if scope_type == "self":
        if visibility_value in {None, "global"}:
            return not any((*extra_subjects, *extra_visibility))
        if visibility_value == "private":
            return bool(visibility_person) and not any((*extra_subjects, visibility_space))
        if visibility_value == "group":
            return bool(visibility_space) and not any((*extra_subjects, visibility_person))
        return False
    if scope_type == "person":
        return bool(subject_person) and not any((subject_space, *extra_visibility))
    if scope_type == "group":
        return bool(subject_space) and not any((subject_person, *extra_visibility))
    if scope_type == "person_group":
        return bool(subject_person) and bool(subject_space) and not any(extra_visibility)
    return False


dream_canonical_owner_complete = canonical_fact_owner_complete
