"""Fill existing canonical shadow columns. No new schema."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from qq_ai_bot.identity.dual_write import (
    _binding_for,
    _presence_for,
    _shadow_conflict,
    _space_binding_for,
)
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.identity.runtime import require_identity_runtime
from qq_ai_bot.identity.sanitize import normalize_external_id
from qq_ai_bot.persistence.models import ChatEventModel, GroupModel, PersonModel


def _external(raw: str | None) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    return normalize_external_id(str(raw))


async def person_id_for(session: AsyncSession, user_id: str | None) -> str | None:
    if user_id is None:
        return None
    external = _external(user_id)
    if external is None:
        return None
    people = await session.get(PersonModel, external)
    if people is not None and people.canonical_person_id:
        return people.canonical_person_id
    binding = await _binding_for(session, external)
    return None if binding is None else binding.person_id


async def space_id_for(session: AsyncSession, group_id: str | None) -> str | None:
    if group_id is None:
        return None
    external = _external(group_id)
    if external is None:
        return None
    group = await session.get(GroupModel, external)
    if group is not None and group.canonical_space_id:
        return group.canonical_space_id
    binding = await _space_binding_for(session, external)
    return None if binding is None else binding.space_id


async def active_person_id_for(session: AsyncSession, user_id: str | None) -> str | None:
    """Resolve Person only from an existing active IdentityBinding."""

    if user_id is None:
        return None
    external = _external(user_id)
    if external is None:
        return None
    binding = await _binding_for(session, external)
    if binding is None or binding.status != "active":
        return None
    person = await session.get(CanonicalPersonModel, binding.person_id)
    if person is None or not person.enabled:
        return None
    return person.id


async def active_space_id_for(session: AsyncSession, group_id: str | None) -> str | None:
    """Resolve Space only from an existing active SpaceBinding."""

    if group_id is None:
        return None
    external = _external(group_id)
    if external is None:
        return None
    binding = await _space_binding_for(session, external)
    if binding is None or binding.status != "active":
        return None
    space = await session.get(CanonicalSpaceModel, binding.space_id)
    if space is None or not space.enabled:
        return None
    return space.id


async def presence_id_for(session: AsyncSession, bot_user_id: str | None) -> str | None:
    if bot_user_id is None:
        return None
    external = _external(bot_user_id)
    if external is None:
        return None
    presence = await _presence_for(session, external)
    return None if presence is None else presence.id


async def assign_shadow(
    current: str | None,
    proven: str | None,
) -> str | None:
    _shadow_conflict(current, proven)
    if proven is None:
        return current
    return current if current is not None else proven


async def fill_person_space_shadows(
    session: AsyncSession,
    row: Any,
    *,
    person_attr: str | None,
    space_attr: str | None,
    user_id: str | None,
    group_id: str | None,
) -> None:
    await require_identity_runtime(session, allowed=frozenset({"v1", "v2"}))
    if person_attr is not None and hasattr(row, person_attr):
        proven = await person_id_for(session, user_id)
        current = getattr(row, person_attr)
        setattr(row, person_attr, await assign_shadow(current, proven))
    if space_attr is not None and hasattr(row, space_attr):
        proven = await space_id_for(session, group_id)
        current = getattr(row, space_attr)
        setattr(row, space_attr, await assign_shadow(current, proven))


async def fill_memory_fact_shadows(session: AsyncSession, row: Any) -> None:
    await require_identity_runtime(session, allowed=frozenset({"v1", "v2"}))
    scope_type = str(getattr(row, "scope_type", "") or "")
    subject_user_id = getattr(row, "subject_user_id", None)
    group_id = getattr(row, "group_id", None)
    visibility_type = str(getattr(row, "visibility_type", "") or "")
    if scope_type in {"person", "person_group", "self"}:
        proven = await person_id_for(session, subject_user_id)
        current = getattr(row, "canonical_subject_person_id", None)
        row.canonical_subject_person_id = await assign_shadow(current, proven)
    if scope_type in {"group", "person_group"}:
        proven = await space_id_for(session, group_id)
        current = getattr(row, "canonical_subject_space_id", None)
        row.canonical_subject_space_id = await assign_shadow(current, proven)
    if visibility_type == "private":
        proven = await person_id_for(session, getattr(row, "visibility_user_id", None))
        current = getattr(row, "canonical_visibility_person_id", None)
        row.canonical_visibility_person_id = await assign_shadow(current, proven)
    if visibility_type == "group":
        proven = await space_id_for(session, getattr(row, "visibility_group_id", None))
        current = getattr(row, "canonical_visibility_space_id", None)
        row.canonical_visibility_space_id = await assign_shadow(current, proven)


async def conversation_id_for_event(session: AsyncSession, event_id: int | None) -> str | None:
    if event_id is None:
        return None
    event = await session.get(ChatEventModel, event_id)
    if event is None:
        return None
    return event.canonical_conversation_id


async def fill_presence_shadow(
    session: AsyncSession,
    row: Any,
    *,
    attr: str,
    bot_user_id: str | None,
) -> None:
    await require_identity_runtime(session, allowed=frozenset({"v1", "v2"}))
    if not hasattr(row, attr):
        raise IdentityDualWriteError("unclassified")
    proven = await presence_id_for(session, bot_user_id)
    current = getattr(row, attr)
    setattr(row, attr, await assign_shadow(current, proven))
