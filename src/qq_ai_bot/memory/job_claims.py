"""Fence live Memory results with the identity returned by queue claiming."""

from __future__ import annotations

from sqlalchemy import ColumnElement, update
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.memory.enums import MemoryJobStatus
from qq_ai_bot.memory.models import MemoryJob
from qq_ai_bot.persistence.models import MemoryJobModel


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
    """Acquire the short result transaction's writer lock without renewing its claim.

    Call only after identity reads and external/model work. The conditional write
    prevents a reclaim between checking ownership and committing the result.
    """
    result = await session.execute(
        update(MemoryJobModel)
        .where(*memory_job_claim_conditions(job))
        .values(updated_at=MemoryJobModel.updated_at)
    )
    if not getattr(result, "rowcount", 0):
        raise MemoryJobClaimLost(f"memory job {job.id} claim lost")
