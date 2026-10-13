"""Host-owned historical social access to structured memory, never write/evidence authority.

Callers must supply a real authenticated message sender, not a plugin-declared
actor. No durable grant cache: forgetting membership takes effect on the next read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from qq_ai_bot.memory.authorized_scope import AuthorizedMemoryScope
from qq_ai_bot.memory.enums import MemoryScopeType, MemoryTargetRole
from qq_ai_bot.memory.models import MemoryEntityTarget
from qq_ai_bot.memory.runtime.query_plane import ResolvedReadScope
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MembershipModel, PersonAliasModel


class MemoryReadScopeResolver:
    """Resolve direct historical relationships, without gateway or enabled-state probes."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def authorized(
        self,
        requester: str | None,
        *,
        current_group_id: str | None = None,
        private_scene: bool = False,
        self_actor: bool = False,
        allowed_scopes: tuple[MemoryScopeType, ...] = (
            MemoryScopeType.PERSON,
            MemoryScopeType.PERSON_GROUP,
            MemoryScopeType.GROUP,
            MemoryScopeType.SELF,
        ),
    ) -> AuthorizedMemoryScope:
        """Resolve only the caller and scene; owners are filtered by canonical SQL."""
        async with self._database.sessions() as session:
            person_id = await self._person(session, requester) if requester else None
            space_id = (
                await session.scalar(
                    select(SpaceBindingModel.space_id).where(
                        SpaceBindingModel.platform == "qq",
                        SpaceBindingModel.external_space_id == current_group_id,
                        SpaceBindingModel.status == "active",
                    )
                )
                if current_group_id is not None
                else None
            )
        return AuthorizedMemoryScope(
            requester_person_id=person_id,
            current_space_id=str(space_id) if space_id is not None else None,
            current_private_person_id=person_id if private_scene else None,
            current_group_only=self_actor,
            allowed_scopes=allowed_scopes,
        )

    @staticmethod
    async def _person(session: AsyncSession, external_id: str) -> str | None:
        value = await session.scalar(
            select(IdentityBindingModel.person_id).where(
                IdentityBindingModel.platform == "qq",
                IdentityBindingModel.external_account_id == external_id,
                IdentityBindingModel.status == "active",
            )
        )
        return str(value) if value is not None else None

    @staticmethod
    async def _groups(session: AsyncSession, person_id: str | None) -> set[str]:
        if person_id is None:
            return set()
        return set(
            await session.scalars(
                select(MembershipModel.canonical_space_id).where(
                    MembershipModel.canonical_person_id == person_id
                )
            )
        )

    async def person(
        self,
        requester: str,
        target: str,
        *,
        group_id: str | None = None,
        include_person_groups: bool = True,
        person_ids: Mapping[str, str] | None = None,
        session: AsyncSession | None = None,
    ) -> ResolvedReadScope:
        scopes = await self.people(
            requester,
            (target,),
            group_id=group_id,
            include_person_groups=include_person_groups,
            person_ids=person_ids,
            session=session,
        )
        return scopes.get(target, ResolvedReadScope(targets=()))

    async def people(
        self,
        requester: str,
        targets: tuple[str, ...],
        *,
        group_id: str | None = None,
        include_person_groups: bool = True,
        restrict_to_group: bool = True,
        person_ids: Mapping[str, str] | None = None,
        session: AsyncSession | None = None,
    ) -> dict[str, ResolvedReadScope]:
        """Batch direct social scopes in one snapshot.

        A caller preparing global Person plus local PersonGroup targets can keep
        the global scope when the selected group is not shared. Supplied owners
        come from the caller's canonical projection in this same session.
        """
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                await owned_session.execute(text("BEGIN"))
                return await self.people(
                    requester,
                    targets,
                    group_id=group_id,
                    include_person_groups=include_person_groups,
                    restrict_to_group=restrict_to_group,
                    person_ids=person_ids,
                    session=owned_session,
                )
        if person_ids is None:
            values = func.json_each(
                json.dumps(tuple(dict.fromkeys((requester, *targets))))
            ).table_valued("value")
            owners = {
                account: person_id
                for account, person_id in (
                    await session.execute(
                        select(
                            IdentityBindingModel.external_account_id, IdentityBindingModel.person_id
                        ).where(
                            IdentityBindingModel.platform == "qq",
                            IdentityBindingModel.status == "active",
                            IdentityBindingModel.external_account_id.in_(select(values.c.value)),
                        )
                    )
                ).all()
            }
        else:
            owners = dict(person_ids)
            if requester not in owners:
                requester_id = await self._person(session, requester)
                if requester_id is not None:
                    owners[requester] = requester_id
        requester_id = owners.get(requester)
        if requester_id is None:
            return {}
        candidate_ids = {owners[target] for target in targets if target in owners}
        groups_by_person: dict[str, set[str]] = {}
        if include_person_groups or group_id is not None or candidate_ids - {requester_id}:
            values = func.json_each(json.dumps(tuple({requester_id, *candidate_ids}))).table_valued(
                "value"
            )
            memberships = (
                await session.execute(
                    select(
                        MembershipModel.canonical_person_id, MembershipModel.canonical_space_id
                    ).where(
                        MembershipModel.canonical_person_id.in_(select(values.c.value)),
                        MembershipModel.canonical_space_id.in_(
                            select(MembershipModel.canonical_space_id).where(
                                MembershipModel.canonical_person_id == requester_id
                            )
                        ),
                    )
                )
            ).all()
            for person_id, space_id in memberships:
                groups_by_person.setdefault(person_id, set()).add(space_id)
        selected = None
        if group_id is not None:
            selected = await session.scalar(
                select(SpaceBindingModel.space_id).where(
                    SpaceBindingModel.platform == "qq",
                    SpaceBindingModel.external_space_id == group_id,
                    SpaceBindingModel.status == "active",
                )
            )
        group_accounts: dict[str, str] = {}
        if include_person_groups:
            if group_id is not None:
                if selected is not None:
                    group_accounts[str(selected)] = group_id
            else:
                values = func.json_each(
                    json.dumps(tuple(groups_by_person.get(requester_id, ())))
                ).table_valued("value")
                bindings = (
                    await session.execute(
                        select(SpaceBindingModel.space_id, SpaceBindingModel.external_space_id)
                        .where(
                            SpaceBindingModel.space_id.in_(select(values.c.value)),
                            SpaceBindingModel.platform == "qq",
                            SpaceBindingModel.status == "active",
                        )
                        .order_by(SpaceBindingModel.space_id, SpaceBindingModel.id)
                    )
                ).all()
                for space_id, account in bindings:
                    group_accounts.setdefault(space_id, account)
        result = {}
        for target in dict.fromkeys(targets):
            person_id = owners.get(target)
            if person_id is None:
                continue
            own = requester_id == person_id
            shared = groups_by_person.get(person_id, set())
            if not own and not shared:
                continue
            if group_id is not None:
                if selected not in shared and restrict_to_group:
                    continue
                shared = {str(selected)} if selected in shared else set()
            entity_targets = [
                MemoryEntityTarget(
                    scope_type=MemoryScopeType.PERSON,
                    subject_user_id=target,
                    role=MemoryTargetRole.CURRENT_PERSON
                    if own
                    else MemoryTargetRole.REFERENCED_PERSON,
                    block_id="current_person" if own else f"referenced_person:{person_id}",
                )
            ]
            if include_person_groups:
                for space_id in sorted(shared):
                    if space_id not in group_accounts:
                        continue
                    entity_targets.append(
                        MemoryEntityTarget(
                            scope_type=MemoryScopeType.PERSON_GROUP,
                            subject_user_id=target,
                            group_id=group_accounts[space_id],
                            role=(
                                MemoryTargetRole.CURRENT_PERSON_GROUP
                                if own
                                else MemoryTargetRole.REFERENCED_PERSON_GROUP
                            ),
                            block_id=f"person_group:{person_id}:{space_id}",
                        )
                    )
            result[target] = ResolvedReadScope(targets=tuple(entity_targets))
        return result

    async def group(self, requester: str, group_id: str) -> ResolvedReadScope:
        async with self._database.sessions() as session:
            person_id = await self._person(session, requester)
            groups = await self._groups(session, person_id)
            space_id = await session.scalar(
                select(SpaceBindingModel.space_id).where(
                    SpaceBindingModel.platform == "qq",
                    SpaceBindingModel.external_space_id == group_id,
                    SpaceBindingModel.status == "active",
                )
            )
            if space_id not in groups:
                return ResolvedReadScope(targets=())
        return ResolvedReadScope(
            targets=(
                MemoryEntityTarget(
                    scope_type=MemoryScopeType.GROUP,
                    group_id=group_id,
                    role=MemoryTargetRole.CURRENT_GROUP,
                    block_id=f"group:{space_id}",
                ),
            )
        )

    async def maximum(self, requester: str) -> ResolvedReadScope:
        """All currently readable social owners, without a scene or target cap.

        The target projection still uses active QQ bindings. Owners that have
        historical membership but no active binding are reported as incomplete
        rather than silently treating this projection as exhaustive.
        """

        async with self._database.sessions() as session:
            requester_id = await self._person(session, requester)
            if requester_id is None:
                return ResolvedReadScope(targets=(), complete=False)
            groups = await self._groups(session, requester_id)
            memberships = (
                (
                    await session.execute(
                        select(
                            MembershipModel.canonical_person_id,
                            MembershipModel.canonical_space_id,
                        )
                        .where(MembershipModel.canonical_space_id.in_(groups))
                        .order_by(
                            MembershipModel.canonical_person_id,
                            MembershipModel.canonical_space_id,
                        )
                    )
                ).all()
                if groups
                else ()
            )
            person_ids = {requester_id, *(row.canonical_person_id for row in memberships)}
            person_bindings = (
                await session.scalars(
                    select(IdentityBindingModel)
                    .where(
                        IdentityBindingModel.person_id.in_(person_ids),
                        IdentityBindingModel.platform == "qq",
                        IdentityBindingModel.status == "active",
                    )
                    .order_by(IdentityBindingModel.person_id, IdentityBindingModel.id)
                )
            ).all()
            group_bindings = (
                await session.scalars(
                    select(SpaceBindingModel)
                    .where(
                        SpaceBindingModel.space_id.in_(groups),
                        SpaceBindingModel.platform == "qq",
                        SpaceBindingModel.status == "active",
                    )
                    .order_by(SpaceBindingModel.space_id, SpaceBindingModel.id)
                )
            ).all()
            people: dict[str, str] = {}
            spaces: dict[str, str] = {}
            for person_binding in person_bindings:
                people.setdefault(person_binding.person_id, person_binding.external_account_id)
            for space_binding in group_bindings:
                spaces.setdefault(space_binding.space_id, space_binding.external_space_id)
            disabled_people = bool(
                await session.scalar(
                    select(CanonicalPersonModel.id)
                    .where(
                        CanonicalPersonModel.id.in_(person_ids),
                        CanonicalPersonModel.enabled.is_(False),
                    )
                    .limit(1)
                )
            )
            disabled_spaces = bool(
                await session.scalar(
                    select(CanonicalSpaceModel.id)
                    .where(
                        CanonicalSpaceModel.id.in_(groups),
                        CanonicalSpaceModel.enabled.is_(False),
                    )
                    .limit(1)
                )
            )

            targets: list[MemoryEntityTarget] = []
            for person_id in sorted(person_ids):
                user_id = people.get(person_id)
                if user_id is None:
                    continue
                own = person_id == requester_id
                targets.append(
                    MemoryEntityTarget(
                        scope_type=MemoryScopeType.PERSON,
                        subject_user_id=user_id,
                        role=(
                            MemoryTargetRole.CURRENT_PERSON
                            if own
                            else MemoryTargetRole.REFERENCED_PERSON
                        ),
                        block_id=f"person:{person_id}",
                    )
                )
            for space_id in sorted(groups):
                group_id = spaces.get(space_id)
                if group_id is None:
                    continue
                targets.append(
                    MemoryEntityTarget(
                        scope_type=MemoryScopeType.GROUP,
                        group_id=group_id,
                        role=MemoryTargetRole.CURRENT_GROUP,
                        block_id=f"group:{space_id}",
                    )
                )
            for person_id, space_id in sorted(set(memberships)):
                user_id = people.get(person_id)
                group_id = spaces.get(space_id)
                if user_id is None or group_id is None:
                    continue
                targets.append(
                    MemoryEntityTarget(
                        scope_type=MemoryScopeType.PERSON_GROUP,
                        subject_user_id=user_id,
                        group_id=group_id,
                        role=(
                            MemoryTargetRole.CURRENT_PERSON_GROUP
                            if person_id == requester_id
                            else MemoryTargetRole.REFERENCED_PERSON_GROUP
                        ),
                        block_id=f"person_group:{person_id}:{space_id}",
                    )
                )
            return ResolvedReadScope(
                targets=tuple(targets),
                complete=(
                    person_ids.issubset(people)
                    and groups.issubset(spaces)
                    and not disabled_people
                    and not disabled_spaces
                ),
            )

    async def people_named(self, requester: str, name: str) -> tuple[str, ...]:
        """Complete exact-name candidates inside authorized relationships."""
        async with self._database.sessions() as session:
            requester_id = await self._person(session, requester)
            if requester_id is None:
                return ()
            groups = select(MembershipModel.canonical_space_id).where(
                MembershipModel.canonical_person_id == requester_id
            )
            related = select(MembershipModel.canonical_person_id).where(
                MembershipModel.canonical_space_id.in_(groups)
            )
            alias_match = (
                select(PersonAliasModel.id)
                .where(
                    PersonAliasModel.canonical_person_id == IdentityBindingModel.person_id,
                    PersonAliasModel.alias == name.strip(),
                    or_(
                        PersonAliasModel.canonical_space_id.is_(None),
                        PersonAliasModel.canonical_space_id.in_(groups),
                    ),
                )
                .exists()
            )
            card_match = (
                select(MembershipModel.canonical_person_id)
                .where(
                    MembershipModel.canonical_person_id == IdentityBindingModel.person_id,
                    MembershipModel.canonical_space_id.in_(groups),
                    MembershipModel.group_card == name.strip(),
                )
                .exists()
            )
            people = select(IdentityBindingModel.person_id).where(
                IdentityBindingModel.platform == "qq",
                IdentityBindingModel.status == "active",
                or_(
                    IdentityBindingModel.person_id == requester_id,
                    IdentityBindingModel.person_id.in_(related),
                ),
                or_(
                    IdentityBindingModel.display_name == name.strip(),
                    alias_match,
                    card_match,
                ),
            )
            bindings = (
                await session.scalars(
                    select(IdentityBindingModel)
                    .where(
                        IdentityBindingModel.person_id.in_(people),
                        IdentityBindingModel.platform == "qq",
                        IdentityBindingModel.status == "active",
                    )
                    .order_by(IdentityBindingModel.person_id, IdentityBindingModel.id)
                )
            ).all()
            selected: dict[str, str] = {}
            for binding in bindings:
                selected.setdefault(binding.person_id, binding.external_account_id)
            return tuple(selected.values())

    async def groups_named(self, requester: str, name: str) -> tuple[str, ...]:
        """No live membership probe and no disclosure of inaccessible group names."""
        async with self._database.sessions() as session:
            requester_id = await self._person(session, requester)
            if requester_id is None:
                return ()
            groups = select(MembershipModel.canonical_space_id).where(
                MembershipModel.canonical_person_id == requester_id
            )
            matching = (
                select(SpaceBindingModel.space_id)
                .join(CanonicalSpaceModel, CanonicalSpaceModel.id == SpaceBindingModel.space_id)
                .where(
                    SpaceBindingModel.space_id.in_(groups),
                    SpaceBindingModel.platform == "qq",
                    SpaceBindingModel.status == "active",
                    or_(
                        SpaceBindingModel.display_name == name.strip(),
                        CanonicalSpaceModel.name == name.strip(),
                    ),
                )
                .distinct()
                .order_by(SpaceBindingModel.space_id)
            )
            bindings = (
                await session.scalars(
                    select(SpaceBindingModel)
                    .where(
                        SpaceBindingModel.space_id.in_(matching),
                        SpaceBindingModel.platform == "qq",
                        SpaceBindingModel.status == "active",
                    )
                    .order_by(SpaceBindingModel.space_id, SpaceBindingModel.id)
                )
            ).all()
            selected: dict[str, str] = {}
            for binding in bindings:
                selected.setdefault(binding.space_id, binding.external_space_id)
            return tuple(selected.values())
