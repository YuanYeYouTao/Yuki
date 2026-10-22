"""Fence live Memory results with the identity returned by queue claiming."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.memory.enums import MemoryJobStatus
from yuki.memory.models import MemoryJob
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel, MemoryJobModel
from yuki.persistence.unit_of_work import next_updated_at


class MemoryJobClaimLost(RuntimeError):
    """The original claimant may no longer commit results or queue state."""


@dataclass(frozen=True)
class PreparedJobClaim:
    job_id: int
    status: str
    updated_at: datetime
    values: dict[str, Any] | None
    live_event_id: int | None = None


async def commit_job_claims(
    database: Database, claims: list[PreparedJobClaim]
) -> dict[int, datetime]:
    """Claim unchanged rows and return their actual post-admission ownership tokens."""
    accepted: dict[int, datetime] = {}
    if not claims:
        return accepted
    async with database.immediate_session() as session:
        for claim in claims:
            statement = (
                delete(MemoryJobModel)
                if claim.values is None
                else update(MemoryJobModel).values(
                    **{**claim.values, "updated_at": next_updated_at(claim.updated_at)}
                )
            )
            if claim.live_event_id is not None:
                statement = statement.where(
                    select(ChatEventModel.id)
                    .join(
                        CanonicalConversationModel,
                        CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
                    )
                    .where(
                        ChatEventModel.id == claim.live_event_id,
                        ChatEventModel.canonical_event_id.is_not(None),
                        ChatEventModel.id > CanonicalConversationModel.starts_after_event_id,
                        ChatEventModel.id
                        > CanonicalConversationModel.last_generation_change_event_id,
                    )
                    .exists()
                )
            row = (
                await session.execute(
                    statement.where(
                        MemoryJobModel.id == claim.job_id,
                        MemoryJobModel.status == claim.status,
                        MemoryJobModel.updated_at == claim.updated_at,
                    ).returning(MemoryJobModel.id, MemoryJobModel.updated_at)
                )
            ).one_or_none()
            if (
                row is not None
                and claim.values is not None
                and claim.values["status"] == MemoryJobStatus.PROCESSING.value
            ):
                accepted[row.id] = row.updated_at
    return accepted


def memory_job_claim_conditions(job: MemoryJob) -> tuple[ColumnElement[bool], ...]:
    return (
        MemoryJobModel.id == job.id,
        MemoryJobModel.event_id == job.event_id,
        MemoryJobModel.status == MemoryJobStatus.PROCESSING.value,
        MemoryJobModel.updated_at == job.updated_at,
    )


async def fence_memory_job_claim(session: AsyncSession, job: MemoryJob) -> None:
    """Check the original claimant in the already owned IMMEDIATE transaction."""
    if not await session.scalar(select(MemoryJobModel.id).where(*memory_job_claim_conditions(job))):
        raise MemoryJobClaimLost(f"memory job {job.id} claim lost")
