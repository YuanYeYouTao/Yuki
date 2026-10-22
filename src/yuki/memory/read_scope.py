"""Host-owned historical social access to structured memory, never write/evidence authority.

Callers must supply a real authenticated message sender, not a plugin-declared
actor. No durable grant cache: forgetting membership takes effect on the next read.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.identity.db_models import (
    AccountModel,
    AccountNameObservationModel,
    AccountSpaceProfileModel,
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from yuki.identity.person_identity import (
    current_person_ids,
    person_family_condition,
    person_family_ids,
    person_root_id,
    same_person_condition,
)
from yuki.memory.authorized_scope import AuthorizedMemoryScope
from yuki.memory.enums import MemoryScopeType, MemoryTargetRole
from yuki.memory.models import MemoryEntityTarget
from yuki.memory.runtime.query_plane import ResolvedReadScope
from yuki.persistence.database import Database
from yuki.persistence.models import MembershipModel, PersonAliasModel


class MemoryReadScopeResolver:
    """Resolve direct historical relationships, without gateway or enabled-state probes."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def authorized(
        self,
        requester: str | None,
        *,
        current_space_id: str | None = None,
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
        async with self._database.read_session() as session:
            person_id = await self._person(session, requester) if requester else None
            family = await person_family_ids(session, person_id) if person_id else ()
            space_id = (
                await self._space(session, current_space_id)
                if current_space_id is not None
                else None
            )
        return AuthorizedMemoryScope(
            requester_person_id=person_id,
            requester_family_ids=family,
            current_space_id=str(space_id) if space_id is not None else None,
            current_private_person_id=person_id if private_scene else None,
            current_group_only=self_actor,
            allowed_scopes=allowed_scopes,
        )

    @staticmethod
    async def _person(session: AsyncSession, person_id: str) -> str | None:
        return await session.scalar(select(person_root_id(person_id)))

    @staticmethod
    async def _space(session: AsyncSession, space_id: str) -> str | None:
        space = await session.get(CanonicalSpaceModel, space_id)
        return space.id if space is not None else None

    @staticmethod
    async def _groups(session: AsyncSession, person_id: str | None) -> set[str]:
        if person_id is None:
            return set()
        return set(
            await session.scalars(
                select(MembershipModel.canonical_space_id).where(
                    person_family_condition(MembershipModel.canonical_person_id, person_id)
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
            async with self._database.read_session() as owned_session:
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
        owners = dict(person_ids or {})
        for canonical in await session.scalars(
            select(CanonicalPersonModel.id).where(
                CanonicalPersonModel.id.in_(tuple(dict.fromkeys((requester, *targets))))
            )
        ):
            owners[canonical] = canonical
        roots = await current_person_ids(session, owners.values())
        owners = {key: roots[value] for key, value in owners.items() if value in roots}
        requester_id = owners.get(requester) or await self._person(session, requester)
        if requester_id is None:
            return {}
        candidate_ids = {owners[target] for target in targets if target in owners}
        groups_by_person: dict[str, set[str]] = {}
        if include_person_groups or group_id is not None or candidate_ids - {requester_id}:
            candidates = tuple({requester_id, *candidate_ids})
            family = (
                select(CanonicalPersonModel.id)
                .where(
                    or_(
                        CanonicalPersonModel.id.in_(candidates),
                        CanonicalPersonModel.merged_into_person_id.in_(candidates),
                    )
                )
                .correlate(None)
            )
            subject = CanonicalPersonModel
            memberships = (
                await session.execute(
                    select(
                        func.coalesce(subject.merged_into_person_id, subject.id),
                        MembershipModel.canonical_space_id,
                    )
                    .select_from(MembershipModel)
                    .join(subject, subject.id == MembershipModel.canonical_person_id)
                    .where(
                        MembershipModel.canonical_person_id.in_(family),
                        MembershipModel.canonical_space_id.in_(
                            select(MembershipModel.canonical_space_id).where(
                                person_family_condition(
                                    MembershipModel.canonical_person_id, requester_id
                                )
                            )
                        ),
                    )
                )
            ).all()
            for person_id, space_id in memberships:
                groups_by_person.setdefault(person_id, set()).add(space_id)
        selected = None
        if group_id is not None:
            selected = await self._space(session, group_id)
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
                    canonical_subject_person_id=person_id,
                    role=MemoryTargetRole.CURRENT_PERSON
                    if own
                    else MemoryTargetRole.REFERENCED_PERSON,
                    block_id="current_person" if own else f"referenced_person:{person_id}",
                )
            ]
            if include_person_groups:
                for space_id in sorted(shared):
                    entity_targets.append(
                        MemoryEntityTarget(
                            scope_type=MemoryScopeType.PERSON_GROUP,
                            canonical_subject_person_id=person_id,
                            canonical_subject_space_id=space_id,
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
        async with self._database.read_session() as session:
            person_id = await self._person(session, requester)
            groups = await self._groups(session, person_id)
            space_id = await self._space(session, group_id)
            if space_id not in groups:
                return ResolvedReadScope(targets=())
        return ResolvedReadScope(
            targets=(
                MemoryEntityTarget(
                    scope_type=MemoryScopeType.GROUP,
                    canonical_subject_space_id=space_id,
                    role=MemoryTargetRole.CURRENT_GROUP,
                    block_id=f"group:{space_id}",
                ),
            )
        )

    async def people_named(self, requester: str, name: str) -> tuple[str, ...]:
        """Complete exact-name candidates inside authorized relationships."""
        async with self._database.read_session() as session:
            requester_id = await self._person(session, requester)
            if requester_id is None:
                return ()
            groups = select(MembershipModel.canonical_space_id).where(
                person_family_condition(MembershipModel.canonical_person_id, requester_id)
            )
            related = select(person_root_id(MembershipModel.canonical_person_id)).where(
                MembershipModel.canonical_space_id.in_(groups)
            )
            alias_match = (
                select(PersonAliasModel.id)
                .where(
                    same_person_condition(
                        PersonAliasModel.canonical_person_id, CanonicalPersonModel.id
                    ),
                    PersonAliasModel.alias == name.strip(),
                    or_(
                        PersonAliasModel.canonical_space_id.is_(None),
                        PersonAliasModel.canonical_space_id.in_(groups),
                    ),
                )
                .exists()
            )
            card_match = (
                select(IdentityBindingModel.id)
                .join(
                    AccountSpaceProfileModel,
                    AccountSpaceProfileModel.account_id == IdentityBindingModel.account_id,
                )
                .join(
                    SpaceBindingModel,
                    SpaceBindingModel.id == AccountSpaceProfileModel.space_binding_id,
                )
                .where(
                    same_person_condition(IdentityBindingModel.person_id, CanonicalPersonModel.id),
                    IdentityBindingModel.status != "ended",
                    SpaceBindingModel.space_id.in_(groups),
                    AccountSpaceProfileModel.display_name == name.strip(),
                )
                .exists()
            )
            display_match = (
                select(IdentityBindingModel.id)
                .join(AccountModel, AccountModel.id == IdentityBindingModel.account_id)
                .where(
                    same_person_condition(IdentityBindingModel.person_id, CanonicalPersonModel.id),
                    IdentityBindingModel.status != "ended",
                    AccountModel.display_name == name.strip(),
                )
                .exists()
            )
            observed_name = (
                select(AccountNameObservationModel.id)
                .join(
                    IdentityBindingModel,
                    IdentityBindingModel.id == AccountNameObservationModel.assignment_id,
                )
                .outerjoin(
                    SpaceBindingModel,
                    SpaceBindingModel.id == AccountNameObservationModel.space_binding_id,
                )
                .where(
                    same_person_condition(IdentityBindingModel.person_id, CanonicalPersonModel.id),
                    AccountNameObservationModel.name == name.strip(),
                    or_(
                        AccountNameObservationModel.space_binding_id.is_(None),
                        SpaceBindingModel.space_id.in_(groups),
                    ),
                )
                .exists()
            )
            return tuple(
                await session.scalars(
                    select(person_root_id(CanonicalPersonModel.id))
                    .where(
                        or_(
                            person_family_condition(CanonicalPersonModel.id, requester_id),
                            person_root_id(CanonicalPersonModel.id).in_(related),
                        ),
                        or_(
                            CanonicalPersonModel.display_name == name.strip(),
                            display_match,
                            alias_match,
                            card_match,
                            observed_name,
                        ),
                    )
                    .distinct()
                    .order_by(person_root_id(CanonicalPersonModel.id))
                )
            )

    async def groups_named(self, requester: str, name: str) -> tuple[str, ...]:
        """Names resolve within the requester's historical canonical relationships."""
        async with self._database.read_session() as session:
            requester_id = await self._person(session, requester)
            if requester_id is None:
                return ()
            groups = select(MembershipModel.canonical_space_id).where(
                person_family_condition(MembershipModel.canonical_person_id, requester_id)
            )
            display_match = (
                select(SpaceBindingModel.id)
                .where(
                    SpaceBindingModel.space_id == CanonicalSpaceModel.id,
                    SpaceBindingModel.status == "active",
                    SpaceBindingModel.display_name == name.strip(),
                )
                .exists()
            )
            return tuple(
                await session.scalars(
                    select(CanonicalSpaceModel.id)
                    .where(
                        CanonicalSpaceModel.id.in_(groups),
                        or_(CanonicalSpaceModel.name == name.strip(), display_match),
                    )
                    .order_by(CanonicalSpaceModel.id)
                )
            )
