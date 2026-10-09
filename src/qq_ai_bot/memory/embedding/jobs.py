"""Persistent embedding job scheduling, reconciliation, and atomic completion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, delete, exists, literal, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult, Row
from sqlalchemy.sql.elements import ColumnElement

from qq_ai_bot.memory.embedding.models import (
    MemoryEmbeddingJob,
    MemoryEmbeddingProfileRecord,
)
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemoryEmbeddingJobModel,
    MemoryEmbeddingModel,
    MemoryEmbeddingProfileModel,
    MemoryFactModel,
)
from qq_ai_bot.persistence.unit_of_work import next_updated_at

_EMBEDDABLE_STATUSES = ("active", "contested")


@dataclass(frozen=True, slots=True)
class EmbeddingWrite:
    job_id: int
    fact_id: int
    content_hash: str
    vector_blob: bytes
    claimed_at: datetime


class MemoryEmbeddingJobRepository:
    def __init__(
        self,
        database: Database,
        *,
        profile: MemoryEmbeddingProfileRecord,
        documents: EmbeddingDocumentBuilder,
    ) -> None:
        self._database = database
        self.profile = profile
        self.documents = documents

    @staticmethod
    def _eligible(now: datetime) -> tuple[ColumnElement[bool], ...]:
        return (
            MemoryFactModel.status.in_(_EMBEDDABLE_STATUSES),
            MemoryFactModel.review_state != "quarantined",
            or_(MemoryFactModel.valid_until.is_(None), MemoryFactModel.valid_until > now),
        )

    @staticmethod
    def _fact_guard(row: Row[Any]) -> ColumnElement[bool]:
        fact = MemoryFactModel
        return exists(
            select(fact.id).where(
                fact.id == row.id,
                fact.updated_at == row.updated_at,
                fact.status == row.status,
                fact.review_state == row.review_state,
                fact.valid_until.is_(None)
                if row.valid_until is None
                else fact.valid_until == row.valid_until,
                fact.kind == row.kind,
                fact.category == row.category,
                fact.memory_key == row.memory_key,
                fact.content == row.content,
            )
        )

    async def _prepare_page(
        self,
        *,
        ids: tuple[int, ...] | None = None,
        after_id: int = 0,
        limit: int = 128,
        only_missing_or_changed: bool = False,
    ) -> tuple[Row[Any], ...]:
        fact, job, vector = MemoryFactModel, MemoryEmbeddingJobModel, MemoryEmbeddingModel
        query = (
            select(
                fact.id,
                fact.status,
                fact.review_state,
                fact.valid_until,
                fact.updated_at,
                fact.kind,
                fact.category,
                fact.memory_key,
                fact.content,
                vector.content_hash.label("vector_hash"),
                job.id.label("job_id"),
                job.content_hash.label("job_hash"),
                job.status.label("job_status"),
                job.updated_at.label("job_updated_at"),
                job.attempts.label("job_attempts"),
            )
            .outerjoin(
                vector, and_(vector.fact_id == fact.id, vector.profile_id == self.profile.id)
            )
            .outerjoin(job, and_(job.fact_id == fact.id, job.profile_id == self.profile.id))
            .where(fact.id > after_id, *self._eligible(datetime.now(UTC)))
            .order_by(fact.id)
            .limit(limit)
        )
        if ids is not None:
            query = query.where(fact.id.in_(ids))
        if only_missing_or_changed:
            query = query.where(or_(vector.id.is_(None), fact.updated_at > vector.updated_at))
        async with self._database.sessions() as reader:
            return tuple((await reader.execute(query)).all())

    async def _enqueue_prepared(
        self,
        rows: tuple[Row[Any], ...],
        *,
        force: bool = False,
        only_if_latest_profile: bool = False,
    ) -> int:
        candidates = []
        for row in rows:
            digest = self.documents.content_hash_fields(
                kind=row.kind, category=row.category, memory_key=row.memory_key, content=row.content
            )
            if row.vector_hash == digest and not force:
                continue
            if row.job_hash == digest and (
                row.job_status == "processing"
                or (not force and row.job_status in {"pending", "failed"})
            ):
                continue
            candidates.append((row, digest))
        if not candidates:
            return 0
        created = 0
        async with self._database.immediate_session() as writer:
            for row, digest in candidates:
                now = next_updated_at(row.job_updated_at)
                values = dict(
                    fact_id=row.id,
                    profile_id=self.profile.id,
                    content_hash=digest,
                    status="pending",
                    attempts=0 if row.job_hash != digest else row.job_attempts,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                    error_category=None,
                )
                conditions = [
                    self._fact_guard(row),
                    exists(
                        select(MemoryFactModel.id).where(
                            MemoryFactModel.id == row.id, *self._eligible(now)
                        )
                    ),
                ]
                if only_if_latest_profile:
                    conditions.append(
                        ~exists(
                            select(MemoryEmbeddingProfileModel.id).where(
                                MemoryEmbeddingProfileModel.id > self.profile.id
                            )
                        )
                    )
                statement = insert(MemoryEmbeddingJobModel).from_select(
                    list(values),
                    select(*(literal(value) for value in values.values())).where(*conditions),
                )
                if row.job_id is None:
                    statement = statement.on_conflict_do_nothing(
                        index_elements=["fact_id", "profile_id"]
                    )
                else:
                    job = MemoryEmbeddingJobModel
                    statement = statement.on_conflict_do_update(
                        index_elements=["fact_id", "profile_id"],
                        where=and_(
                            job.id == row.job_id,
                            job.updated_at == row.job_updated_at,
                            job.content_hash == row.job_hash,
                            job.status == row.job_status,
                        ),
                        set_={
                            key: value
                            for key, value in values.items()
                            if key not in {"fact_id", "profile_id", "created_at"}
                        },
                    )
                result = await writer.execute(statement)
                created += int(cast(CursorResult[Any], result).rowcount == 1)
        return created

    async def enqueue_fact(self, fact_id: int, *, force: bool = False) -> bool:
        rows = await self._prepare_page(ids=(fact_id,))
        return bool(await self._enqueue_prepared(rows, force=force))

    async def enqueue_facts(
        self, fact_ids: tuple[int, ...], *, only_if_latest_profile: bool = False
    ) -> int:
        created = 0
        for offset in range(0, len(fact_ids), 128):
            rows = await self._prepare_page(ids=fact_ids[offset : offset + 128])
            created += await self._enqueue_prepared(
                rows, only_if_latest_profile=only_if_latest_profile
            )
        return created

    async def reconcile(self, *, force: bool = False) -> int:
        """Page derived candidates; preserve in-flight claims and retry budgets."""
        created, after_id = 0, 0
        while True:
            rows = await self._prepare_page(after_id=after_id, only_missing_or_changed=not force)
            if not rows:
                return created
            after_id = rows[-1].id
            created += await self._enqueue_prepared(rows, force=force)

    async def recover_interrupted(self) -> int:
        """Only the first Worker start, before any document request can be in flight."""
        job = MemoryEmbeddingJobModel
        recovered, after_id = 0, 0
        while True:
            async with self._database.sessions() as reader:
                rows = tuple(
                    await reader.scalars(
                        select(job)
                        .where(
                            job.id > after_id,
                            job.profile_id == self.profile.id,
                            job.status == "processing",
                        )
                        .order_by(job.id)
                        .limit(128)
                    )
                )
            if not rows:
                return recovered
            after_id = rows[-1].id
            claims = tuple(self._project(row) for row in rows)
            async with self._database.immediate_session() as writer:
                for claim in claims:
                    now = next_updated_at(claim.updated_at)
                    result = await writer.execute(
                        update(job)
                        .where(*self._claim_guard(claim))
                        .values(
                            status="pending",
                            next_attempt_at=now,
                            updated_at=now,
                            error_category="embedding_worker_interrupted",
                        )
                    )
                    recovered += int(cast(CursorResult[Any], result).rowcount == 1)

    async def claim(self, *, limit: int) -> tuple[MemoryEmbeddingJob, ...]:
        if limit <= 0:
            return ()
        job = MemoryEmbeddingJobModel
        now = datetime.now(UTC)
        async with self._database.sessions() as reader:
            ids = tuple(
                await reader.scalars(
                    select(job.id)
                    .where(
                        job.profile_id == self.profile.id,
                        job.status == "pending",
                        job.next_attempt_at <= now,
                    )
                    .order_by(job.id)
                    .limit(min(limit, 256))
                )
            )
        if not ids:
            return ()
        async with self._database.immediate_session() as writer:
            now = datetime.now(UTC)
            rows = tuple(
                await writer.scalars(
                    update(job)
                    .where(
                        job.id.in_(ids),
                        job.profile_id == self.profile.id,
                        job.status == "pending",
                        job.next_attempt_at <= now,
                    )
                    .values(status="processing", attempts=job.attempts + 1, updated_at=now)
                    .returning(job)
                )
            )
            return tuple(self._project(row) for row in rows)

    @staticmethod
    def _claim_guard(job: MemoryEmbeddingJob) -> tuple[ColumnElement[bool], ...]:
        model = MemoryEmbeddingJobModel
        return (
            model.id == job.id,
            model.fact_id == job.fact_id,
            model.profile_id == job.profile_id,
            model.content_hash == job.content_hash,
            model.status == "processing",
            model.updated_at == job.updated_at,
            model.attempts == job.attempts,
        )

    async def load_active_facts(
        self, jobs: tuple[MemoryEmbeddingJob, ...]
    ) -> dict[int, MemoryFactModel]:
        ids = tuple(job.fact_id for job in jobs)
        if not ids:
            return {}
        async with self._database.sessions() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(MemoryFactModel).where(
                            MemoryFactModel.id.in_(ids),
                            MemoryFactModel.status.in_(_EMBEDDABLE_STATUSES),
                            MemoryFactModel.review_state != "quarantined",
                            or_(
                                MemoryFactModel.valid_until.is_(None),
                                MemoryFactModel.valid_until > datetime.now(UTC),
                            ),
                        )
                    )
                ).all()
            )
        return {row.id: row for row in rows}

    async def complete(self, writes: tuple[EmbeddingWrite, ...]) -> int:
        completed = 0
        for offset in range(0, len(writes), 128):
            completed += await self._complete_page(writes[offset : offset + 128])
        return completed

    async def _complete_page(self, writes: tuple[EmbeddingWrite, ...]) -> int:
        if not writes:
            return 0
        fact = MemoryFactModel
        async with self._database.sessions() as reader:
            jobs = {
                row.id: self._project(row)
                for row in await reader.scalars(
                    select(MemoryEmbeddingJobModel).where(
                        MemoryEmbeddingJobModel.id.in_(tuple(item.job_id for item in writes)),
                        MemoryEmbeddingJobModel.profile_id == self.profile.id,
                    )
                )
            }
            facts = {
                row.id: row
                for row in await reader.execute(
                    select(
                        fact.id,
                        fact.status,
                        fact.review_state,
                        fact.valid_until,
                        fact.updated_at,
                        fact.kind,
                        fact.category,
                        fact.memory_key,
                        fact.content,
                    ).where(fact.id.in_(tuple(item.fact_id for item in writes)))
                )
            }
        prepared = []
        now = datetime.now(UTC)
        for item in writes:
            job, row = jobs.get(item.job_id), facts.get(item.fact_id)
            if (
                job is None
                or job.fact_id != item.fact_id
                or job.content_hash != item.content_hash
                or job.status.value != "processing"
                or job.updated_at != item.claimed_at
            ):
                continue
            valid_until = row.valid_until if row is not None else None
            if valid_until is not None and valid_until.tzinfo is None:
                valid_until = valid_until.replace(tzinfo=UTC)
            eligible = (
                row is not None
                and row.status in _EMBEDDABLE_STATUSES
                and row.review_state != "quarantined"
                and (valid_until is None or valid_until > now)
            )
            digest = (
                self.documents.content_hash_fields(
                    kind=row.kind,
                    category=row.category,
                    memory_key=row.memory_key,
                    content=row.content,
                )
                if eligible and row is not None
                else None
            )
            prepared.append((item, job, row, digest))
        if not prepared:
            return 0
        vectors = []
        async with self._database.immediate_session() as writer:
            for item, job, row, digest in prepared:
                stamp = next_updated_at(job.updated_at)
                values: dict[str, Any] = dict(status="done", updated_at=stamp, error_category=None)
                if digest is not None and digest != item.content_hash:
                    values.update(
                        status="pending", content_hash=digest, attempts=0, next_attempt_at=stamp
                    )
                conditions = list(self._claim_guard(job))
                if row is not None:
                    conditions.append(self._fact_guard(row))
                    if digest is not None:
                        conditions.append(
                            exists(select(fact.id).where(fact.id == row.id, *self._eligible(stamp)))
                        )
                result = await writer.execute(
                    update(MemoryEmbeddingJobModel).where(*conditions).values(**values)
                )
                if cast(CursorResult[Any], result).rowcount != 1:
                    # Only this original claim can be returned for a fresh read.
                    # A newer claim or replaced content remains untouched.
                    await writer.execute(
                        update(MemoryEmbeddingJobModel)
                        .where(*self._claim_guard(job))
                        .values(
                            status="pending",
                            updated_at=stamp,
                            next_attempt_at=stamp,
                            error_category="embedding_input_changed",
                        )
                    )
                    continue
                if digest != item.content_hash:
                    continue
                vectors.append(
                    dict(
                        fact_id=item.fact_id,
                        profile_id=self.profile.id,
                        content_hash=digest,
                        vector_blob=item.vector_blob,
                        created_at=stamp,
                        updated_at=stamp,
                    )
                )
            if vectors:
                statement = insert(MemoryEmbeddingModel)
                await writer.execute(
                    statement.on_conflict_do_update(
                        index_elements=["fact_id", "profile_id"],
                        set_={
                            key: getattr(statement.excluded, key)
                            for key in ("content_hash", "vector_blob", "updated_at")
                        },
                    ),
                    vectors,
                )
        return len(vectors)

    async def skip(self, job: MemoryEmbeddingJob) -> None:
        async with self._database.immediate_session() as writer:
            await writer.execute(
                update(MemoryEmbeddingJobModel)
                .where(*self._claim_guard(job))
                .values(
                    status="done", updated_at=next_updated_at(job.updated_at), error_category=None
                )
            )

    async def fail(
        self,
        job: MemoryEmbeddingJob,
        *,
        error_category: str,
        retryable: bool,
        initial_delay_seconds: float,
    ) -> None:
        now = next_updated_at(job.updated_at)
        async with self._database.immediate_session() as writer:
            await writer.execute(
                update(MemoryEmbeddingJobModel)
                .where(*self._claim_guard(job))
                .values(
                    status="pending" if retryable else "failed",
                    updated_at=now,
                    next_attempt_at=now + timedelta(seconds=initial_delay_seconds)
                    if retryable
                    else now,
                    error_category=error_category[:64],
                )
            )

    async def retry_failed(self) -> int:
        job = MemoryEmbeddingJobModel
        changed, after_id = 0, 0
        while True:
            async with self._database.sessions() as reader:
                rows = tuple(
                    await reader.execute(
                        select(job.id, job.updated_at, job.content_hash, job.attempts)
                        .where(
                            job.id > after_id,
                            job.profile_id == self.profile.id,
                            job.status == "failed",
                        )
                        .order_by(job.id)
                        .limit(128)
                    )
                )
            if not rows:
                return changed
            after_id = rows[-1].id
            async with self._database.immediate_session() as writer:
                for row in rows:
                    # Explicit retry resets its budget, but never reuses the
                    # previous claim identity when the wall clock stalls.
                    stamp = next_updated_at(row.updated_at)
                    result = await writer.execute(
                        update(job)
                        .where(
                            job.id == row.id,
                            job.profile_id == self.profile.id,
                            job.status == "failed",
                            job.updated_at == row.updated_at,
                            job.content_hash == row.content_hash,
                            job.attempts == row.attempts,
                        )
                        .values(
                            status="pending",
                            attempts=0,
                            next_attempt_at=stamp,
                            updated_at=stamp,
                            error_category=None,
                        )
                    )
                    changed += int(cast(CursorResult[Any], result).rowcount == 1)

    async def delete_for_old_profiles(self) -> int:
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(MemoryEmbeddingJobModel).where(
                    MemoryEmbeddingJobModel.profile_id != self.profile.id
                )
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    @staticmethod
    def _project(row: MemoryEmbeddingJobModel) -> MemoryEmbeddingJob:
        return MemoryEmbeddingJob(
            id=row.id,
            fact_id=row.fact_id,
            profile_id=row.profile_id,
            content_hash=row.content_hash,
            status=row.status,
            attempts=row.attempts,
            next_attempt_at=row.next_attempt_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            error_category=row.error_category,
        )
