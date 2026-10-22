"""Fence live Memory results with the identity returned by queue claiming."""

from __future__ import annotations

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.memory.enums import MemoryJobStatus
from yuki.memory.models import MemoryJob
from yuki.persistence.models import MemoryJobModel


class MemoryJobClaimLost(RuntimeError):
    """The original claimant may no longer commit results or queue state."""


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
