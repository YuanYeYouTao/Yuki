"""Claim completed work once; uncertainty never grants permission to repeat effects."""

import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, text, update

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.sandbox.db_models import SandboxTaskContinuationModel, SandboxTaskRunModel


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

    async def claim(self, request_id: str) -> str | None:
        token = str(uuid4())
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(SandboxTaskContinuationModel)
                .where(
                    SandboxTaskContinuationModel.request_id == request_id,
                    SandboxTaskContinuationModel.state == "ready",
                )
                .values(
                    state="claimed",
                    claim_token=token,
                    attempts=SandboxTaskContinuationModel.attempts + 1,
                    reason=None,
                    updated_at=datetime.now(UTC),
                )
                .returning(SandboxTaskContinuationModel.request_id)
            )
            return token if result.scalar_one_or_none() is not None else None

    async def claim_group(self, request_id: str) -> tuple[str, tuple[str, ...]] | None:
        """Reserve all unobserved completions of one original invocation atomically.

        A running source, outstanding sibling job, or previous uncertain attempt
        cannot create a new execution budget or a second concurrent continuation.
        """
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            task = await session.get(SandboxTaskRunModel, request_id)
            if task is None:
                return None
            progress = json.loads(task.progress_json)
            group_id = progress.get("group_id")
            if not group_id or progress.get("phase") != "yielded":
                if progress.get("phase") != "running":
                    await session.execute(
                        update(SandboxTaskContinuationModel)
                        .where(
                            SandboxTaskContinuationModel.request_id == request_id,
                            SandboxTaskContinuationModel.state == "ready",
                        )
                        .values(
                            state="uncertain",
                            reason="source_usage_unavailable",
                            updated_at=datetime.now(UTC),
                        )
                    )
                    await session.commit()
                return None
            siblings = list(
                await session.scalars(
                    select(SandboxTaskRunModel).where(
                        SandboxTaskRunModel.source_conversation_id == task.source_conversation_id,
                        SandboxTaskRunModel.progress_json.contains(group_id),
                    )
                )
            )
            siblings = [
                row for row in siblings if json.loads(row.progress_json).get("group_id") == group_id
            ]
            selected: list[str] = []
            for row in siblings:
                if (
                    row.status != "completed"
                    or json.loads(row.progress_json).get("phase") != "yielded"
                ):
                    return None
                state = await session.get(SandboxTaskContinuationModel, row.request_id)
                if state is None or state.state in {"claimed", "uncertain"}:
                    return None
                if state.state == "ready":
                    selected.append(row.request_id)
            if request_id not in selected:
                return None
            token = str(uuid4())
            await session.execute(
                update(SandboxTaskContinuationModel)
                .where(
                    SandboxTaskContinuationModel.request_id.in_(selected),
                    SandboxTaskContinuationModel.state == "ready",
                )
                .values(
                    state="claimed",
                    claim_token=token,
                    attempts=SandboxTaskContinuationModel.attempts + 1,
                    reason=None,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()
            return token, tuple(selected)

    async def settle(self, request_id: str, token: str, *, state: str, reason: str) -> bool:
        """Only the current claim can finish, defer before execution, or record uncertainty."""
        if state not in {"ready", "finished", "uncertain", "blocked"} or not 1 <= len(reason) <= 64:
            raise ValueError("invalid_continuation_outcome")
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(SandboxTaskContinuationModel)
                .where(
                    SandboxTaskContinuationModel.request_id == request_id,
                    SandboxTaskContinuationModel.state == "claimed",
                    SandboxTaskContinuationModel.claim_token == token,
                )
                .values(state=state, reason=reason, updated_at=datetime.now(UTC))
                .returning(SandboxTaskContinuationModel.request_id)
            )
            return result.scalar_one_or_none() is not None

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

    async def recover_abandoned(self, process_id: str) -> None:
        """Completed input is replayable; an interrupted effect is never assumed absent."""
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            rows = await session.scalars(select(SandboxTaskRunModel))
            for row in rows:
                progress = json.loads(row.progress_json)
                if progress.get("phase") == "running" and progress.get("owner") != process_id:
                    progress["phase"] = "uncertain"
                    row.progress_json = json.dumps(progress, sort_keys=True)
            await session.execute(
                update(SandboxTaskContinuationModel)
                .where(
                    SandboxTaskContinuationModel.state == "claimed",
                )
                .values(
                    state="uncertain", reason="process_interrupted", updated_at=datetime.now(UTC)
                )
            )
            await session.commit()

    async def record_outcome(self, token: str, outcome: dict[str, object]) -> None:
        payload = json.dumps(outcome, ensure_ascii=False, sort_keys=True)
        if len(payload.encode()) > 65536:
            raise ValueError("sandbox_outcome_too_large")
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(SandboxTaskContinuationModel)
                .where(
                    SandboxTaskContinuationModel.state == "claimed",
                    SandboxTaskContinuationModel.claim_token == token,
                )
                .values(outcome_json=payload, updated_at=datetime.now(UTC))
            )
