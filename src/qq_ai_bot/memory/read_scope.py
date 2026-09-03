"""Host-owned historical social access to structured memory, never write/evidence authority.

Callers must supply a real authenticated message sender, not a plugin-declared
actor. No durable grant cache: forgetting membership takes effect on the next read.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.db_models import (
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from qq_ai_bot.memory.enums import MemoryScopeType, MemoryTargetRole
from qq_ai_bot.memory.models import MemoryEntityTarget, MemoryFact
from qq_ai_bot.memory.partition import canonical_fact_owner_complete
from qq_ai_bot.memory.runtime.query_plane import ResolvedReadScope
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MembershipModel, PersonAliasModel


class MemoryReadScopeResolver:
    """Resolve direct historical relationships, without gateway or enabled-state probes."""

    def __init__(self, database: Database) -> None:
        self._database = database

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
    ) -> ResolvedReadScope:
        async with self._database.sessions() as session:
            requester_id = await self._person(session, requester)
            person_id = await self._person(session, target)
            if requester_id is None or person_id is None:
                return ResolvedReadScope(targets=())
            shared = await self._groups(session, requester_id)
            if requester_id != person_id:
                shared &= await self._groups(session, person_id)
                if not shared:
                    return ResolvedReadScope(targets=())
            if group_id is not None:
                selected = await session.scalar(
                    select(SpaceBindingModel.space_id).where(
                        SpaceBindingModel.platform == "qq",
                        SpaceBindingModel.external_space_id == group_id,
                        SpaceBindingModel.status == "active",
                    )
                )
                if selected not in shared:
                    return ResolvedReadScope(targets=())
                shared = {selected}
            own = requester_id == person_id
            targets = [
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
                bindings = (
                    await session.scalars(
                        select(SpaceBindingModel)
                        .where(
                            SpaceBindingModel.space_id.in_(shared),
                            SpaceBindingModel.platform == "qq",
                            SpaceBindingModel.status == "active",
                        )
                        .order_by(SpaceBindingModel.space_id, SpaceBindingModel.id)
                    )
                ).all()
                seen: set[str] = set()
                for binding in bindings:
                    if binding.space_id in seen:
                        continue
                    seen.add(binding.space_id)
                    targets.append(
                        MemoryEntityTarget(
                            scope_type=MemoryScopeType.PERSON_GROUP,
                            subject_user_id=target,
                            group_id=group_id or binding.external_space_id,
                            role=(
                                MemoryTargetRole.CURRENT_PERSON_GROUP
                                if own
                                else MemoryTargetRole.REFERENCED_PERSON_GROUP
                            ),
                            block_id=f"person_group:{person_id}:{binding.space_id}",
                        )
                    )
            return ResolvedReadScope(targets=tuple(targets))

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

    async def allows_fact(self, requester: str, fact: MemoryFact) -> bool:
        if not canonical_fact_owner_complete(fact) or fact.scope_type is MemoryScopeType.SELF:
            return False  # SELF has its separate current-conversation policy.
        async with self._database.sessions() as session:
            requester_id = await self._person(session, requester)
            if requester_id is None:
                return False
            groups = await self._groups(session, requester_id)
            if fact.scope_type is MemoryScopeType.GROUP:
                return fact.canonical_subject_space_id in groups
            target = fact.canonical_subject_person_id
            if fact.scope_type is MemoryScopeType.PERSON:
                return requester_id == target or bool(groups & await self._groups(session, target))
            if fact.scope_type is MemoryScopeType.PERSON_GROUP:
                return fact.canonical_subject_space_id in (
                    groups & await self._groups(session, target)
                )
            return False

    async def people_named(self, requester: str, name: str) -> tuple[str, ...]:
        """Exact names inside authorized relationships, at most six distinct people."""
        async with self._database.sessions() as session:
            requester_id = await self._person(session, requester)
            if requester_id is None:
                return ()
            groups = await self._groups(session, requester_id)
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
            people = (
                await session.scalars(
                    select(IdentityBindingModel.person_id)
                    .where(
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
                    .distinct()
                    .order_by(IdentityBindingModel.person_id)
                    .limit(6)
                )
            ).all()
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
            groups = await self._groups(session, await self._person(session, requester))
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
                .limit(6)
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
