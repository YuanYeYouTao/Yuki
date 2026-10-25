"""Persistence for immutable profiles and target-scoped derived vectors."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.identity.authority import person_is_superuser
from yuki.identity.person_identity import person_family_condition
from yuki.memory.authorized_scope import AuthorizedMemoryScope, authorized_fact_condition
from yuki.memory.embedding.models import (
    EmbeddingProviderProfile,
    MemoryEmbeddingProfileRecord,
)
from yuki.memory.embedding.text import EmbeddingDocumentBuilder
from yuki.memory.models import MemoryEntityTarget, MemoryTemporalIntent
from yuki.memory.partition import (
    MemoryPartitionResolutionError,
    resolve_fact_canonical_owners,
)
from yuki.memory.temporal_filter import strict_time_conditions
from yuki.persistence.database import Database
from yuki.persistence.models import (
    MemoryEmbeddingJobModel,
    MemoryEmbeddingModel,
    MemoryEmbeddingProfileModel,
    MemoryFactModel,
)
from yuki.persistence.vec1 import create_profile_vectors, drop_profile_vectors, vector_table


@dataclass(frozen=True, slots=True)
class StoredTargetVector:
    fact_id: int
    content_hash: str
    vector_blob: bytes
    kind: str
    category: str
    memory_key: str
    content: str


@dataclass(frozen=True, slots=True)
class VectorCandidates:
    rows: tuple[StoredTargetVector, ...]
    active_count: int
    indexed_count: int
    approximate: bool


class MemoryEmbeddingRepository:
    """Never loads vectors before SQL has enforced the exact identity target."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    @staticmethod
    async def store_vectors(
        session: AsyncSession, *, profile_id: int, records: list[dict[str, Any]]
    ) -> None:
        """Publish only claim-accepted metadata and raw values in the caller's writer."""
        statement = insert(MemoryEmbeddingModel)
        metadata = [
            {key: value for key, value in row.items() if key != "vector_blob"} for row in records
        ]
        accepted = (
            await session.execute(
                statement.values(metadata)
                .on_conflict_do_update(
                    index_elements=["fact_id", "profile_id"],
                    set_={
                        key: getattr(statement.excluded, key)
                        for key in ("content_hash", "updated_at")
                    },
                )
                .returning(MemoryEmbeddingModel.id, MemoryEmbeddingModel.fact_id)
            )
        ).all()
        vectors = vector_table(profile_id)
        payloads = {row["fact_id"]: row["vector_blob"] for row in records}
        # Vec1 has ordinary INSERT/UPDATE/DELETE, not SQLite UPSERT. Replacement
        # stays inside this same transaction, including a failed/cancelled insert.
        await session.execute(
            vectors.delete().where(vectors.c.rowid.in_(row.id for row in accepted))
        )
        await session.execute(
            vectors.insert(),
            [
                {"rowid": row.id, "fact_id": row.fact_id, "vector": payloads[row.fact_id]}
                for row in accepted
            ],
        )

    async def load_authorized_vectors(
        self,
        *,
        scope: AuthorizedMemoryScope,
        profile_id: int,
        kinds: tuple[str, ...],
        temporal: MemoryTemporalIntent | None = None,
        query_blob: bytes,
        candidate_count: int,
    ) -> VectorCandidates:
        """Native candidate selection with current SQL authorization inside top-k."""
        fact_conditions: list[Any] = [
            authorized_fact_condition(scope),
            *strict_time_conditions(temporal),
            MemoryFactModel.status == "active",
            MemoryFactModel.review_state != "quarantined",
            or_(
                MemoryFactModel.valid_until.is_(None),
                MemoryFactModel.valid_until > datetime.now(UTC),
            ),
        ]
        if kinds:
            fact_conditions.append(MemoryFactModel.kind.in_(kinds))
        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            return await self._candidates(
                session,
                conditions=fact_conditions,
                profile_id=profile_id,
                query_blob=query_blob,
                candidate_count=candidate_count,
            )

    @staticmethod
    async def _candidates(
        session: AsyncSession,
        *,
        conditions: list[Any],
        profile_id: int,
        query_blob: bytes,
        candidate_count: int,
    ) -> VectorCandidates:
        fact, embedding = MemoryFactModel, MemoryEmbeddingModel
        active_ids = select(fact.id).where(*conditions)
        active_count = int(
            await session.scalar(select(func.count()).select_from(fact).where(*conditions)) or 0
        )
        vectors = vector_table(profile_id)
        indexed_count = int(
            await session.scalar(
                select(func.count())
                .select_from(embedding)
                .join(vectors, vectors.c.rowid == embedding.id)
                .where(embedding.profile_id == profile_id, embedding.fact_id.in_(active_ids))
            )
            or 0
        )
        ann = getattr(func, vectors.name)(
            query_blob, json.dumps({"K": candidate_count})
        ).table_valued("rowid", "fact_id")
        # IN must address a Vec1 metadata column, not rowid or a post-filtered LIMIT.
        selected = select(ann.c.rowid).where(ann.c.fact_id.in_(active_ids))
        rows = (
            await session.execute(
                select(
                    fact.id,
                    embedding.content_hash,
                    vectors.c.vector.label("vector_blob"),
                    fact.kind,
                    fact.category,
                    fact.memory_key,
                    fact.content,
                )
                .join(embedding, embedding.fact_id == fact.id)
                .join(vectors, vectors.c.rowid == embedding.id)
                .where(embedding.profile_id == profile_id, embedding.id.in_(selected), *conditions)
                .order_by(fact.id)
            )
        ).all()
        return VectorCandidates(
            tuple(StoredTargetVector(*row) for row in rows),
            active_count,
            indexed_count,
            indexed_count > candidate_count,
        )

    async def ensure_profile(
        self, profile: EmbeddingProviderProfile
    ) -> MemoryEmbeddingProfileRecord:
        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            created = await session.scalar(
                insert(MemoryEmbeddingProfileModel)
                .values(
                    fingerprint=profile.fingerprint,
                    provider_id=profile.provider_id,
                    model_id=profile.model_id,
                    dimensions=profile.dimensions,
                    output_type=profile.output_type,
                    document_template_version=profile.document_template_version,
                    endpoint_identity=profile.endpoint_identity,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["fingerprint"])
                .returning(MemoryEmbeddingProfileModel.id)
            )
            row = await session.scalar(
                select(MemoryEmbeddingProfileModel).where(
                    MemoryEmbeddingProfileModel.fingerprint == profile.fingerprint
                )
            )
            assert row is not None
            if created is not None:
                await create_profile_vectors(session, int(created))
        return MemoryEmbeddingProfileRecord(id=row.id, profile=profile, created_at=row.created_at)

    async def load_target_vectors(
        self,
        *,
        target: MemoryEntityTarget,
        profile_id: int,
        kinds: tuple[str, ...],
        temporal: MemoryTemporalIntent | None = None,
        query_blob: bytes,
        candidate_count: int,
    ) -> VectorCandidates:
        conditions: list[Any] = [
            *strict_time_conditions(temporal),
            MemoryFactModel.status == "active",
            MemoryFactModel.review_state != "quarantined",
            MemoryFactModel.scope_type == target.scope_type.value,
            or_(
                MemoryFactModel.valid_until.is_(None),
                MemoryFactModel.valid_until > datetime.now(UTC),
            ),
        ]
        if kinds:
            conditions.append(MemoryFactModel.kind.in_(kinds))
        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            try:
                owners = await resolve_fact_canonical_owners(session, target)
            except MemoryPartitionResolutionError:
                return VectorCandidates((), 0, 0, False)
            conditions.extend(
                (
                    person_family_condition(
                        MemoryFactModel.canonical_subject_person_id, owners.subject_person_id
                    )
                    if owners.subject_person_id is not None
                    else MemoryFactModel.canonical_subject_person_id.is_(None),
                    MemoryFactModel.canonical_subject_space_id == owners.subject_space_id
                    if owners.subject_space_id is not None
                    else MemoryFactModel.canonical_subject_space_id.is_(None),
                )
            )
            if target.scope_type.value == "self":
                current_visibility = and_(
                    MemoryFactModel.visibility_type
                    == (target.visibility_type.value if target.visibility_type else ""),
                    (
                        person_family_condition(
                            MemoryFactModel.canonical_visibility_person_id,
                            owners.visibility_person_id,
                        )
                        if owners.visibility_person_id is not None
                        else MemoryFactModel.canonical_visibility_person_id.is_(None)
                    ),
                    (
                        MemoryFactModel.canonical_visibility_space_id == owners.visibility_space_id
                        if owners.visibility_space_id is not None
                        else MemoryFactModel.canonical_visibility_space_id.is_(None)
                    ),
                )
                conditions.append(
                    or_(
                        and_(
                            MemoryFactModel.visibility_type == "global",
                            MemoryFactModel.canonical_visibility_person_id.is_(None),
                            MemoryFactModel.canonical_visibility_space_id.is_(None),
                        ),
                        current_visibility,
                    )
                )
            else:
                conditions.extend(
                    (
                        MemoryFactModel.visibility_type.is_(None),
                        MemoryFactModel.canonical_visibility_person_id.is_(None),
                        MemoryFactModel.canonical_visibility_space_id.is_(None),
                    )
                )
            return await self._candidates(
                session,
                conditions=conditions,
                profile_id=profile_id,
                query_blob=query_blob,
                candidate_count=candidate_count,
            )

    async def purge_old_profiles(self, *, current_profile_id: int, actor_person_id: str) -> int:
        async with self._database.immediate_session() as session:
            if not await person_is_superuser(session, actor_person_id):
                raise PermissionError("embedding_purge_requires_current_superuser")
            old_ids = tuple(
                await session.scalars(
                    select(MemoryEmbeddingProfileModel.id).where(
                        MemoryEmbeddingProfileModel.id != current_profile_id
                    )
                )
            )
            await session.execute(
                delete(MemoryEmbeddingProfileModel).where(
                    MemoryEmbeddingProfileModel.id.in_(old_ids)
                )
            )
            for identity in old_ids:
                await drop_profile_vectors(session, identity)
        return len(old_ids)

    async def active_fact_count(self) -> int:
        async with self._database.read_session() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryFactModel)
                    .where(
                        MemoryFactModel.status == "active",
                        MemoryFactModel.review_state != "quarantined",
                        or_(
                            MemoryFactModel.valid_until.is_(None),
                            MemoryFactModel.valid_until > datetime.now(UTC),
                        ),
                    )
                )
                or 0
            )

    async def local_health(
        self,
        *,
        current_profile_id: int,
        documents: EmbeddingDocumentBuilder,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        active = [
            MemoryFactModel.status == "active",
            MemoryFactModel.review_state != "quarantined",
            or_(MemoryFactModel.valid_until.is_(None), MemoryFactModel.valid_until > now),
        ]
        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            active_count = int(
                await session.scalar(
                    select(func.count()).select_from(MemoryFactModel).where(*active)
                )
                or 0
            )
            active_embedding_rows = await session.stream(
                select(
                    MemoryEmbeddingModel.content_hash,
                    MemoryFactModel.kind,
                    MemoryFactModel.category,
                    MemoryFactModel.memory_key,
                    MemoryFactModel.content,
                )
                .join(MemoryFactModel, MemoryFactModel.id == MemoryEmbeddingModel.fact_id)
                .where(MemoryEmbeddingModel.profile_id == current_profile_id, *active)
                .execution_options(yield_per=100)
            )
            matching_count = indexed_count = 0
            async for row in active_embedding_rows:
                indexed_count += 1
                matching_count += int(
                    row.content_hash
                    == documents.content_hash_fields(
                        kind=row.kind,
                        category=row.category,
                        memory_key=row.memory_key,
                        content=row.content,
                    )
                )
            inactive_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryEmbeddingModel)
                    .join(MemoryFactModel, MemoryFactModel.id == MemoryEmbeddingModel.fact_id)
                    .where(
                        MemoryEmbeddingModel.profile_id == current_profile_id,
                        MemoryFactModel.status.not_in(("active", "contested")),
                    )
                )
                or 0
            )
            orphan_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryEmbeddingModel)
                    .outerjoin(MemoryFactModel, MemoryFactModel.id == MemoryEmbeddingModel.fact_id)
                    .where(MemoryFactModel.id.is_(None))
                )
                or 0
            )
            job_count_rows = (
                await session.execute(
                    select(MemoryEmbeddingJobModel.status, func.count())
                    .where(MemoryEmbeddingJobModel.profile_id == current_profile_id)
                    .group_by(MemoryEmbeddingJobModel.status)
                )
            ).all()
            job_counts: dict[str, int] = {
                str(status): int(count) for status, count in job_count_rows
            }
            old_profile_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryEmbeddingProfileModel)
                    .where(MemoryEmbeddingProfileModel.id != current_profile_id)
                )
                or 0
            )
            last_success = await session.scalar(
                select(func.max(MemoryEmbeddingModel.updated_at)).where(
                    MemoryEmbeddingModel.profile_id == current_profile_id
                )
            )
            last_error = await session.scalar(
                select(MemoryEmbeddingJobModel.error_category)
                .where(
                    MemoryEmbeddingJobModel.profile_id == current_profile_id,
                    MemoryEmbeddingJobModel.error_category.is_not(None),
                )
                .order_by(MemoryEmbeddingJobModel.updated_at.desc())
                .limit(1)
            )
        return {
            "active_fact_count": active_count,
            "ready_embedding_count": matching_count,
            "pending_job_count": int(job_counts.get("pending", 0)),
            "processing_job_count": int(job_counts.get("processing", 0)),
            "failed_job_count": int(job_counts.get("failed", 0)),
            "stale_embedding_count": inactive_count + (indexed_count - matching_count),
            "orphan_embedding_count": orphan_count,
            "old_profile_count": old_profile_count,
            "last_success_at": last_success,
            "last_error_category": last_error,
        }
