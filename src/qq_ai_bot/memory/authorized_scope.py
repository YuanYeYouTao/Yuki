"""One SQL authorization boundary for global, on-demand Memory retrieval."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, false, or_, select
from sqlalchemy.orm import aliased

from qq_ai_bot.memory.enums import MemoryScopeType, MemoryTargetRole, SelfMemoryVisibility
from qq_ai_bot.memory.models import MemoryEntityTarget, MemoryFact
from qq_ai_bot.persistence.models import MembershipModel, MemoryFactModel


class AuthorizedMemoryScope(BaseModel):
    """Trusted canonical IDs; no model argument or persistent permission grant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requester_person_id: str | None = None
    current_space_id: str | None = None
    current_private_person_id: str | None = None
    current_group_only: bool = False
    allowed_scopes: tuple[MemoryScopeType, ...] = ()


def authorized_fact_condition(scope: AuthorizedMemoryScope) -> Any:
    """Correlated scope filter, shared by FTS, vector and fact hydration queries."""

    fact = MemoryFactModel
    clauses: list[Any] = []
    requester = scope.requester_person_id
    if requester is not None:
        requester_membership = aliased(MembershipModel)
        member_of_fact_space = (
            select(1)
            .select_from(requester_membership)
            .where(
                requester_membership.canonical_person_id == requester,
                requester_membership.canonical_space_id == fact.canonical_subject_space_id,
            )
            .exists()
        )
        if MemoryScopeType.PERSON in scope.allowed_scopes:
            first = aliased(MembershipModel)
            second = aliased(MembershipModel)
            direct_shared_group = (
                select(1)
                .select_from(first)
                .join(second, second.canonical_space_id == first.canonical_space_id)
                .where(
                    first.canonical_person_id == requester,
                    second.canonical_person_id == fact.canonical_subject_person_id,
                )
                .exists()
            )
            clauses.append(
                and_(
                    fact.scope_type == MemoryScopeType.PERSON.value,
                    fact.canonical_subject_person_id.is_not(None),
                    fact.canonical_subject_space_id.is_(None),
                    or_(fact.canonical_subject_person_id == requester, direct_shared_group),
                )
            )
        if MemoryScopeType.PERSON_GROUP in scope.allowed_scopes:
            owner_membership = aliased(MembershipModel)
            owner_in_fact_space = (
                select(1)
                .select_from(owner_membership)
                .where(
                    owner_membership.canonical_person_id == fact.canonical_subject_person_id,
                    owner_membership.canonical_space_id == fact.canonical_subject_space_id,
                )
                .exists()
            )
            clauses.append(
                and_(
                    fact.scope_type == MemoryScopeType.PERSON_GROUP.value,
                    fact.canonical_subject_person_id.is_not(None),
                    fact.canonical_subject_space_id.is_not(None),
                    member_of_fact_space,
                    owner_in_fact_space,
                )
            )
        if MemoryScopeType.GROUP in scope.allowed_scopes:
            clauses.append(
                and_(
                    fact.scope_type == MemoryScopeType.GROUP.value,
                    fact.canonical_subject_person_id.is_(None),
                    fact.canonical_subject_space_id.is_not(None),
                    member_of_fact_space,
                )
            )
    elif (
        scope.current_group_only
        and scope.current_space_id is not None
        and MemoryScopeType.GROUP in scope.allowed_scopes
    ):
        clauses.append(
            and_(
                fact.scope_type == MemoryScopeType.GROUP.value,
                fact.canonical_subject_person_id.is_(None),
                fact.canonical_subject_space_id == scope.current_space_id,
            )
        )
    if MemoryScopeType.SELF in scope.allowed_scopes:
        visibility: list[Any] = [
            and_(
                fact.visibility_type == SelfMemoryVisibility.GLOBAL.value,
                fact.canonical_visibility_person_id.is_(None),
                fact.canonical_visibility_space_id.is_(None),
            )
        ]
        if scope.current_private_person_id is not None:
            visibility.append(
                and_(
                    fact.visibility_type == SelfMemoryVisibility.PRIVATE.value,
                    fact.canonical_visibility_person_id == scope.current_private_person_id,
                    fact.canonical_visibility_space_id.is_(None),
                )
            )
        if scope.current_space_id is not None:
            visibility.append(
                and_(
                    fact.visibility_type == SelfMemoryVisibility.GROUP.value,
                    fact.canonical_visibility_person_id.is_(None),
                    fact.canonical_visibility_space_id == scope.current_space_id,
                )
            )
        clauses.append(
            and_(
                fact.scope_type == MemoryScopeType.SELF.value,
                fact.canonical_subject_person_id.is_(None),
                fact.canonical_subject_space_id.is_(None),
                or_(*visibility),
            )
        )
    return or_(*clauses) if clauses else false()


def target_for_authorized_fact(
    fact: MemoryFact, scope: AuthorizedMemoryScope
) -> MemoryEntityTarget:
    """Rank by canonical owner even if every transport Binding was removed."""

    if fact.scope_type is MemoryScopeType.PERSON:
        person_id = fact.canonical_subject_person_id
        assert person_id is not None
        return MemoryEntityTarget(
            role=(
                MemoryTargetRole.CURRENT_PERSON
                if person_id == scope.requester_person_id
                else MemoryTargetRole.REFERENCED_PERSON
            ),
            scope_type=fact.scope_type,
            canonical_subject_person_id=person_id,
            block_id=f"person:{person_id}",
        )
    if fact.scope_type is MemoryScopeType.PERSON_GROUP:
        person_id = fact.canonical_subject_person_id
        space_id = fact.canonical_subject_space_id
        assert person_id is not None and space_id is not None
        return MemoryEntityTarget(
            role=(
                MemoryTargetRole.CURRENT_PERSON_GROUP
                if person_id == scope.requester_person_id
                else MemoryTargetRole.REFERENCED_PERSON_GROUP
            ),
            scope_type=fact.scope_type,
            canonical_subject_person_id=person_id,
            canonical_subject_space_id=space_id,
            block_id=f"person_group:{person_id}:{space_id}",
        )
    if fact.scope_type is MemoryScopeType.GROUP:
        space_id = fact.canonical_subject_space_id
        assert space_id is not None
        return MemoryEntityTarget(
            role=MemoryTargetRole.CURRENT_GROUP,
            scope_type=fact.scope_type,
            canonical_subject_space_id=space_id,
            block_id=f"group:{space_id}",
        )
    return MemoryEntityTarget(
        role=MemoryTargetRole.CURRENT_SELF,
        scope_type=MemoryScopeType.SELF,
        visibility_type=fact.visibility_type,
        canonical_visibility_person_id=fact.canonical_visibility_person_id,
        canonical_visibility_space_id=fact.canonical_visibility_space_id,
        block_id="current_self",
    )
