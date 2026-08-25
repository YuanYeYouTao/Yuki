"""Persistence and bounded candidate discovery for background reflection."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import aliased

from qq_ai_bot.memory.reflection.models import (
    MemoryReflectionCandidate,
    MemoryReflectionIssue,
    MemoryReflectionJob,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MembershipModel,
    MemoryFactModel,
    MemoryReflectionJobModel,
)

_OPEN_FACT_STATUSES = ("active", "contested")


class MemoryReflectionRepository:
    """Find deterministic anomalies and manage restart-safe reflection jobs."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def discover(self, *, limit: int) -> tuple[MemoryReflectionCandidate, ...]:
        bounded = max(1, limit)
        left = aliased(MemoryFactModel, name="reflection_left")
        right = aliased(MemoryFactModel, name="reflection_right")
        same_subject = or_(
            and_(
                left.canonical_subject_person_id.is_(None),
                right.canonical_subject_person_id.is_(None),
            ),
            left.canonical_subject_person_id == right.canonical_subject_person_id,
        )
        same_group = or_(
            and_(
                left.canonical_subject_space_id.is_(None),
                right.canonical_subject_space_id.is_(None),
            ),
            left.canonical_subject_space_id == right.canonical_subject_space_id,
        )
        duplicate_query = (
            select(left.id, right.id)
            .join(
                right,
                and_(
                    left.id < right.id,
                    left.scope_type == right.scope_type,
                    same_subject,
                    same_group,
                    left.kind == right.kind,
                    left.normalized_content == right.normalized_content,
                    left.memory_key != right.memory_key,
                ),
            )
            .where(
                left.scope_type != "self",
                right.scope_type != "self",
                left.status.in_(_OPEN_FACT_STATUSES),
                right.status.in_(_OPEN_FACT_STATUSES),
                left.normalized_content != "",
            )
            .order_by(left.id, right.id)
            .limit(bounded)
        )
        contested_query = (
            select(MemoryFactModel.id)
            .where(
                MemoryFactModel.scope_type != "self",
                MemoryFactModel.status.in_(_OPEN_FACT_STATUSES),
                or_(
                    MemoryFactModel.status == "contested",
                    MemoryFactModel.conflict_state == "contested",
                ),
            )
            .order_by(MemoryFactModel.updated_at, MemoryFactModel.id)
            .limit(bounded)
        )
        member_exists = exists(
            select(MembershipModel.canonical_person_id).where(
                MembershipModel.canonical_person_id == MemoryFactModel.canonical_subject_person_id,
                MembershipModel.canonical_space_id == MemoryFactModel.canonical_subject_space_id,
            )
        )
        attribution_query = (
            select(MemoryFactModel.id)
            .where(
                MemoryFactModel.status.in_(_OPEN_FACT_STATUSES),
                MemoryFactModel.scope_type == "person_group",
                MemoryFactModel.canonical_subject_person_id.is_not(None),
                MemoryFactModel.canonical_subject_space_id.is_not(None),
                ~member_exists,
            )
            .order_by(MemoryFactModel.updated_at, MemoryFactModel.id)
            .limit(bounded)
        )
        async with self._database.sessions() as session:
            duplicate_rows = (await session.execute(duplicate_query)).all()
            attribution_ids = tuple(await session.scalars(attribution_query))
            contested_ids = tuple(await session.scalars(contested_query))
        candidates = [
            MemoryReflectionCandidate(MemoryReflectionIssue.DUPLICATE, first, second)
            for first, second in duplicate_rows
        ]
        candidates.extend(
            MemoryReflectionCandidate(MemoryReflectionIssue.ATTRIBUTION, fact_id)
            for fact_id in attribution_ids
        )
        candidates.extend(
            MemoryReflectionCandidate(MemoryReflectionIssue.CONTESTED, fact_id)
            for fact_id in contested_ids
        )
        unique: dict[tuple[MemoryReflectionIssue, int, int | None], MemoryReflectionCandidate] = {}
        from qq_ai_bot.identity.memory_guard import refuse_legacy_live_fact

        async with self._database.sessions() as guard_session:
            for candidate in candidates:
                if await refuse_legacy_live_fact(guard_session, candidate.fact_id):
                    continue
                if candidate.related_fact_id is not None and await refuse_legacy_live_fact(
                    guard_session, candidate.related_fact_id
                ):
                    continue
                key = (candidate.issue_type, candidate.fact_id, candidate.related_fact_id)
                unique.setdefault(key, candidate)
                if len(unique) >= bounded:
                    break
        return tuple(unique.values())

    async def enqueue(
        self,
        candidates: tuple[MemoryReflectionCandidate, ...],
        *,
        max_attempts: int = 3,
        now: datetime | None = None,
    ) -> int:
        if not candidates:
            return 0
        occurred_at = now or datetime.now(UTC)
        rows = [
            {
                "fingerprint": _candidate_fingerprint(candidate),
                "issue_type": candidate.issue_type.value,
                "fact_id": candidate.fact_id,
                "related_fact_id": candidate.related_fact_id,
                "status": "pending",
                "attempts": 0,
                "max_attempts": max(1, min(max_attempts, 20)),
                "next_attempt_at": occurred_at,
                "claimed_at": None,
                "error_category": None,
                "created_at": occurred_at,
                "updated_at": occurred_at,
                "completed_at": None,
            }
            for candidate in candidates
        ]
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                insert(MemoryReflectionJobModel)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["fingerprint"])
            )
            return max(0, int(getattr(result, "rowcount", 0) or 0))

    async def recover_stale(
        self,
        *,
        before: datetime,
        now: datetime | None = None,
    ) -> int:
        occurred_at = now or datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            recoverable = await session.execute(
                update(MemoryReflectionJobModel)
                .where(
                    MemoryReflectionJobModel.status == "processing",
                    MemoryReflectionJobModel.claimed_at <= before,
                    MemoryReflectionJobModel.attempts < MemoryReflectionJobModel.max_attempts,
                )
                .values(
                    status="pending",
                    next_attempt_at=occurred_at,
                    claimed_at=None,
                    error_category="worker_recovered",
                    updated_at=occurred_at,
                )
            )
            exhausted = await session.execute(
                update(MemoryReflectionJobModel)
                .where(
                    MemoryReflectionJobModel.status == "processing",
                    MemoryReflectionJobModel.claimed_at <= before,
                    MemoryReflectionJobModel.attempts >= MemoryReflectionJobModel.max_attempts,
                )
                .values(
                    status="failed",
                    claimed_at=None,
                    error_category="worker_recovery_exhausted",
                    updated_at=occurred_at,
                )
            )
            return sum(
                max(0, int(getattr(result, "rowcount", 0) or 0))
                for result in (recoverable, exhausted)
            )

    async def claim(
        self,
        *,
        limit: int,
        now: datetime | None = None,
    ) -> tuple[MemoryReflectionJob, ...]:
        occurred_at = now or datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            rows = list(
                (
                    await session.scalars(
                        select(MemoryReflectionJobModel)
                        .where(
                            MemoryReflectionJobModel.status == "pending",
                            MemoryReflectionJobModel.next_attempt_at <= occurred_at,
                            MemoryReflectionJobModel.attempts
                            < MemoryReflectionJobModel.max_attempts,
                        )
                        .order_by(
                            MemoryReflectionJobModel.next_attempt_at,
                            MemoryReflectionJobModel.id,
                        )
                        .limit(max(1, limit))
                    )
                ).all()
            )
            claimed: list[MemoryReflectionJob] = []
            for row in rows:
                row.status = "processing"
                row.attempts += 1
                row.claimed_at = occurred_at
                row.updated_at = occurred_at
                row.error_category = None
                claimed.append(_job(row))
            await session.flush()
            return tuple(claimed)

    async def complete(self, job_id: int, *, now: datetime | None = None) -> bool:
        occurred_at = now or datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(MemoryReflectionJobModel)
                .where(
                    MemoryReflectionJobModel.id == job_id,
                    MemoryReflectionJobModel.status == "processing",
                )
                .values(
                    status="completed",
                    completed_at=occurred_at,
                    claimed_at=None,
                    error_category=None,
                    updated_at=occurred_at,
                )
            )
            return bool(getattr(result, "rowcount", 0))

    async def fail(
        self,
        job_id: int,
        error_category: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        occurred_at = now or datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(MemoryReflectionJobModel, job_id)
            if row is None or row.status != "processing":
                return False
            exhausted = row.attempts >= row.max_attempts
            row.status = "failed" if exhausted else "pending"
            row.next_attempt_at = occurred_at + timedelta(
                seconds=min(3600, 30 * (2 ** max(0, row.attempts - 1)))
            )
            row.claimed_at = None
            row.error_category = error_category[:64]
            row.updated_at = occurred_at
            return True

    async def get(self, job_id: int) -> MemoryReflectionJob | None:
        async with self._database.sessions() as session:
            row = await session.get(MemoryReflectionJobModel, job_id)
        return _job(row) if row is not None else None


def _candidate_fingerprint(candidate: MemoryReflectionCandidate) -> str:
    payload = f"{candidate.issue_type.value}:{candidate.fact_id}:{candidate.related_fact_id or 0}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _job(row: MemoryReflectionJobModel) -> MemoryReflectionJob:
    return MemoryReflectionJob(
        id=row.id,
        fingerprint=row.fingerprint,
        issue_type=MemoryReflectionIssue(row.issue_type),
        fact_id=row.fact_id,
        related_fact_id=row.related_fact_id,
        status=row.status,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        next_attempt_at=_aware(row.next_attempt_at) or datetime.now(UTC),
        claimed_at=_aware(row.claimed_at),
        error_category=row.error_category,
        created_at=_aware(row.created_at) or datetime.now(UTC),
        updated_at=_aware(row.updated_at) or datetime.now(UTC),
        completed_at=_aware(row.completed_at),
    )
