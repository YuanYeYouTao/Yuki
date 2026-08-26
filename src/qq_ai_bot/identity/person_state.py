"""Canonical Person, Space, alias, and membership state.

Transport-facing APIs accept QQ identifiers, resolve them through Bindings, and
store ownership exclusively as canonical UUIDs.  External identifiers never act
as database ownership keys in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.canonical_repository import (
    AccountRole,
    bindings_for_person,
    external_id,
    find_identity_binding,
    find_space_binding,
    representative_external_account_id,
    require_person_binding,
    require_space_binding,
)
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.models import (
    MembershipModel,
    PersonAliasModel,
    PersonRelationshipModel,
)
from qq_ai_bot.persistence.repository_records import GroupSetting, PrivateUserSetting


@dataclass(frozen=True, slots=True)
class CanonicalMemberNameProjection:
    user_id: str
    nickname: str
    group_card: str
    aliases: tuple[str, ...]


async def _live_person_binding(
    session: AsyncSession,
    user_id: str,
    *,
    allow_disabled: bool = False,
) -> IdentityBindingModel | None:
    binding = await find_identity_binding(session, external_id(user_id))
    if binding is None:
        return None
    if binding.status != "active":
        return None
    person = await session.get(CanonicalPersonModel, binding.person_id)
    if person is None:
        raise CanonicalIdentityError("unclassified")
    if not person.enabled and not allow_disabled:
        return None
    return binding


async def _live_space_binding(
    session: AsyncSession,
    group_id: str,
) -> SpaceBindingModel | None:
    binding = await find_space_binding(session, external_id(group_id))
    if binding is None or binding.status != "active":
        return None
    if await session.get(CanonicalSpaceModel, binding.space_id) is None:
        raise CanonicalIdentityError("unclassified")
    return binding


async def _nickname(
    session: AsyncSession,
    *,
    person_id: str,
    binding: IdentityBindingModel,
) -> str:
    alias = await session.scalar(
        select(PersonAliasModel.alias)
        .where(
            PersonAliasModel.canonical_person_id == person_id,
            PersonAliasModel.canonical_space_id.is_(None),
            PersonAliasModel.alias_type == "nickname",
        )
        .order_by(PersonAliasModel.last_seen_at.desc(), PersonAliasModel.id.desc())
        .limit(1)
    )
    if alias:
        return str(alias)
    if binding.display_name:
        return binding.display_name
    named = [
        row
        for row in await bindings_for_person(session, person_id)
        if row.status == "active" and row.display_name
    ]
    return named[-1].display_name if named else ""


async def _upsert_alias(
    session: AsyncSession,
    *,
    person_id: str,
    space_id: str | None,
    alias: str,
    alias_type: str,
    now: datetime,
) -> None:
    conditions = [
        PersonAliasModel.canonical_person_id == person_id,
        PersonAliasModel.alias == alias,
    ]
    conditions.append(
        PersonAliasModel.canonical_space_id.is_(None)
        if space_id is None
        else PersonAliasModel.canonical_space_id == space_id
    )
    row = await session.scalar(select(PersonAliasModel).where(*conditions))
    if row is None:
        session.add(
            PersonAliasModel(
                canonical_person_id=person_id,
                canonical_space_id=space_id,
                alias=alias[:128],
                alias_type=alias_type,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        return
    row.alias_type = alias_type
    row.last_seen_at = now


async def observe_canonical_person(
    session: AsyncSession,
    *,
    user_id: str,
    nickname: str,
    group_id: str | None,
    group_card: str,
    group_name: str,
    nickname_known: bool,
    group_card_known: bool,
    role: AccountRole,
    initial_affection: int,
    initial_trust: int,
    now: datetime,
) -> None:
    if role != "human":
        return
    binding = await require_person_binding(session, user_id)
    binding.last_seen_at = now
    if nickname_known and nickname:
        binding.display_name = nickname[:128]
        binding.updated_at = now
        binding.revision += 1
    if await session.get(PersonRelationshipModel, binding.person_id) is None:
        session.add(
            PersonRelationshipModel(
                canonical_person_id=binding.person_id,
                affection_score=initial_affection,
                trust_score=initial_trust,
                created_at=now,
                updated_at=now,
                last_automatic_change_at=None,
            )
        )
    if nickname:
        await _upsert_alias(
            session,
            person_id=binding.person_id,
            space_id=None,
            alias=nickname,
            alias_type="nickname",
            now=now,
        )
    if group_id is None:
        return
    space_binding = await require_space_binding(session, group_id, allow_disabled=True)
    space = await session.get(CanonicalSpaceModel, space_binding.space_id)
    if space is None:
        raise CanonicalIdentityError("unclassified")
    space_binding.last_seen_at = now
    if group_name:
        space.name = group_name[:128]
        space.updated_at = now
        space.revision += 1
        space_binding.display_name = group_name[:128]
        space_binding.updated_at = now
        space_binding.revision += 1
    membership = await session.get(
        MembershipModel,
        (binding.person_id, space_binding.space_id),
    )
    if membership is None:
        membership = MembershipModel(
            canonical_person_id=binding.person_id,
            canonical_space_id=space_binding.space_id,
            group_card=group_card if group_card_known else "",
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(membership)
    else:
        if group_card_known:
            membership.group_card = group_card[:128]
        membership.last_seen_at = now
    if group_card:
        await _upsert_alias(
            session,
            person_id=binding.person_id,
            space_id=space_binding.space_id,
            alias=group_card,
            alias_type="group_card",
            now=now,
        )


async def load_canonical_profile(
    session: AsyncSession,
    *,
    user_id: str,
    group_id: str | None,
) -> tuple[str, str] | None:
    binding = await _live_person_binding(session, user_id)
    if binding is None:
        return None
    card = ""
    if group_id is not None:
        space_binding = await _live_space_binding(session, group_id)
        if space_binding is None:
            return None
        membership = await session.get(
            MembershipModel,
            (binding.person_id, space_binding.space_id),
        )
        if membership is not None:
            card = membership.group_card
    return await _nickname(session, person_id=binding.person_id, binding=binding), card


async def load_canonical_aliases(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int,
) -> tuple[str, ...]:
    binding = await _live_person_binding(session, user_id)
    if binding is None:
        return ()
    values = (
        await session.scalars(
            select(PersonAliasModel.alias)
            .where(PersonAliasModel.canonical_person_id == binding.person_id)
            .order_by(PersonAliasModel.last_seen_at.desc(), PersonAliasModel.id.desc())
            .limit(max(1, limit))
        )
    ).all()
    return tuple(dict.fromkeys(str(value) for value in values))


async def load_canonical_membership_count(session: AsyncSession, user_id: str) -> int:
    binding = await _live_person_binding(session, user_id)
    if binding is None:
        return 0
    value = await session.scalar(
        select(func.count())
        .select_from(MembershipModel)
        .where(MembershipModel.canonical_person_id == binding.person_id)
    )
    return int(value or 0)


async def load_canonical_members_in_group(
    session: AsyncSession,
    user_ids: tuple[str, ...],
    group_id: str,
) -> frozenset[str]:
    space_binding = await require_space_binding(session, group_id, allow_disabled=True)
    matched: set[str] = set()
    for user_id in user_ids:
        binding = await _live_person_binding(session, user_id)
        if binding is None:
            continue
        if (
            await session.get(
                MembershipModel,
                (binding.person_id, space_binding.space_id),
            )
            is not None
        ):
            matched.add(user_id)
    return frozenset(matched)


async def _aliases_for_person(
    session: AsyncSession,
    person_id: str,
    *,
    space_id: str | None = None,
) -> tuple[str, ...]:
    statement = select(PersonAliasModel.alias).where(
        PersonAliasModel.canonical_person_id == person_id
    )
    if space_id is not None:
        statement = statement.where(
            (PersonAliasModel.canonical_space_id.is_(None))
            | (PersonAliasModel.canonical_space_id == space_id)
        )
    values = (
        await session.scalars(
            statement.order_by(
                PersonAliasModel.last_seen_at.desc(),
                PersonAliasModel.id.desc(),
            )
        )
    ).all()
    return tuple(dict.fromkeys(str(value) for value in values))


async def load_canonical_people_by_exact_name(
    session: AsyncSession,
    name: str,
) -> tuple[str, ...]:
    normalized = name.strip()
    if not normalized:
        return ()
    person_ids = set(
        await session.scalars(
            select(IdentityBindingModel.person_id).where(
                IdentityBindingModel.status == "active",
                IdentityBindingModel.display_name == normalized,
            )
        )
    )
    person_ids.update(
        await session.scalars(
            select(PersonAliasModel.canonical_person_id).where(PersonAliasModel.alias == normalized)
        )
    )
    enabled = set(
        await session.scalars(
            select(CanonicalPersonModel.id).where(
                CanonicalPersonModel.id.in_(person_ids),
                CanonicalPersonModel.enabled.is_(True),
            )
        )
    )
    projected: list[str] = []
    for person_id in enabled:
        bindings = await bindings_for_person(session, person_id)
        if any(row.status == "active" for row in bindings):
            projected.append(representative_external_account_id(bindings))
    return tuple(sorted(projected))


async def load_canonical_group_member_name_projections(
    session: AsyncSession,
    group_id: str,
) -> tuple[CanonicalMemberNameProjection, ...]:
    space_binding = await require_space_binding(session, group_id, allow_disabled=True)
    memberships = list(
        await session.scalars(
            select(MembershipModel).where(
                MembershipModel.canonical_space_id == space_binding.space_id
            )
        )
    )
    result: list[CanonicalMemberNameProjection] = []
    for membership in memberships:
        person = await session.get(CanonicalPersonModel, membership.canonical_person_id)
        if person is None or not person.enabled:
            continue
        bindings = await bindings_for_person(session, person.id)
        active = tuple(row for row in bindings if row.status == "active")
        if not active:
            continue
        representative_id = representative_external_account_id(active)
        representative = next(row for row in active if row.external_account_id == representative_id)
        result.append(
            CanonicalMemberNameProjection(
                user_id=representative_id,
                nickname=await _nickname(
                    session,
                    person_id=person.id,
                    binding=representative,
                ),
                group_card=membership.group_card,
                aliases=await _aliases_for_person(
                    session,
                    person.id,
                    space_id=space_binding.space_id,
                ),
            )
        )
    return tuple(sorted(result, key=lambda item: item.user_id))


async def load_canonical_group_members_by_exact_name(
    session: AsyncSession,
    name: str,
    group_id: str,
) -> tuple[str, ...]:
    normalized = name.strip()
    if not normalized:
        return ()
    result = []
    for projection in await load_canonical_group_member_name_projections(session, group_id):
        if normalized in {projection.nickname, projection.group_card, *projection.aliases}:
            result.append(projection.user_id)
    return tuple(sorted(result))


async def observe_canonical_space(
    session: AsyncSession,
    group_id: str,
    *,
    name: str,
    now: datetime,
) -> GroupSetting:
    binding = await require_space_binding(session, group_id, allow_disabled=True)
    space = await session.get(CanonicalSpaceModel, binding.space_id)
    if space is None:
        raise CanonicalIdentityError("unclassified")
    binding.last_seen_at = now
    if name:
        binding.display_name = name[:128]
        binding.updated_at = now
        binding.revision += 1
        space.name = name[:128]
        space.updated_at = now
        space.revision += 1
    return _group_setting(group_id, space)


async def load_canonical_group(
    session: AsyncSession,
    group_id: str,
) -> GroupSetting | None:
    binding = await _live_space_binding(session, group_id)
    if binding is None:
        return None
    space = await session.get(CanonicalSpaceModel, binding.space_id)
    if space is None:
        raise CanonicalIdentityError("unclassified")
    return _group_setting(group_id, space)


async def set_canonical_space_flags(
    session: AsyncSession,
    group_id: str,
    *,
    enabled: bool | None = None,
    autonomous_enabled: bool | None = None,
    now: datetime,
) -> GroupSetting:
    binding = await require_space_binding(session, group_id, allow_disabled=True)
    space = await session.get(CanonicalSpaceModel, binding.space_id)
    if space is None:
        raise CanonicalIdentityError("unclassified")
    if enabled is not None:
        space.enabled = enabled
    if autonomous_enabled is not None:
        space.autonomous_enabled = autonomous_enabled
    space.updated_at = now
    space.revision += 1
    return _group_setting(group_id, space)


def _group_setting(group_id: str, space: CanonicalSpaceModel) -> GroupSetting:
    return GroupSetting(
        group_id=external_id(group_id),
        enabled=bool(space.enabled),
        require_mention=bool(space.require_mention),
        autonomous_enabled=bool(space.autonomous_enabled),
        name=space.name,
    )


async def set_canonical_person_enabled(
    session: AsyncSession,
    user_id: str,
    enabled: bool,
    *,
    now: datetime,
) -> PrivateUserSetting:
    binding = await require_person_binding(session, user_id, allow_disabled=True)
    person = await session.get(CanonicalPersonModel, binding.person_id)
    if person is None:
        raise CanonicalIdentityError("unclassified")
    person.enabled = enabled
    person.updated_at = now
    person.revision += 1
    return PrivateUserSetting(user_id=external_id(user_id), enabled=enabled)


async def load_canonical_person_enabled(
    session: AsyncSession,
    user_id: str,
) -> PrivateUserSetting | None:
    binding = await _live_person_binding(session, user_id, allow_disabled=True)
    if binding is None:
        return None
    person = await session.get(CanonicalPersonModel, binding.person_id)
    if person is None:
        return None
    return PrivateUserSetting(user_id=external_id(user_id), enabled=bool(person.enabled))
