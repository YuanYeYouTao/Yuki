"""Persistent TTL-backed staging for uncertain Memory V2 claims."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert

from qq_ai_bot.memory.enums import MemoryScopeType
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.job_claims import fence_memory_job_claim
from qq_ai_bot.memory.models import MemoryJob
from qq_ai_bot.memory.subjects import SubjectResolutionContext, SubjectResolver
from qq_ai_bot.memory.validation import normalize_memory_text
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemoryClaimCandidateEvidenceModel,
    MemoryClaimCandidateModel,
)
from qq_ai_bot.persistence.repository_records import EventRecord


@dataclass(frozen=True, slots=True)
class MemoryClaimCandidate:
    id: int
    candidate_type: str
    target_scope: MemoryScopeType
    subject_user_id: str | None
    group_id: str | None
    memory_key: str
    content: str
    subject_basis: str
    retention: str
    source_style: str
    confidence: float
    status: str
    evidence_count: int
    expires_at: datetime

    @property
    def ready_for_promotion(self) -> bool:
        return (
            self.candidate_type == "memory"
            and self.status == "pending"
            and self.subject_user_id is not None
            and self.evidence_count >= 2
        )


class MemoryClaimCandidateRepository:
    def __init__(self, database: Database, *, ttl_days: int = 7) -> None:
        self._database = database
        self._ttl_days = max(1, ttl_days)

    async def stage(
        self,
        claim: MemoryClaim,
        event: EventRecord,
        *,
        candidate_type: str,
        subject_context: SubjectResolutionContext | None,
        job: MemoryJob,
    ) -> MemoryClaimCandidate:
        if job.event_id != event.id:
            raise ValueError("memory job claim does not own the source event")
        if candidate_type not in {"memory", "self"}:
            raise ValueError("unknown memory candidate type")
        resolved = (
            None
            if candidate_type == "self"
            else SubjectResolver.resolve(
                event,
                subject_ref=claim.subject_ref,
                scope_type=claim.scope_type,
                context=subject_context,
            )
        )
        target_scope = MemoryScopeType.SELF if candidate_type == "self" else claim.scope_type
        target = {
            "scope": target_scope.value,
            "subject_user_id": (
                (event.private_peer_user_id or event.sender_user_id)
                if candidate_type == "self" and event.group_id is None
                else resolved.subject_user_id
                if resolved is not None
                else None
            ),
            "group_id": (
                event.group_id
                if candidate_type == "self"
                else resolved.group_id
                if resolved is not None
                else event.group_id
                if target_scope is MemoryScopeType.PERSON_GROUP
                else None
            ),
        }
        memory_key = normalize_memory_text(claim.memory_key, maximum=128).casefold()
        content = normalize_memory_text(claim.content, maximum=4000)
        target_fingerprint = _fingerprint(target)
        fingerprint = _fingerprint(
            {
                "candidate_type": candidate_type,
                "target": target,
                "memory_key": memory_key,
                "content": content.casefold(),
            }
        )
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=self._ttl_days)
        async with self._database.sessions() as session, session.begin():
            await fence_memory_job_claim(session, job)
            await session.execute(
                update(MemoryClaimCandidateModel)
                .where(
                    MemoryClaimCandidateModel.status == "pending",
                    MemoryClaimCandidateModel.expires_at <= now,
                )
                .values(status="expired", updated_at=now)
            )
            row = await session.scalar(
                select(MemoryClaimCandidateModel).where(
                    MemoryClaimCandidateModel.fingerprint == fingerprint
                )
            )
            if row is not None and row.status != "pending":
                if row.expires_at > now:
                    return _candidate(row)
                await session.execute(
                    delete(MemoryClaimCandidateEvidenceModel).where(
                        MemoryClaimCandidateEvidenceModel.candidate_id == row.id
                    )
                )
                row.candidate_type = candidate_type
                row.target_scope = target_scope.value
                row.subject_user_id = target["subject_user_id"]
                row.group_id = target["group_id"]
                row.target_fingerprint = target_fingerprint
                row.normalized_memory_key = memory_key
                row.content = content
                row.subject_basis = claim.subject_basis.value
                row.retention = claim.retention.value
                row.source_style = claim.source_style.value
                row.confidence = claim.confidence
                row.status = "pending"
                row.evidence_count = 1
                row.expires_at = expires_at
                row.updated_at = now
                added_evidence = True
            elif row is None:
                row = MemoryClaimCandidateModel(
                    fingerprint=fingerprint,
                    candidate_type=candidate_type,
                    target_scope=target_scope.value,
                    subject_user_id=target["subject_user_id"],
                    group_id=target["group_id"],
                    target_fingerprint=target_fingerprint,
                    normalized_memory_key=memory_key,
                    content=content,
                    subject_basis=claim.subject_basis.value,
                    retention=claim.retention.value,
                    source_style=claim.source_style.value,
                    confidence=claim.confidence,
                    status="pending",
                    evidence_count=1,
                    expires_at=expires_at,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                await session.flush()
                added_evidence = True
            else:
                result = await session.execute(
                    insert(MemoryClaimCandidateEvidenceModel)
                    .values(candidate_id=row.id, event_id=event.id, created_at=now)
                    .on_conflict_do_nothing(
                        index_elements=[
                            MemoryClaimCandidateEvidenceModel.candidate_id,
                            MemoryClaimCandidateEvidenceModel.event_id,
                        ]
                    )
                )
                added_evidence = bool(result.rowcount)  # type: ignore[attr-defined]
                if added_evidence:
                    row.evidence_count += 1
                    row.confidence = max(row.confidence, claim.confidence)
                    row.updated_at = now
                    row.expires_at = expires_at
            if row is not None and added_evidence and row.evidence_count == 1:
                await session.execute(
                    insert(MemoryClaimCandidateEvidenceModel)
                    .values(candidate_id=row.id, event_id=event.id, created_at=now)
                    .on_conflict_do_nothing(
                        index_elements=[
                            MemoryClaimCandidateEvidenceModel.candidate_id,
                            MemoryClaimCandidateEvidenceModel.event_id,
                        ]
                    )
                )
            await session.flush()
            return _candidate(row)

    async def list_pending_self(
        self,
        *,
        group_id: str | None,
        private_user_id: str | None = None,
        limit: int = 20,
    ) -> tuple[MemoryClaimCandidate, ...]:
        now = datetime.now(UTC)
        conditions = [
            MemoryClaimCandidateModel.candidate_type == "self",
            MemoryClaimCandidateModel.status == "pending",
            MemoryClaimCandidateModel.expires_at > now,
        ]
        if group_id is not None:
            conditions.append(MemoryClaimCandidateModel.group_id == group_id)
        else:
            conditions.extend(
                (
                    MemoryClaimCandidateModel.group_id.is_(None),
                    MemoryClaimCandidateModel.subject_user_id == private_user_id,
                )
            )
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(MemoryClaimCandidateModel)
                    .where(*conditions)
                    .order_by(
                        MemoryClaimCandidateModel.evidence_count.desc(),
                        MemoryClaimCandidateModel.created_at.asc(),
                    )
                    .limit(max(1, min(limit, 100)))
                )
            ).all()
        return tuple(_candidate(row) for row in rows)

    async def set_status(
        self, candidate_id: int, status: str, *, job: MemoryJob | None = None
    ) -> bool:
        if status not in {"accepted", "rejected", "expired"}:
            raise ValueError("invalid memory candidate status")
        async with self._database.sessions() as session, session.begin():
            if job is not None:
                await fence_memory_job_claim(session, job)
            result = await session.execute(
                update(MemoryClaimCandidateModel)
                .where(
                    MemoryClaimCandidateModel.id == candidate_id,
                    MemoryClaimCandidateModel.status == "pending",
                )
                .values(status=status, updated_at=datetime.now(UTC))
            )
            return bool(getattr(result, "rowcount", 0))


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _candidate(row: MemoryClaimCandidateModel) -> MemoryClaimCandidate:
    return MemoryClaimCandidate(
        id=row.id,
        candidate_type=row.candidate_type,
        target_scope=MemoryScopeType(row.target_scope),
        subject_user_id=row.subject_user_id,
        group_id=row.group_id,
        memory_key=row.normalized_memory_key,
        content=row.content,
        subject_basis=row.subject_basis,
        retention=row.retention,
        source_style=row.source_style,
        confidence=row.confidence,
        status=row.status,
        evidence_count=row.evidence_count,
        expires_at=row.expires_at,
    )
