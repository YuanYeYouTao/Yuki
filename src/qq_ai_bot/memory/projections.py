"""Explicit read projections from canonical Memory owners to active bindings.

Memory ownership is stored only as Person/Space UUIDs.  The external QQ and
group identifiers exposed by the current domain DTOs are adapter projections;
they are never used as a persistence fallback or an authorization boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.canonical_repository import IDENTITY_PLATFORM
from qq_ai_bot.identity.db_models import IdentityBindingModel, SpaceBindingModel
from qq_ai_bot.memory.models import MemoryFact
from qq_ai_bot.persistence.models import MemoryFactModel


@dataclass(frozen=True, slots=True)
class MemoryOwnerProjection:
    """Deterministic active external-ID view of canonical owners."""

    persons: dict[str, str]
    spaces: dict[str, str]


async def load_memory_owner_projection(
    session: AsyncSession,
    rows: Iterable[MemoryFactModel],
) -> MemoryOwnerProjection:
    person_ids: set[str] = set()
    space_ids: set[str] = set()
    for row in rows:
        person_ids.update(
            owner
            for owner in (
                row.canonical_subject_person_id,
                row.canonical_visibility_person_id,
            )
            if owner is not None
        )
        space_ids.update(
            owner
            for owner in (
                row.canonical_subject_space_id,
                row.canonical_visibility_space_id,
            )
            if owner is not None
        )

    persons: dict[str, str] = {}
    if person_ids:
        bindings = (
            await session.execute(
                select(
                    IdentityBindingModel.person_id,
                    IdentityBindingModel.external_account_id,
                )
                .where(
                    IdentityBindingModel.person_id.in_(person_ids),
                    IdentityBindingModel.platform == IDENTITY_PLATFORM,
                    IdentityBindingModel.status == "active",
                )
                .order_by(
                    IdentityBindingModel.person_id,
                    IdentityBindingModel.created_at,
                    IdentityBindingModel.id,
                )
            )
        ).all()
        for person_id, external_id in bindings:
            persons.setdefault(str(person_id), str(external_id))

    spaces: dict[str, str] = {}
    if space_ids:
        bindings = (
            await session.execute(
                select(
                    SpaceBindingModel.space_id,
                    SpaceBindingModel.external_space_id,
                )
                .where(
                    SpaceBindingModel.space_id.in_(space_ids),
                    SpaceBindingModel.platform == IDENTITY_PLATFORM,
                    SpaceBindingModel.status == "active",
                )
                .order_by(
                    SpaceBindingModel.space_id,
                    SpaceBindingModel.created_at,
                    SpaceBindingModel.id,
                )
            )
        ).all()
        for space_id, external_id in bindings:
            spaces.setdefault(str(space_id), str(external_id))
    return MemoryOwnerProjection(persons=persons, spaces=spaces)


async def project_active_person_external_id(
    session: AsyncSession,
    person_id: str,
) -> str | None:
    value = await session.scalar(
        select(IdentityBindingModel.external_account_id)
        .where(
            IdentityBindingModel.person_id == person_id,
            IdentityBindingModel.platform == IDENTITY_PLATFORM,
            IdentityBindingModel.status == "active",
        )
        .order_by(IdentityBindingModel.created_at, IdentityBindingModel.id)
        .limit(1)
    )
    return str(value) if value is not None else None


async def project_active_space_external_id(
    session: AsyncSession,
    space_id: str,
) -> str | None:
    value = await session.scalar(
        select(SpaceBindingModel.external_space_id)
        .where(
            SpaceBindingModel.space_id == space_id,
            SpaceBindingModel.platform == IDENTITY_PLATFORM,
            SpaceBindingModel.status == "active",
        )
        .order_by(SpaceBindingModel.created_at, SpaceBindingModel.id)
        .limit(1)
    )
    return str(value) if value is not None else None


def project_memory_fact(
    row: MemoryFactModel,
    owners: MemoryOwnerProjection,
    *,
    evidence_count: int = 0,
) -> MemoryFact:
    """Project one canonical fact without consulting removed carrier columns."""

    subject_user_id = (
        owners.persons.get(row.canonical_subject_person_id)
        if row.canonical_subject_person_id
        else None
    )
    group_id = (
        owners.spaces.get(row.canonical_subject_space_id)
        if row.canonical_subject_space_id
        else None
    )
    visibility_user_id = (
        owners.persons.get(row.canonical_visibility_person_id)
        if row.canonical_visibility_person_id
        else None
    )
    visibility_group_id = (
        owners.spaces.get(row.canonical_visibility_space_id)
        if row.canonical_visibility_space_id
        else None
    )
    return MemoryFact(
        id=row.id,
        scope_type=row.scope_type,
        subject_user_id=subject_user_id,
        group_id=group_id,
        visibility_type=row.visibility_type,
        visibility_user_id=visibility_user_id,
        visibility_group_id=visibility_group_id,
        kind=row.kind,
        memory_key=row.memory_key,
        category=row.category,
        content=row.content,
        normalized_content=row.normalized_content,
        importance=row.importance,
        confidence=row.confidence,
        source_type=row.source_type,
        authority=row.authority,
        status=row.status,
        conflict_state=row.conflict_state,
        supersedes_id=row.supersedes_id,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_confirmed_at=row.last_confirmed_at,
        invalidated_reason=row.invalidated_reason,
        last_injected_at=row.last_injected_at,
        evidence_count=evidence_count,
        validation_version=row.validation_version,
        last_audited_at=row.last_audited_at,
        review_state=row.review_state,
        canonical_subject_person_id=row.canonical_subject_person_id,
        canonical_subject_space_id=row.canonical_subject_space_id,
        canonical_visibility_person_id=row.canonical_visibility_person_id,
        canonical_visibility_space_id=row.canonical_visibility_space_id,
    )


async def project_memory_fact_rows(
    session: AsyncSession,
    rows: Iterable[tuple[MemoryFactModel, Any]],
) -> tuple[MemoryFact, ...]:
    materialized = tuple(rows)
    owners = await load_memory_owner_projection(session, (row for row, _count in materialized))
    return tuple(
        project_memory_fact(row, owners, evidence_count=int(count or 0))
        for row, count in materialized
    )
