"""Claim completed work once; uncertainty never grants permission to repeat effects."""

from datetime import UTC, datetime

from sqlalchemy import select, update

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.sandbox.db_models import SandboxTaskContinuationModel


class SandboxContinuationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def get(self, request_id: str) -> SandboxTaskContinuationModel | None:
        async with self.database.sessions() as session:
            return await session.get(SandboxTaskContinuationModel, request_id)

    async def ready(self, *, limit: int = 20) -> tuple[str, ...]:
        if not 1 <= limit <= 20:
            raise ValueError("invalid_continuation_limit")
        async with self.database.sessions() as session:
            return tuple(
                await session.scalars(
                    select(SandboxTaskContinuationModel.request_id)
                    .where(SandboxTaskContinuationModel.state == "ready")
                    .order_by(
                        SandboxTaskContinuationModel.updated_at,
                        SandboxTaskContinuationModel.request_id,
                    )
                    .limit(limit)
                )
            )

    async def observed(self, request_id: str) -> bool:
        """The original Agent consumed the terminal result; no extra turn is needed."""
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(SandboxTaskContinuationModel)
                .where(
                    SandboxTaskContinuationModel.request_id == request_id,
                    SandboxTaskContinuationModel.state == "ready",
                )
                .values(
                    state="observed", reason="original_turn_observed", updated_at=datetime.now(UTC)
                )
                .returning(SandboxTaskContinuationModel.request_id)
            )
            return result.scalar_one_or_none() is not None

    async def rotate(self, request_id: str) -> None:
        """Busy or invalid sources cannot starve later completions in a bounded scan."""
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(SandboxTaskContinuationModel)
                .where(
                    SandboxTaskContinuationModel.request_id == request_id,
                    SandboxTaskContinuationModel.state == "ready",
                )
                .values(updated_at=datetime.now(UTC))
            )

    async def retire_legacy(self) -> None:
        """Old detached continuations have no authority to resume after upgrade."""
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(SandboxTaskContinuationModel)
                .where(SandboxTaskContinuationModel.state == "claimed")
                .values(
                    state="blocked",
                    reason="legacy_continuation_retired",
                    updated_at=datetime.now(UTC),
                )
            )
