"""complete-v2 reads/writes through Binding → Person/Space. No people/groups rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.dual_write import AccountRole, _external_id
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.identity.event_author import complete_v2_account_is_person
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.runtime import require_complete_v2_runtime
from qq_ai_bot.identity.shadows import fill_person_space_shadows
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MembershipModel,
    PersonAliasModel,
    PersonRelationshipModel,
    PersonTimeSettingModel,
    RelationshipEventModel,
    RuntimeConfigOverrideModel,
)
from qq_ai_bot.persistence.repository_records import GroupSetting, PrivateUserSetting
from qq_ai_bot.speech.db_models import PersonSpeechPreferenceModel

_NON_PERSON_AUTHORS = frozenset(
    {
        AuthorKind.YUKI.value,
        AuthorKind.EXTERNAL_BOT.value,
        AuthorKind.SYSTEM.value,
    }
)


@dataclass(frozen=True, slots=True)
class CanonicalMemberNameProjection:
    """One canonical Person projected to a deterministic representative QQ id."""

    user_id: str
    nickname: str
    group_card: str
    aliases: tuple[str, ...]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_person_storage_key(person_id: str) -> str:
    """Deterministic Person-owned storage key, independent of external QQ."""

    return person_id


def canonical_relationship_storage_key(person_id: str) -> str:
    """Deterministic person_relationships.user_id when no cutover row exists."""

    return canonical_person_storage_key(person_id)


async def require_person_binding(
    session: AsyncSession,
    user_id: str,
    *,
    allow_disabled: bool = False,
) -> IdentityBindingModel:
    await require_complete_v2_runtime(session)
    external = _external_id(user_id)
    rows = list(
        await session.scalars(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == IDENTITY_PLATFORM,
                IdentityBindingModel.external_account_id == external,
            )
        )
    )
    active = [row for row in rows if row.status == "active"]
    if len(active) != 1:
        raise IdentityDualWriteError("unclassified")
    person = await session.get(CanonicalPersonModel, active[0].person_id)
    if person is None:
        raise IdentityDualWriteError("unclassified")
    if not person.enabled and not allow_disabled:
        raise IdentityDualWriteError("canonical_owner_disabled")
    return active[0]


async def require_space_binding(session: AsyncSession, group_id: str) -> SpaceBindingModel:
    await require_complete_v2_runtime(session)
    external = _external_id(group_id)
    rows = list(
        await session.scalars(
            select(SpaceBindingModel).where(
                SpaceBindingModel.platform == IDENTITY_PLATFORM,
                SpaceBindingModel.external_space_id == external,
            )
        )
    )
    active = [row for row in rows if row.status == "active"]
    if len(active) != 1:
        raise IdentityDualWriteError("no_space_binding")
    space = await session.get(CanonicalSpaceModel, active[0].space_id)
    if space is None:
        raise IdentityDualWriteError("unclassified")
    return active[0]


async def bindings_for_person(
    session: AsyncSession, person_id: str
) -> tuple[IdentityBindingModel, ...]:
    rows = list(
        await session.scalars(
            select(IdentityBindingModel).where(IdentityBindingModel.person_id == person_id)
        )
    )
    return tuple(rows)


def _active_bindings(
    bindings: tuple[IdentityBindingModel, ...],
) -> tuple[IdentityBindingModel, ...]:
    return tuple(item for item in bindings if item.status == "active")


def representative_external_account_id(
    bindings: tuple[IdentityBindingModel, ...],
) -> str:
    """Deterministic public QQ id for one Person: min(active external_account_id)."""

    active = _active_bindings(bindings)
    if not active:
        raise IdentityDualWriteError("unclassified")
    return min(item.external_account_id for item in active)


async def owner_keys_for_person(session: AsyncSession, person_id: str) -> tuple[str, ...]:
    externals = tuple(
        item.external_account_id for item in await bindings_for_person(session, person_id)
    )
    storage = canonical_relationship_storage_key(person_id)
    return tuple(dict.fromkeys((*externals, storage)))


async def canonical_relationship_owner_keys(
    session: AsyncSession, user_id: str
) -> tuple[str, tuple[str, ...]]:
    binding = await require_person_binding(session, user_id)
    return binding.person_id, await owner_keys_for_person(session, binding.person_id)


async def resolve_person_author_id_for_event(
    session: AsyncSession, event: ChatEventModel
) -> str | None:
    """Return Person id for a Person-authored event.

    Yuki / external_bot / system return None so callers can refuse the job.
    Missing or conflicting Person authorship fail closed.
    """

    await require_complete_v2_runtime(session)
    kind = event.author_kind
    if kind in _NON_PERSON_AUTHORS:
        return None
    if kind != AuthorKind.PERSON.value:
        raise IdentityDualWriteError("unclassified")
    if event.author_person_id:
        person = await session.get(CanonicalPersonModel, event.author_person_id)
        if person is None:
            raise IdentityDualWriteError("unclassified")
        sender = event.sender_user_id
        if sender:
            rows = list(
                await session.scalars(
                    select(IdentityBindingModel).where(
                        IdentityBindingModel.platform == IDENTITY_PLATFORM,
                        IdentityBindingModel.external_account_id == _external_id(sender),
                    )
                )
            )
            if rows:
                active = [row for row in rows if row.status == "active"]
                if len(active) != 1:
                    raise IdentityDualWriteError("unclassified")
                if active[0].person_id != event.author_person_id:
                    raise IdentityDualWriteError("canonical_owner_mismatch")
        return event.author_person_id
    binding = await require_person_binding(session, event.sender_user_id)
    return binding.person_id


async def _relationship_rows_for_person(
    session: AsyncSession,
    *,
    person_id: str,
    owner_keys: tuple[str, ...],
) -> tuple[PersonRelationshipModel, ...]:
    found: dict[str, PersonRelationshipModel] = {}
    if owner_keys:
        for row in await session.scalars(
            select(PersonRelationshipModel).where(PersonRelationshipModel.user_id.in_(owner_keys))
        ):
            found[row.user_id] = row
    for row in await session.scalars(
        select(PersonRelationshipModel).where(
            PersonRelationshipModel.canonical_person_id == person_id
        )
    ):
        found[row.user_id] = row
    return tuple(found.values())


def _reject_inconsistent_relationship_rows(
    rows: tuple[PersonRelationshipModel, ...],
    *,
    person_id: str,
) -> PersonRelationshipModel | None:
    if not rows:
        return None
    if len(rows) > 1:
        raise IdentityDualWriteError("canonical_owner_mismatch")
    row = rows[0]
    if row.canonical_person_id not in {None, person_id}:
        raise IdentityDualWriteError("canonical_owner_mismatch")
    return row


async def resolve_canonical_relationship(
    session: AsyncSession,
    user_id: str,
    *,
    create: bool,
    initial_affection: int,
    initial_trust: int,
    now: datetime | None = None,
) -> PersonRelationshipModel | None:
    if not await complete_v2_account_is_person(session, user_id):
        return None
    binding = await require_person_binding(session, user_id)
    person_id = binding.person_id
    owner_keys = tuple(
        dict.fromkeys(
            (
                *(
                    item.external_account_id
                    for item in await bindings_for_person(session, person_id)
                ),
                canonical_relationship_storage_key(person_id),
            )
        )
    )
    rows = await _relationship_rows_for_person(session, person_id=person_id, owner_keys=owner_keys)
    existing = _reject_inconsistent_relationship_rows(rows, person_id=person_id)
    if existing is not None:
        await fill_person_space_shadows(
            session,
            existing,
            person_attr="canonical_person_id",
            space_attr=None,
            user_id=_external_id(user_id),
            group_id=None,
        )
        if existing.canonical_person_id not in {None, person_id}:
            raise IdentityDualWriteError("canonical_owner_mismatch")
        existing.canonical_person_id = person_id
        return existing
    if not create:
        return None
    timestamp = now or _utcnow()
    storage_key = canonical_relationship_storage_key(person_id)
    await session.execute(
        insert(PersonRelationshipModel)
        .values(
            user_id=storage_key,
            affection_score=initial_affection,
            trust_score=initial_trust,
            created_at=timestamp,
            updated_at=timestamp,
            last_automatic_change_at=None,
            canonical_person_id=person_id,
        )
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    created = await session.get(PersonRelationshipModel, storage_key)
    if created is None:
        raise IdentityDualWriteError("unclassified")
    await fill_person_space_shadows(
        session,
        created,
        person_attr="canonical_person_id",
        space_attr=None,
        user_id=_external_id(user_id),
        group_id=None,
    )
    again = await _relationship_rows_for_person(session, person_id=person_id, owner_keys=owner_keys)
    chosen = _reject_inconsistent_relationship_rows(again, person_id=person_id)
    if chosen is None:
        raise IdentityDualWriteError("unclassified")
    return chosen


async def _time_setting_rows_for_person(
    session: AsyncSession,
    *,
    person_id: str,
    owner_keys: tuple[str, ...],
) -> tuple[PersonTimeSettingModel, ...]:
    found: dict[str, PersonTimeSettingModel] = {}
    if owner_keys:
        for row in await session.scalars(
            select(PersonTimeSettingModel).where(PersonTimeSettingModel.user_id.in_(owner_keys))
        ):
            found[row.user_id] = row
    for row in await session.scalars(
        select(PersonTimeSettingModel).where(
            PersonTimeSettingModel.canonical_person_id == person_id
        )
    ):
        found[row.user_id] = row
    return tuple(found.values())


def _reject_inconsistent_time_settings(
    rows: tuple[PersonTimeSettingModel, ...],
    *,
    person_id: str,
) -> PersonTimeSettingModel | None:
    if not rows:
        return None
    if len(rows) > 1:
        raise IdentityDualWriteError("canonical_owner_mismatch")
    row = rows[0]
    if row.canonical_person_id not in {None, person_id}:
        raise IdentityDualWriteError("canonical_owner_mismatch")
    return row


async def resolve_canonical_time_setting(
    session: AsyncSession,
    user_id: str,
    *,
    timezone: str | None = None,
    now: datetime | None = None,
) -> PersonTimeSettingModel | None:
    binding = await require_person_binding(session, user_id)
    person_id = binding.person_id
    owner_keys = await owner_keys_for_person(session, person_id)
    existing = _reject_inconsistent_time_settings(
        await _time_setting_rows_for_person(session, person_id=person_id, owner_keys=owner_keys),
        person_id=person_id,
    )
    caller = _external_id(user_id)
    if existing is not None:
        if timezone is not None:
            existing.timezone = timezone
            existing.updated_at = now or _utcnow()
        await fill_person_space_shadows(
            session,
            existing,
            person_attr="canonical_person_id",
            space_attr=None,
            user_id=caller,
            group_id=None,
        )
        if existing.canonical_person_id not in {None, person_id}:
            raise IdentityDualWriteError("canonical_owner_mismatch")
        existing.canonical_person_id = person_id
        return existing
    if timezone is None:
        return None
    timestamp = now or _utcnow()
    storage_key = canonical_person_storage_key(person_id)
    await session.execute(
        insert(PersonTimeSettingModel)
        .values(
            user_id=storage_key,
            timezone=timezone,
            created_at=timestamp,
            updated_at=timestamp,
        )
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    created = await session.get(PersonTimeSettingModel, storage_key)
    if created is None:
        raise IdentityDualWriteError("unclassified")
    await fill_person_space_shadows(
        session,
        created,
        person_attr="canonical_person_id",
        space_attr=None,
        user_id=caller,
        group_id=None,
    )
    chosen = _reject_inconsistent_time_settings(
        await _time_setting_rows_for_person(session, person_id=person_id, owner_keys=owner_keys),
        person_id=person_id,
    )
    if chosen is None:
        raise IdentityDualWriteError("unclassified")
    chosen.canonical_person_id = person_id
    return chosen


async def _speech_preference_rows_for_person(
    session: AsyncSession,
    *,
    person_id: str,
    owner_keys: tuple[str, ...],
) -> tuple[PersonSpeechPreferenceModel, ...]:
    found: dict[str, PersonSpeechPreferenceModel] = {}
    if owner_keys:
        for row in await session.scalars(
            select(PersonSpeechPreferenceModel).where(
                PersonSpeechPreferenceModel.user_id.in_(owner_keys)
            )
        ):
            found[row.user_id] = row
    for row in await session.scalars(
        select(PersonSpeechPreferenceModel).where(
            PersonSpeechPreferenceModel.canonical_person_id == person_id
        )
    ):
        found[row.user_id] = row
    return tuple(found.values())


def _reject_inconsistent_speech_preferences(
    rows: tuple[PersonSpeechPreferenceModel, ...],
    *,
    person_id: str,
) -> PersonSpeechPreferenceModel | None:
    if not rows:
        return None
    if len(rows) > 1:
        raise IdentityDualWriteError("canonical_owner_mismatch")
    row = rows[0]
    if row.canonical_person_id not in {None, person_id}:
        raise IdentityDualWriteError("canonical_owner_mismatch")
    return row


async def resolve_canonical_speech_preference(
    session: AsyncSession,
    user_id: str,
    *,
    create: bool,
    mode: str | None = None,
    source_message_id: str = "",
    now: datetime | None = None,
) -> PersonSpeechPreferenceModel | None:
    binding = await require_person_binding(session, user_id)
    person_id = binding.person_id
    owner_keys = await owner_keys_for_person(session, person_id)
    existing = _reject_inconsistent_speech_preferences(
        await _speech_preference_rows_for_person(
            session, person_id=person_id, owner_keys=owner_keys
        ),
        person_id=person_id,
    )
    caller = _external_id(user_id)
    timestamp = now or _utcnow()
    if existing is not None:
        if create:
            if mode is None:
                raise IdentityDualWriteError("unclassified")
            existing.mode = mode
            existing.source_message_id = source_message_id[:128]
            existing.updated_at = timestamp
        await fill_person_space_shadows(
            session,
            existing,
            person_attr="canonical_person_id",
            space_attr=None,
            user_id=caller,
            group_id=None,
        )
        if existing.canonical_person_id not in {None, person_id}:
            raise IdentityDualWriteError("canonical_owner_mismatch")
        existing.canonical_person_id = person_id
        return existing
    if not create:
        return None
    if mode is None:
        raise IdentityDualWriteError("unclassified")
    storage_key = canonical_person_storage_key(person_id)
    row = PersonSpeechPreferenceModel(
        user_id=storage_key,
        mode=mode,
        source_message_id=source_message_id[:128],
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(row)
    await session.flush()
    await fill_person_space_shadows(
        session,
        row,
        person_attr="canonical_person_id",
        space_attr=None,
        user_id=caller,
        group_id=None,
    )
    chosen = _reject_inconsistent_speech_preferences(
        await _speech_preference_rows_for_person(
            session, person_id=person_id, owner_keys=owner_keys
        ),
        person_id=person_id,
    )
    if chosen is None:
        raise IdentityDualWriteError("unclassified")
    chosen.canonical_person_id = person_id
    return chosen


@dataclass(frozen=True, slots=True)
class CanonicalUserConfigScope:
    """One Person's user-scoped runtime-config storage identity."""

    person_id: str
    storage_scope_id: str
    owner_keys: tuple[str, ...]


async def load_canonical_user_config_overrides(
    session: AsyncSession,
    *,
    person_id: str,
    owner_keys: tuple[str, ...],
) -> tuple[RuntimeConfigOverrideModel, ...]:
    found: dict[tuple[str, str], RuntimeConfigOverrideModel] = {}
    conditions = [RuntimeConfigOverrideModel.canonical_person_id == person_id]
    if owner_keys:
        conditions.append(RuntimeConfigOverrideModel.scope_id.in_(owner_keys))
    for row in await session.scalars(
        select(RuntimeConfigOverrideModel).where(
            RuntimeConfigOverrideModel.scope_type == "user",
            or_(*conditions),
        )
    ):
        found[(row.config_key, row.scope_id)] = row
    return tuple(found.values())


def _reject_inconsistent_user_config_rows(
    rows: tuple[RuntimeConfigOverrideModel, ...],
    *,
    person_id: str,
) -> tuple[RuntimeConfigOverrideModel, ...]:
    keys: list[str] = []
    scope_ids: set[str] = set()
    for row in rows:
        if row.scope_type != "user":
            raise IdentityDualWriteError("canonical_owner_mismatch")
        if row.canonical_person_id not in {None, person_id}:
            raise IdentityDualWriteError("canonical_owner_mismatch")
        keys.append(row.config_key)
        scope_ids.add(row.scope_id)
    if len(keys) != len(set(keys)) or len(scope_ids) > 1:
        raise IdentityDualWriteError("canonical_owner_mismatch")
    return rows


async def resolve_canonical_user_config_scope(
    session: AsyncSession,
    scope_id: str,
) -> CanonicalUserConfigScope:
    await require_complete_v2_runtime(session)
    person = await session.get(CanonicalPersonModel, scope_id)
    if person is None:
        binding = await require_person_binding(session, scope_id)
        person_id = binding.person_id
    else:
        if not person.enabled:
            raise IdentityDualWriteError("canonical_owner_disabled")
        person_id = person.id
    owner_keys = await owner_keys_for_person(session, person_id)
    rows = _reject_inconsistent_user_config_rows(
        await load_canonical_user_config_overrides(
            session, person_id=person_id, owner_keys=owner_keys
        ),
        person_id=person_id,
    )
    storage = next(
        (row.scope_id for row in rows),
        canonical_person_storage_key(person_id),
    )
    return CanonicalUserConfigScope(
        person_id=person_id,
        storage_scope_id=storage,
        owner_keys=owner_keys,
    )


async def _memberships_for_person_space(
    session: AsyncSession,
    *,
    person_id: str,
    space_id: str,
    owner_keys: tuple[str, ...],
    group_id: str,
) -> tuple[MembershipModel, ...]:
    found: dict[tuple[str, str], MembershipModel] = {}
    for row in await session.scalars(
        select(MembershipModel).where(
            or_(
                and_(
                    MembershipModel.canonical_person_id == person_id,
                    MembershipModel.canonical_space_id == space_id,
                ),
                and_(
                    MembershipModel.user_id.in_(owner_keys),
                    MembershipModel.group_id == group_id,
                ),
            )
        )
    ):
        found[(row.user_id, row.group_id)] = row
    return tuple(found.values())


def _reject_inconsistent_memberships(
    rows: tuple[MembershipModel, ...],
    *,
    person_id: str,
    space_id: str,
) -> MembershipModel | None:
    if not rows:
        return None
    if len(rows) > 1:
        raise IdentityDualWriteError("canonical_owner_mismatch")
    row = rows[0]
    if row.canonical_person_id not in {None, person_id}:
        raise IdentityDualWriteError("canonical_owner_mismatch")
    if row.canonical_space_id not in {None, space_id}:
        raise IdentityDualWriteError("canonical_owner_mismatch")
    return row


async def _projected_nickname(
    session: AsyncSession,
    *,
    person_id: str,
    binding: IdentityBindingModel,
) -> str:
    alias = await session.scalar(
        select(PersonAliasModel.alias)
        .where(
            PersonAliasModel.canonical_person_id == person_id,
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
        item
        for item in await bindings_for_person(session, person_id)
        if item.status == "active" and item.display_name
    ]
    if not named:
        return ""
    named.sort(key=lambda item: (item.updated_at, item.external_account_id, item.id))
    return named[-1].display_name


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
    caller = _external_id(user_id)
    if nickname_known and nickname:
        binding.display_name = nickname[:128]
        binding.updated_at = now
        binding.revision = int(binding.revision) + 1
    await resolve_canonical_relationship(
        session,
        user_id,
        create=True,
        initial_affection=initial_affection,
        initial_trust=initial_trust,
        now=now,
    )
    if nickname:
        await _upsert_alias(
            session,
            storage_user_id=canonical_relationship_storage_key(binding.person_id),
            shadow_user_id=caller,
            group_scope="",
            alias=nickname,
            alias_type="nickname",
            now=now,
            person_id=binding.person_id,
            space_id=None,
        )
    if group_id is None:
        return
    space_binding = await require_space_binding(session, group_id)
    space = await session.get(CanonicalSpaceModel, space_binding.space_id)
    if space is None:
        raise IdentityDualWriteError("unclassified")
    group_external = _external_id(group_id)
    if group_name:
        space.name = group_name[:128]
        space.updated_at = now
        space.revision = int(space.revision) + 1
        space_binding.display_name = group_name[:128]
        space_binding.updated_at = now
        space_binding.revision = int(space_binding.revision) + 1
    owner_keys = (
        *tuple(
            item.external_account_id
            for item in await bindings_for_person(session, binding.person_id)
        ),
        canonical_relationship_storage_key(binding.person_id),
    )
    memberships = await _memberships_for_person_space(
        session,
        person_id=binding.person_id,
        space_id=space_binding.space_id,
        owner_keys=owner_keys,
        group_id=group_external,
    )
    membership = _reject_inconsistent_memberships(
        memberships, person_id=binding.person_id, space_id=space_binding.space_id
    )
    if membership is None:
        storage_user_id = canonical_relationship_storage_key(binding.person_id)
        await session.execute(
            insert(MembershipModel)
            .values(
                user_id=storage_user_id,
                group_id=group_external,
                group_card=group_card if group_card_known else "",
                first_seen_at=now,
                last_seen_at=now,
                canonical_person_id=binding.person_id,
                canonical_space_id=space_binding.space_id,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "group_id"])
        )
        again = await _memberships_for_person_space(
            session,
            person_id=binding.person_id,
            space_id=space_binding.space_id,
            owner_keys=owner_keys,
            group_id=group_external,
        )
        membership = _reject_inconsistent_memberships(
            again, person_id=binding.person_id, space_id=space_binding.space_id
        )
        if membership is None:
            raise IdentityDualWriteError("unclassified")
    if group_card_known:
        membership.group_card = group_card
    membership.last_seen_at = now
    await fill_person_space_shadows(
        session,
        membership,
        person_attr="canonical_person_id",
        space_attr="canonical_space_id",
        user_id=caller,
        group_id=group_external,
    )
    if group_card:
        await _upsert_alias(
            session,
            storage_user_id=canonical_relationship_storage_key(binding.person_id),
            shadow_user_id=caller,
            group_scope=group_external,
            alias=group_card,
            alias_type="group_card",
            now=now,
            person_id=binding.person_id,
            space_id=space_binding.space_id,
        )


async def _upsert_alias(
    session: AsyncSession,
    *,
    storage_user_id: str,
    shadow_user_id: str,
    group_scope: str,
    alias: str,
    alias_type: str,
    now: datetime,
    person_id: str,
    space_id: str | None,
) -> None:
    statement = insert(PersonAliasModel).values(
        user_id=storage_user_id,
        group_scope=group_scope,
        alias=alias,
        alias_type=alias_type,
        first_seen_at=now,
        last_seen_at=now,
        canonical_person_id=person_id,
        canonical_space_id=space_id,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[
                PersonAliasModel.user_id,
                PersonAliasModel.group_scope,
                PersonAliasModel.alias,
            ],
            set_={"alias_type": alias_type, "last_seen_at": now},
        )
    )
    row = await session.scalar(
        select(PersonAliasModel).where(
            PersonAliasModel.user_id == storage_user_id,
            PersonAliasModel.group_scope == group_scope,
            PersonAliasModel.alias == alias,
        )
    )
    if row is None:
        raise IdentityDualWriteError("unclassified")
    await fill_person_space_shadows(
        session,
        row,
        person_attr="canonical_person_id",
        space_attr="canonical_space_id",
        user_id=shadow_user_id,
        group_id=group_scope or None,
    )


async def load_canonical_profile(
    session: AsyncSession,
    *,
    user_id: str,
    group_id: str | None,
) -> tuple[str, str] | None:
    if not await complete_v2_account_is_person(session, user_id):
        return None
    binding = await require_person_binding(session, user_id)
    card = ""
    if group_id is not None:
        space_binding = await require_space_binding(session, group_id)
        owner_keys = (
            *tuple(
                item.external_account_id
                for item in await bindings_for_person(session, binding.person_id)
            ),
            canonical_relationship_storage_key(binding.person_id),
        )
        memberships = await _memberships_for_person_space(
            session,
            person_id=binding.person_id,
            space_id=space_binding.space_id,
            owner_keys=owner_keys,
            group_id=_external_id(group_id),
        )
        membership = _reject_inconsistent_memberships(
            memberships, person_id=binding.person_id, space_id=space_binding.space_id
        )
        if membership is not None:
            card = membership.group_card
    return await _projected_nickname(session, person_id=binding.person_id, binding=binding), card


async def load_canonical_aliases(
    session: AsyncSession, user_id: str, *, limit: int
) -> tuple[str, ...]:
    if not await complete_v2_account_is_person(session, user_id):
        return ()
    binding = await require_person_binding(session, user_id)
    values = (
        await session.scalars(
            select(PersonAliasModel.alias)
            .where(PersonAliasModel.canonical_person_id == binding.person_id)
            .order_by(PersonAliasModel.last_seen_at.desc())
            .limit(limit)
        )
    ).all()
    return tuple(dict.fromkeys(values))


async def load_canonical_membership_count(session: AsyncSession, user_id: str) -> int:
    if not await complete_v2_account_is_person(session, user_id):
        return 0
    binding = await require_person_binding(session, user_id)
    owner_keys = (
        *tuple(
            item.external_account_id
            for item in await bindings_for_person(session, binding.person_id)
        ),
        canonical_relationship_storage_key(binding.person_id),
    )
    rows = list(
        await session.scalars(
            select(MembershipModel).where(
                or_(
                    MembershipModel.canonical_person_id == binding.person_id,
                    MembershipModel.user_id.in_(owner_keys),
                )
            )
        )
    )
    owners = {row.canonical_person_id for row in rows}
    if any(owner not in {None, binding.person_id} for owner in owners):
        raise IdentityDualWriteError("canonical_owner_mismatch")
    spaces = {(row.canonical_space_id, row.group_id) for row in rows}
    return len(spaces)


async def load_canonical_members_in_group(
    session: AsyncSession,
    user_ids: tuple[str, ...],
    group_id: str,
) -> frozenset[str]:
    space_binding = await require_space_binding(session, group_id)
    group_external = _external_id(group_id)
    matched: set[str] = set()
    for item in user_ids:
        if not await complete_v2_account_is_person(session, item):
            continue
        binding = await require_person_binding(session, item)
        owner_keys = (
            *tuple(
                row.external_account_id
                for row in await bindings_for_person(session, binding.person_id)
            ),
            canonical_relationship_storage_key(binding.person_id),
        )
        memberships = await _memberships_for_person_space(
            session,
            person_id=binding.person_id,
            space_id=space_binding.space_id,
            owner_keys=owner_keys,
            group_id=group_external,
        )
        membership = _reject_inconsistent_memberships(
            memberships, person_id=binding.person_id, space_id=space_binding.space_id
        )
        if membership is not None:
            matched.add(item)
    return frozenset(matched)


async def load_canonical_relationship_events(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int,
) -> tuple[RelationshipEventModel, ...]:
    person_id, owner_keys = await canonical_relationship_owner_keys(session, user_id)
    rows = (
        await session.scalars(
            select(RelationshipEventModel)
            .where(
                or_(
                    RelationshipEventModel.canonical_person_id == person_id,
                    RelationshipEventModel.user_id.in_(owner_keys),
                )
            )
            .order_by(
                RelationshipEventModel.created_at.desc(),
                RelationshipEventModel.id.desc(),
            )
            .limit(max(1, min(limit, 100)))
        )
    ).all()
    return tuple(rows)


async def observe_canonical_space(
    session: AsyncSession,
    group_id: str,
    *,
    name: str,
    now: datetime,
) -> GroupSetting:
    binding = await require_space_binding(session, group_id)
    space = await session.get(CanonicalSpaceModel, binding.space_id)
    if space is None:
        raise IdentityDualWriteError("unclassified")
    if name:
        space.name = name[:128]
        space.updated_at = now
        space.revision = int(space.revision) + 1
        binding.display_name = name[:128]
        binding.updated_at = now
        binding.revision = int(binding.revision) + 1
    return GroupSetting(
        group_id=_external_id(group_id),
        enabled=bool(space.enabled),
        require_mention=bool(space.require_mention),
        autonomous_enabled=bool(space.autonomous_enabled),
        name=space.name,
    )


async def load_canonical_group(session: AsyncSession, group_id: str) -> GroupSetting | None:
    await require_complete_v2_runtime(session)
    external = _external_id(group_id)
    rows = list(
        await session.scalars(
            select(SpaceBindingModel).where(
                SpaceBindingModel.platform == IDENTITY_PLATFORM,
                SpaceBindingModel.external_space_id == external,
            )
        )
    )
    if not rows:
        return None
    active = [row for row in rows if row.status == "active"]
    if len(active) != 1:
        raise IdentityDualWriteError("no_space_binding")
    space = await session.get(CanonicalSpaceModel, active[0].space_id)
    if space is None:
        raise IdentityDualWriteError("unclassified")
    return GroupSetting(
        group_id=external,
        enabled=bool(space.enabled),
        require_mention=bool(space.require_mention),
        autonomous_enabled=bool(space.autonomous_enabled),
        name=space.name,
    )


async def set_canonical_space_flags(
    session: AsyncSession,
    group_id: str,
    *,
    enabled: bool | None = None,
    autonomous_enabled: bool | None = None,
    now: datetime,
) -> GroupSetting:
    binding = await require_space_binding(session, group_id)
    space = await session.get(CanonicalSpaceModel, binding.space_id)
    if space is None:
        raise IdentityDualWriteError("unclassified")
    if enabled is not None:
        space.enabled = enabled
    if autonomous_enabled is not None:
        space.autonomous_enabled = autonomous_enabled
    space.updated_at = now
    space.revision = int(space.revision) + 1
    return GroupSetting(
        group_id=_external_id(group_id),
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
        raise IdentityDualWriteError("unclassified")
    person.enabled = enabled
    person.updated_at = now
    person.revision = int(person.revision) + 1
    return PrivateUserSetting(user_id=_external_id(user_id), enabled=enabled)


async def load_canonical_person_enabled(
    session: AsyncSession, user_id: str
) -> PrivateUserSetting | None:
    await require_complete_v2_runtime(session)
    external = _external_id(user_id)
    rows = list(
        await session.scalars(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == IDENTITY_PLATFORM,
                IdentityBindingModel.external_account_id == external,
            )
        )
    )
    if not rows:
        return None
    active = [row for row in rows if row.status == "active"]
    if len(active) != 1:
        raise IdentityDualWriteError("unclassified")
    person = await session.get(CanonicalPersonModel, active[0].person_id)
    if person is None:
        return None
    return PrivateUserSetting(user_id=external, enabled=bool(person.enabled))


async def _assert_projection_owner(
    session: AsyncSession,
    storage_or_external: str,
    person_id: str,
) -> None:
    rows = list(
        await session.scalars(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == IDENTITY_PLATFORM,
                IdentityBindingModel.external_account_id == _external_id(storage_or_external),
            )
        )
    )
    if not rows:
        return
    active = [row for row in rows if row.status == "active"]
    if len(active) != 1:
        raise IdentityDualWriteError("unclassified")
    if active[0].person_id != person_id:
        raise IdentityDualWriteError("canonical_owner_mismatch")


async def _live_person_ids(session: AsyncSession, person_ids: set[str]) -> set[str]:
    if not person_ids:
        return set()
    return set(
        await session.scalars(
            select(CanonicalPersonModel.id).where(
                CanonicalPersonModel.id.in_(person_ids),
                CanonicalPersonModel.enabled.is_(True),
            )
        )
    )


async def _project_person_external(session: AsyncSession, person_id: str) -> str | None:
    person = await session.get(CanonicalPersonModel, person_id)
    if person is None:
        raise IdentityDualWriteError("unclassified")
    if not person.enabled:
        return None
    bindings = await bindings_for_person(session, person_id)
    if not _active_bindings(bindings):
        return None
    return representative_external_account_id(bindings)


async def _person_ids_matching_exact_name(session: AsyncSession, name: str) -> set[str]:
    person_ids: set[str] = set()
    for row in await session.scalars(
        select(IdentityBindingModel).where(
            IdentityBindingModel.platform == IDENTITY_PLATFORM,
            IdentityBindingModel.status == "active",
            IdentityBindingModel.display_name == name,
        )
    ):
        person_ids.add(row.person_id)
    for alias in await session.scalars(
        select(PersonAliasModel).where(
            PersonAliasModel.alias == name,
            PersonAliasModel.canonical_person_id.is_not(None),
        )
    ):
        person_id = alias.canonical_person_id
        if person_id is None:
            continue
        await _assert_projection_owner(session, alias.user_id, person_id)
        person_ids.add(person_id)
    return await _live_person_ids(session, person_ids)


async def _aliases_for_person(
    session: AsyncSession,
    person_id: str,
    *,
    group_id: str | None = None,
) -> tuple[str, ...]:
    statement = select(PersonAliasModel).where(PersonAliasModel.canonical_person_id == person_id)
    if group_id is not None:
        statement = statement.where(PersonAliasModel.group_scope.in_(("", group_id)))
    aliases: list[str] = []
    for alias in await session.scalars(
        statement.order_by(PersonAliasModel.last_seen_at.desc(), PersonAliasModel.id.desc())
    ):
        await _assert_projection_owner(session, alias.user_id, person_id)
        aliases.append(str(alias.alias))
    return tuple(dict.fromkeys(aliases))


async def _memberships_in_space(
    session: AsyncSession,
    *,
    space_id: str,
    group_id: str,
) -> dict[str, MembershipModel]:
    rows = list(
        await session.scalars(
            select(MembershipModel).where(
                or_(
                    MembershipModel.canonical_space_id == space_id,
                    MembershipModel.group_id == group_id,
                )
            )
        )
    )
    by_person: dict[str, MembershipModel] = {}
    for row in rows:
        person_id = row.canonical_person_id
        if person_id is None:
            binding_rows = list(
                await session.scalars(
                    select(IdentityBindingModel).where(
                        IdentityBindingModel.platform == IDENTITY_PLATFORM,
                        IdentityBindingModel.external_account_id == _external_id(row.user_id),
                    )
                )
            )
            active = [item for item in binding_rows if item.status == "active"]
            if len(active) != 1:
                raise IdentityDualWriteError("unclassified")
            person_id = active[0].person_id
        else:
            await _assert_projection_owner(session, row.user_id, person_id)
            if row.canonical_space_id not in {None, space_id}:
                raise IdentityDualWriteError("canonical_owner_mismatch")
        existing = by_person.get(person_id)
        if existing is not None and existing is not row:
            raise IdentityDualWriteError("canonical_owner_mismatch")
        by_person[person_id] = row
    return by_person


async def load_canonical_people_by_exact_name(session: AsyncSession, name: str) -> tuple[str, ...]:
    """Exact nickname/display/alias search. One representative QQ id per Person."""

    await require_complete_v2_runtime(session)
    normalized = name.strip()
    if not normalized:
        return ()
    externals: list[str] = []
    for person_id in await _person_ids_matching_exact_name(session, normalized):
        external = await _project_person_external(session, person_id)
        if external is not None:
            externals.append(external)
    return tuple(sorted(externals))


async def load_canonical_group_member_name_projections(
    session: AsyncSession,
    group_id: str,
) -> tuple[CanonicalMemberNameProjection, ...]:
    """Project each Person in a Space to one representative member identity."""

    space_binding = await require_space_binding(session, group_id)
    group_external = _external_id(group_id)
    members = await _memberships_in_space(
        session, space_id=space_binding.space_id, group_id=group_external
    )
    live = await _live_person_ids(session, set(members))
    projections: list[CanonicalMemberNameProjection] = []
    for person_id, membership in members.items():
        if person_id not in live:
            continue
        bindings = await bindings_for_person(session, person_id)
        if not _active_bindings(bindings):
            continue
        representative = min(
            _active_bindings(bindings),
            key=lambda item: (item.external_account_id, item.id),
        )
        nickname = await _projected_nickname(session, person_id=person_id, binding=representative)
        aliases = await _aliases_for_person(session, person_id, group_id=group_external)
        projections.append(
            CanonicalMemberNameProjection(
                user_id=representative.external_account_id,
                nickname=nickname,
                group_card=str(membership.group_card or ""),
                aliases=aliases,
            )
        )
    projections.sort(key=lambda item: item.user_id)
    return tuple(projections)


async def load_canonical_group_members_by_exact_name(
    session: AsyncSession,
    name: str,
    group_id: str,
) -> tuple[str, ...]:
    """Exact card/nickname/in-scope alias inside one Space. One id per Person."""

    await require_complete_v2_runtime(session)
    normalized = name.strip()
    if not normalized:
        return ()
    matched: list[str] = []
    for projection in await load_canonical_group_member_name_projections(session, group_id):
        labels = {projection.group_card, projection.nickname, *projection.aliases}
        if normalized in labels:
            matched.append(projection.user_id)
    return tuple(sorted(matched))
