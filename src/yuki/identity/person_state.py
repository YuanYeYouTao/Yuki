"""Canonical Person, Space, alias, and membership state.

Transport-facing APIs accept QQ identifiers, resolve them through Bindings, and
store ownership exclusively as canonical UUIDs.  External identifiers never act
as database ownership keys in this module.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.domain.messages import InboundMessage, sanitize_display_name
from yuki.identity.canonical_repository import (
    external_id,
    find_space_binding,
)
from yuki.identity.db_models import (
    AccountAssignmentModel,
    AccountModel,
    AccountNameObservationModel,
    AccountSpaceProfileModel,
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.person_identity import (
    current_person_id,
    person_family_condition,
)
from yuki.persistence.models import MembershipModel, PersonAliasModel
from yuki.persistence.repository_records import GroupSetting, PrivateUserSetting


class ObservationChanged(CanonicalIdentityError):
    """The original observation needs fresh database preparation, not a replayed effect."""

    def __init__(self) -> None:
        super().__init__("account_observation_changed")


PreparedPersonObservation = Callable[[AsyncSession], Awaitable[Callable[[], None]]]


@dataclass(frozen=True, slots=True)
class CanonicalMemberNameProjection:
    user_id: str
    nickname: str
    group_card: str
    aliases: tuple[str, ...]
    person_id: str | None = None


async def prepare_message_account_observations(
    session: AsyncSession,
    message: InboundMessage,
    *,
    space_binding_id: str | None,
    now: datetime,
    assignment_id: str | None = None,
) -> tuple[InboundMessage, PreparedPersonObservation]:
    """Prepare account/name facts without dirtying the admission read snapshot."""
    from yuki.identity.ownership_interval import assignments_at

    natives = tuple(
        dict.fromkeys(
            (
                message.sender.user_id,
                *(mention.target_user_id for mention in message.mentions if mention.target_user_id),
            )
        )
    )
    accounts = {
        row.external_account_id: row
        for row in await session.scalars(
            select(AccountModel).where(
                AccountModel.platform == message.platform,
                AccountModel.external_account_id.in_(natives),
            )
        )
    }
    new_accounts = []
    for native in natives:
        if native not in accounts:
            new_account = AccountModel(
                id=str(uuid4()),
                platform=message.platform,
                external_account_id=native,
                display_name="",
                created_at=now,
                updated_at=now,
            )
            accounts[native] = new_account
            new_accounts.append(new_account)
    ids = tuple(account.id for account in accounts.values())
    stages = await assignments_at(session, ids, message.occurred_at)
    sender_account = accounts[message.sender.user_id]
    sender_stage = (
        await session.get(AccountAssignmentModel, assignment_id)
        if assignment_id
        else stages.get(sender_account.id)
    )
    moment = message.occurred_at or now
    values: list[tuple[str, str | None, str | None, str, str]] = []
    sender_stage_id = sender_stage.id if sender_stage else None
    if message.sender.nickname:
        values.append(
            (sender_account.id, sender_stage_id, None, message.sender.nickname, "nickname")
        )
    if message.sender.native_username:
        values.append(
            (
                sender_account.id,
                sender_stage_id,
                None,
                message.sender.native_username,
                "native_username",
            )
        )
    if space_binding_id and message.sender.group_card:
        values.append(
            (
                sender_account.id,
                sender_stage_id,
                space_binding_id,
                message.sender.group_card,
                "group_card",
            )
        )
    mentions = []
    for mention in message.mentions:
        account = accounts.get(mention.target_user_id or "")
        if account is None:
            mentions.append(mention)
            continue
        stage = stages.get(account.id)
        mentions.append(
            replace(
                mention,
                account_id=account.id,
                assignment_id=stage.id if stage else None,
                person_id=stage.person_id if stage else None,
                presence_id=stage.id if stage and stage.kind == "yuki" else None,
                kind="yuki" if stage and stage.kind == "yuki" else "member",
            )
        )
        source = message.segments[mention.segment_index]
        data = source.get("data", {})
        if source.get("type") == "mention" and isinstance(data, dict):
            source = data.get("source", {})
            data = source.get("data", {}) if isinstance(source, dict) else {}
        if isinstance(data, dict):
            native_name = data.get("display_name") or data.get("name")
            if native_name:
                values.append(
                    (
                        account.id,
                        stage.id if stage else None,
                        space_binding_id,
                        str(native_name),
                        "native_mention",
                    )
                )
            if data.get("native_username"):
                values.append(
                    (
                        account.id,
                        stage.id if stage else None,
                        None,
                        str(data["native_username"]),
                        "native_username",
                    )
                )
    prepared = {}
    for account_id, stage_id, scope, name, kind in values:
        name = sanitize_display_name(name)
        if name:
            prepared[(account_id, stage_id, scope, name)] = kind
    source_ids = {native: account.id for native, account in accounts.items()}
    new_values = {
        account.external_account_id: {
            "id": account.id,
            "platform": account.platform,
            "external_account_id": account.external_account_id,
            "display_name": account.display_name,
            "created_at": account.created_at,
            "updated_at": account.updated_at,
        }
        for account in new_accounts
    }
    source_stages = {
        identity: (stage.id, stage.kind, stage.person_id) if stage is not None else None
        for identity, stage in stages.items()
    }

    observation_keys = json.dumps(
        [
            (account_id, stage_id or "", scope or "", name)
            for account_id, stage_id, scope, name in prepared
        ]
    )

    async def bind(active: AsyncSession) -> Callable[[], None]:
        # Preparation crosses sessions only as original IDs and values. These
        # bounded lookups resolve the writer's own rows before its first mutation.
        accounts = {
            row.external_account_id: row
            for row in await active.scalars(
                select(AccountModel).where(
                    AccountModel.platform == message.platform,
                    AccountModel.external_account_id.in_(natives),
                )
            )
        }
        new_accounts = []
        for native, identity in source_ids.items():
            existing = accounts.get(native)
            if native in new_values:
                if existing is not None:
                    raise ObservationChanged()
                account = AccountModel(**new_values[native])
                accounts[native] = account
                new_accounts.append(account)
            elif existing is None or existing.id != identity:
                raise ObservationChanged()
        current_stages = await assignments_at(active, ids, message.occurred_at)
        if {
            identity: (stage.id, stage.kind, stage.person_id) if stage is not None else None
            for identity, stage in current_stages.items()
        } != source_stages:
            raise ObservationChanged()
        sender_account = accounts[message.sender.user_id]
        sender_stage = (
            await active.get(AccountAssignmentModel, assignment_id)
            if assignment_id
            else current_stages.get(sender_account.id)
        )
        profiles = (
            {
                row.account_id: row
                for row in await active.scalars(
                    select(AccountSpaceProfileModel).where(
                        AccountSpaceProfileModel.account_id.in_(ids),
                        AccountSpaceProfileModel.space_binding_id == space_binding_id,
                    )
                )
            }
            if space_binding_id
            else {}
        )
        observations = {
            (row.account_id, row.assignment_id, row.space_binding_id, row.name): row
            for row in (
                await active.scalars(
                    select(AccountNameObservationModel).from_statement(
                        text(
                            "SELECT observed.* FROM json_each(:keys) AS wanted "
                            "CROSS JOIN account_name_observations AS observed "
                            "WHERE observed.account_id=json_extract(wanted.value,'$[0]') "
                            "AND coalesce(observed.assignment_id,'')="
                            "json_extract(wanted.value,'$[1]') "
                            "AND coalesce(observed.space_binding_id,'')="
                            "json_extract(wanted.value,'$[2]') "
                            "AND observed.name=json_extract(wanted.value,'$[3]')"
                        )
                    ),
                    {"keys": observation_keys},
                )
                if prepared
                else ()
            )
        }

        def apply() -> None:
            active.add_all(new_accounts)
            for key, kind in prepared.items():
                row = observations.get(key)
                if row is None:
                    account_id, stage_id, scope, name = key
                    active.add(
                        AccountNameObservationModel(
                            account_id=account_id,
                            assignment_id=stage_id,
                            space_binding_id=scope,
                            name=name,
                            kind=kind,
                            first_seen_at=moment,
                            last_seen_at=moment,
                        )
                    )
                else:
                    row.first_seen_at = min(_utc(row.first_seen_at), _utc(moment))
                    row.last_seen_at = max(_utc(row.last_seen_at), _utc(moment))
            if sender_stage is None or sender_stage.status != "ended":
                if (
                    message.sender.nickname_known
                    and message.sender.nickname
                    and (
                        not sender_account.display_name
                        or _utc(moment) >= _utc(sender_account.updated_at)
                    )
                ):
                    sender_account.display_name = sanitize_display_name(message.sender.nickname)
                    sender_account.updated_at = moment
            if space_binding_id:
                for account in accounts.values():
                    profile = profiles.get(account.id)
                    if profile is None:
                        active.add(
                            AccountSpaceProfileModel(
                                account_id=account.id,
                                space_binding_id=space_binding_id,
                                display_name=message.sender.group_card
                                if (
                                    account.id == sender_account.id
                                    and message.sender.group_card_known
                                    and (sender_stage is None or sender_stage.status != "ended")
                                )
                                else "",
                                first_seen_at=moment,
                                last_seen_at=moment,
                            )
                        )
                    else:
                        if (
                            account.id == sender_account.id
                            and message.sender.group_card_known
                            and (sender_stage is None or sender_stage.status != "ended")
                            and _utc(moment) >= _utc(profile.last_seen_at)
                        ):
                            profile.display_name = sanitize_display_name(message.sender.group_card)
                        profile.first_seen_at = min(_utc(profile.first_seen_at), _utc(moment))
                        profile.last_seen_at = max(_utc(profile.last_seen_at), _utc(moment))

        return apply

    return replace(message, mentions=tuple(mentions)), bind


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def observe_canonical_person(
    session: AsyncSession,
    *,
    binding: IdentityBindingModel,
    nickname: str,
    group_id: str | None,
    group_card: str,
    group_name: str,
    nickname_known: bool,
    group_card_known: bool,
    now: datetime,
    space_id: str | None = None,
) -> None:
    prepared = await prepare_canonical_person_observation(
        session,
        binding=binding,
        nickname=nickname,
        group_id=group_id,
        group_card=group_card,
        group_name=group_name,
        nickname_known=nickname_known,
        group_card_known=group_card_known,
        now=now,
        space_id=space_id,
    )
    space_binding = (
        await find_space_binding(session, group_id, platform=binding.platform) if group_id else None
    )
    profile = (
        await session.get(AccountSpaceProfileModel, (binding.account_id, space_binding.id))
        if space_binding
        else None
    )
    names = {
        (row.space_binding_id, row.name): row
        for row in await session.scalars(
            select(AccountNameObservationModel).where(
                AccountNameObservationModel.account_id == binding.account_id,
                AccountNameObservationModel.assignment_id == binding.id,
                AccountNameObservationModel.space_binding_id.is_(None)
                | (
                    AccountNameObservationModel.space_binding_id == space_binding.id
                    if space_binding
                    else False
                ),
            )
        )
    }
    apply = await prepared(session)
    apply()
    if nickname_known and nickname:
        binding.account.display_name = sanitize_display_name(nickname)
        binding.account.updated_at = now
    if space_binding:
        if profile is None:
            session.add(
                AccountSpaceProfileModel(
                    account_id=binding.account_id,
                    space_binding_id=space_binding.id,
                    display_name=group_card if group_card_known else "",
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        else:
            if group_card_known:
                profile.display_name = sanitize_display_name(group_card)
            profile.last_seen_at = now
    for name, scope, kind in (
        (nickname, None, "nickname"),
        (group_card, space_binding.id if space_binding else None, "group_card"),
    ):
        name = sanitize_display_name(name)
        if not name or (kind == "group_card" and space_binding is None):
            continue
        row = names.get((scope, name))
        if row is None:
            session.add(
                AccountNameObservationModel(
                    account_id=binding.account_id,
                    assignment_id=binding.id,
                    space_binding_id=scope,
                    name=name,
                    kind=kind,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        else:
            row.last_seen_at = now


async def prepare_canonical_person_observation(
    session: AsyncSession,
    *,
    binding: IdentityBindingModel,
    nickname: str,
    group_id: str | None,
    group_card: str,
    group_name: str,
    nickname_known: bool,
    group_card_known: bool,
    now: datetime,
    space_id: str | None = None,
) -> PreparedPersonObservation:
    """Read the original owners and names before the event claims its receipt."""

    person = await session.get(CanonicalPersonModel, binding.person_id)
    if person is None:
        raise CanonicalIdentityError("unclassified")
    space_binding = None
    space = None
    if group_id is not None:
        space_binding = await find_space_binding(session, group_id, platform=binding.platform)
        if space_binding is not None:
            if space_id is not None and space_binding.space_id != space_id:
                raise CanonicalIdentityError("canonical_owner_mismatch")
            space = await session.get(CanonicalSpaceModel, space_binding.space_id)
    elif space_id is not None:
        space = await session.get(CanonicalSpaceModel, space_id)
    original_binding = (binding.id, binding.account_id, binding.person_id)
    original_space = space.id if space is not None else None
    original_space_binding = space_binding.id if space_binding is not None else None

    async def bind(active: AsyncSession) -> Callable[[], None]:
        binding = await active.get(IdentityBindingModel, original_binding[0])
        if (
            binding is None
            or (binding.id, binding.account_id, binding.person_id) != original_binding
        ):
            raise ObservationChanged()
        person = await active.get(CanonicalPersonModel, binding.person_id)
        if person is None:
            raise ObservationChanged()
        current = await active.get(CanonicalPersonModel, await current_person_id(active, person.id))
        if current is None:
            raise ObservationChanged()
        space = await active.get(CanonicalSpaceModel, original_space) if original_space else None
        space_binding = (
            await active.get(SpaceBindingModel, original_space_binding)
            if original_space_binding
            else None
        )
        if original_space is not None and (
            space is None
            or (
                original_space_binding is not None
                and (space_binding is None or space_binding.space_id != original_space)
            )
        ):
            raise ObservationChanged()
        membership = (
            await active.get(MembershipModel, (binding.person_id, original_space))
            if original_space is not None
            else None
        )

        def apply() -> None:
            if not current.display_name and nickname:
                current.display_name = nickname[:128]
                current.updated_at = now
                current.revision += 1
            binding.last_seen_at = now
            if space is None:
                return
            apply_canonical_space_observation(space, space_binding, name=group_name, now=now)
            if membership is None:
                active.add(
                    MembershipModel(
                        canonical_person_id=binding.person_id,
                        canonical_space_id=space.id,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            else:
                membership.last_seen_at = now

        return apply

    return bind


def apply_canonical_space_observation(
    space: CanonicalSpaceModel,
    binding: SpaceBindingModel | None,
    *,
    name: str,
    now: datetime,
) -> None:
    """Apply only local event metadata, leaving the Space's policy unchanged."""

    if binding is not None:
        binding.last_seen_at = now
    if name:
        if space.name != name[:128]:
            space.name = name[:128]
            space.updated_at = now
            space.revision += 1
        if binding is not None and binding.display_name != name[:128]:
            binding.display_name = name[:128]
            binding.updated_at = now
            binding.revision += 1


async def load_canonical_profile(
    session: AsyncSession,
    *,
    user_id: str | None = None,
    group_id: str | None = None,
    include_nickname: bool = True,
    person_id: str | None = None,
    space_id: str | None = None,
    platform: str = "qq",
) -> tuple[str, str] | None:
    if person_id is not None:
        person = await session.get(CanonicalPersonModel, person_id)
        if person is None:
            return None
        person = await session.get(
            CanonicalPersonModel, await current_person_id(session, person.id)
        )
        assert person is not None
        nickname = person.display_name if include_nickname else ""
        card = ""
        return str(nickname or ""), str(card or "")
    if user_id is None:
        return None
    return (
        await load_canonical_profiles(
            session,
            (user_id,),
            group_id=group_id,
            include_nickname=include_nickname,
            person_ids={user_id: person_id} if person_id is not None else None,
            platform=platform,
            space_id=space_id,
        )
    ).get(user_id)


async def load_canonical_profiles(
    session: AsyncSession,
    user_ids: tuple[str, ...],
    *,
    group_id: str | None,
    include_nickname: bool = True,
    person_ids: Mapping[str, str] | None = None,
    members_only: bool = False,
    platform: str = "qq",
    space_id: str | None = None,
) -> dict[str, tuple[str, str]]:
    """Read requested profiles, reusing canonical owners from this read snapshot."""
    accounts = {raw: external_id(raw) for raw in user_ids}
    if not accounts:
        return {}
    rows = (
        await session.execute(
            select(AccountModel, AccountAssignmentModel)
            .join(AccountAssignmentModel, AccountAssignmentModel.account_id == AccountModel.id)
            .where(
                AccountModel.platform == platform,
                AccountModel.external_account_id.in_(set(accounts.values())),
                AccountAssignmentModel.status != "ended",
            )
        )
    ).all()
    people = {
        account.external_account_id: (account, assignment)
        for account, assignment in rows
        if person_ids is None or person_ids.get(account.external_account_id) == assignment.person_id
    }
    space_binding = (
        await find_space_binding(session, group_id, platform=platform) if group_id else None
    )
    cards = {}
    if space_binding is not None:
        cards = {
            row.account_id: row.display_name
            for row in await session.scalars(
                select(AccountSpaceProfileModel).where(
                    AccountSpaceProfileModel.space_binding_id == space_binding.id,
                    AccountSpaceProfileModel.account_id.in_(
                        [account.id for account, _ in people.values()]
                    ),
                )
            )
        }
    return {
        raw: (account.display_name if include_nickname else "", cards.get(account.id, ""))
        for raw, native in accounts.items()
        if native in people
        for account, assignment in [people[native]]
        if not members_only or account.id in cards
    }


async def load_canonical_aliases(
    session: AsyncSession,
    person_id: str | None,
    *,
    limit: int,
) -> tuple[str, ...]:
    person = await session.get(CanonicalPersonModel, person_id) if person_id else None
    if person is None:
        return ()
    values = (
        await session.scalars(
            select(PersonAliasModel.alias)
            .where(person_family_condition(PersonAliasModel.canonical_person_id, person.id))
            .group_by(PersonAliasModel.alias)
            .order_by(
                func.max(PersonAliasModel.last_seen_at).desc(), func.max(PersonAliasModel.id).desc()
            )
            .limit(max(1, limit))
        )
    ).all()
    return tuple(dict.fromkeys(str(value) for value in values))


async def load_canonical_membership_count(session: AsyncSession, person_id: str | None) -> int:
    person = await session.get(CanonicalPersonModel, person_id) if person_id else None
    if person is None:
        return 0
    value = await session.scalar(
        select(func.count(func.distinct(MembershipModel.canonical_space_id))).where(
            person_family_condition(MembershipModel.canonical_person_id, person.id)
        )
    )
    return int(value or 0)


async def load_canonical_members_in_group(
    session: AsyncSession,
    user_ids: tuple[str, ...],
    group_id: str,
    *,
    person_ids: Mapping[str, str] | None = None,
) -> frozenset[str]:
    values = await load_canonical_profiles(
        session,
        user_ids,
        group_id=group_id,
        person_ids=person_ids,
        members_only=True,
        include_nickname=False,
    )
    return frozenset(values)


async def load_canonical_group_member_name_projections(
    session: AsyncSession,
    group_id: str,
    *,
    space_id: str | None = None,
    platform: str = "qq",
) -> tuple[CanonicalMemberNameProjection, ...]:
    binding = await find_space_binding(session, group_id, platform=platform)
    if binding is None or (space_id is not None and binding.space_id != space_id):
        return ()
    rows = (
        await session.execute(
            select(AccountModel, AccountSpaceProfileModel, AccountAssignmentModel.person_id)
            .join(AccountSpaceProfileModel, AccountSpaceProfileModel.account_id == AccountModel.id)
            .outerjoin(
                AccountAssignmentModel,
                (AccountAssignmentModel.account_id == AccountModel.id)
                & (AccountAssignmentModel.status != "ended"),
            )
            .where(AccountSpaceProfileModel.space_binding_id == binding.id)
            .order_by(AccountModel.id)
        )
    ).all()
    account_ids = tuple(account.id for account, _, _ in rows)
    aliases: dict[str, dict[str, None]] = {}
    for account_id, name in (
        await session.execute(
            select(AccountNameObservationModel.account_id, AccountNameObservationModel.name)
            .where(
                AccountNameObservationModel.account_id.in_(account_ids),
                AccountNameObservationModel.space_binding_id.is_(None)
                | (AccountNameObservationModel.space_binding_id == binding.id),
            )
            .order_by(AccountNameObservationModel.id)
        )
    ).all():
        aliases.setdefault(account_id, {})[name] = None
    return tuple(
        CanonicalMemberNameProjection(
            account.external_account_id,
            account.display_name,
            profile.display_name,
            tuple(aliases.get(account.id, {})),
            person_id,
        )
        for account, profile, person_id in rows
    )


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


async def load_canonical_group(
    session: AsyncSession,
    space_id: str,
) -> GroupSetting | None:
    space = await session.get(CanonicalSpaceModel, space_id)
    if space is None:
        return None
    return _group_setting(space.id, space)


async def set_canonical_space_flags(
    session: AsyncSession,
    space_id: str,
    *,
    enabled: bool | None = None,
    autonomous_enabled: bool | None = None,
    now: datetime,
) -> GroupSetting:
    space = await session.get(CanonicalSpaceModel, space_id)
    if space is None:
        raise CanonicalIdentityError("missing_canonical_owner")
    if enabled is not None:
        space.enabled = enabled
    if autonomous_enabled is not None:
        space.autonomous_enabled = autonomous_enabled
    space.updated_at = now
    space.revision += 1
    return _group_setting(space.id, space)


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
    person_id: str,
    enabled: bool,
    *,
    now: datetime,
) -> PrivateUserSetting:
    person = await session.get(CanonicalPersonModel, await current_person_id(session, person_id))
    if person is None:
        raise CanonicalIdentityError("missing_canonical_owner")
    person.enabled = enabled
    person.updated_at = now
    person.revision += 1
    return PrivateUserSetting(user_id=person.id, enabled=enabled)


async def load_canonical_person_enabled(
    session: AsyncSession,
    person_id: str,
) -> PrivateUserSetting | None:
    person = await session.get(CanonicalPersonModel, person_id)
    if person is None:
        return None
    person = await session.get(CanonicalPersonModel, await current_person_id(session, person_id))
    assert person is not None
    return PrivateUserSetting(user_id=person.id, enabled=bool(person.enabled))
