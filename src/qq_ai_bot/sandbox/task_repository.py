"""Durable receipt handoff; stored sources are anchors, never renewed authority."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.sandbox.db_models import SandboxTaskContinuationModel, SandboxTaskRunModel


def canonical_json(value: Any, *, limit: int) -> str:
    serialized = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if len(serialized.encode()) > limit:
        raise ValueError("sandbox_task_payload_too_large")
    return serialized


class SandboxTaskRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def prepare(
        self, request_id: str, arguments: dict[str, Any], source: dict[str, Any]
    ) -> SandboxTaskRunModel:
        """Called by the host before socket submission, with host-derived source data."""
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
            raise ValueError("invalid_request_id")
        if not isinstance(source.get("conversation_id"), str):
            raise ValueError("invalid_task_source")
        conversation_id = str(UUID(source["conversation_id"]))
        from qq_ai_bot.runtime.work_activation import current_work_control

        control = current_work_control.get()
        owned_work = bool(
            control is not None
            and control.current is not None
            and source.get("work_id") == control.current["id"]
            and conversation_id == control.lease.conversation_id
        )
        if owned_work:
            assert control is not None
            await control.validate()
            if not await control.repository.valid(control.lease):
                raise ValueError("invalid_task_work_lease")
            generation = source.get("generation")
            if generation is not None and (
                type(generation) is not int or generation != control.lease.generation
            ):
                raise ValueError("invalid_task_work_generation")
            # Automation has no chat snapshot. Anchor its receipt to the validated
            # work lease, without modifying the caller's source or delegation.
            source = {
                **source,
                "conversation_id": control.lease.conversation_id,
                "generation": control.lease.generation,
            }
        elif source.get("origin") not in {
            "user_message",
            "autonomous_group",
            "scheduled_automation",
        } or not source.get("actor_user_id"):
            raise ValueError("invalid_task_source")
        if not owned_work and source["origin"] == "scheduled_automation":
            raise ValueError("scheduled_task_requires_work_lease")
        elif not owned_work and (
            type(source.get("trigger_event_id")) is not int or source["trigger_event_id"] <= 0
        ):
            raise ValueError("invalid_task_event_anchor")
        source_json = canonical_json(source, limit=65536)
        digest = hashlib.sha256(canonical_json(arguments, limit=262144).encode()).hexdigest()
        now = datetime.now(UTC)
        row = SandboxTaskRunModel(
            request_id=request_id,
            source_conversation_id=conversation_id,
            source_json=source_json,
            payload_hash=digest,
            status="waiting",
            created_at=now,
            updated_at=now,
        )
        try:
            async with self.database.sessions() as session, session.begin():
                session.add(row)
                await session.flush()
        except IntegrityError:
            # Concurrent retries may both attempt the insert. Only an identical
            # existing receipt is reusable; foreign-key failures still propagate.
            existing = await self.get(request_id)
            if existing is None:
                raise
            if existing.payload_hash != digest or existing.source_json != source_json:
                raise ValueError("sandbox_task_idempotency_conflict") from None
            return existing
        return row

    async def get(self, request_id: str) -> SandboxTaskRunModel | None:
        async with self.database.sessions() as session:
            return await session.get(SandboxTaskRunModel, request_id)

    async def by_run(self, run_id: str) -> SandboxTaskRunModel | None:
        async with self.database.sessions() as session:
            return cast(
                SandboxTaskRunModel | None,
                await session.scalar(
                    select(SandboxTaskRunModel).where(SandboxTaskRunModel.run_id == run_id)
                ),
            )

    async def checkpoint(self, request_id: str, progress: dict[str, Any]) -> None:
        payload = canonical_json(progress, limit=16384)
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(SandboxTaskRunModel)
                .where(SandboxTaskRunModel.request_id == request_id)
                .values(progress_json=payload)
            )

    async def bind_run(self, request_id: str, run_id: str) -> None:
        """Keep a returned/rediscovered Manager identity without changing the source."""
        run_id = str(UUID(run_id))
        async with self.database.sessions() as session, session.begin():
            changed = await session.execute(
                update(SandboxTaskRunModel)
                .where(
                    SandboxTaskRunModel.request_id == request_id,
                    or_(SandboxTaskRunModel.run_id.is_(None), SandboxTaskRunModel.run_id == run_id),
                )
                .values(run_id=run_id)
                .returning(SandboxTaskRunModel.request_id)
            )
            if changed.scalar_one_or_none() is None:
                raise ValueError("sandbox_task_run_binding_conflict")

    async def reject(self, request_id: str, result: dict[str, Any]) -> None:
        """A definitive admission rejection has no execution or continuation to recover."""
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(SandboxTaskRunModel)
                .where(
                    SandboxTaskRunModel.request_id == request_id,
                    SandboxTaskRunModel.status == "waiting",
                    SandboxTaskRunModel.run_id.is_(None),
                )
                .values(
                    status="completed",
                    completion_json=canonical_json(result, limit=16384),
                    updated_at=datetime.now(UTC),
                )
            )

    async def receive(self, event: dict[str, Any]) -> None:
        """Persist a completion before the caller may acknowledge it to Manager."""
        run_id = str(UUID(event["run_id"]))
        result = event.get("result")
        if (
            not isinstance(result, dict)
            or result.get("run_id") != run_id
            or result.get("status") not in {"succeeded", "failed", "cancelled"}
            or result.get("pending") is not False
        ):
            raise ValueError("invalid_task_completion")
        payload = canonical_json(result, limit=240000)
        request_id = event["request_id"]
        async with self.database.sessions() as session, session.begin():
            changed = await session.execute(
                update(SandboxTaskRunModel)
                .where(
                    SandboxTaskRunModel.request_id == request_id,
                    SandboxTaskRunModel.status == "waiting",
                    or_(SandboxTaskRunModel.run_id.is_(None), SandboxTaskRunModel.run_id == run_id),
                )
                .values(
                    run_id=run_id,
                    status="completed",
                    completion_json=payload,
                    updated_at=datetime.now(UTC),
                )
                .returning(SandboxTaskRunModel.request_id)
            )
            if changed.scalar_one_or_none() is not None:
                session.add(
                    SandboxTaskContinuationModel(
                        request_id=request_id,
                        state="blocked" if result["status"] == "cancelled" else "ready",
                        attempts=0,
                        reason="task_cancelled" if result["status"] == "cancelled" else None,
                        updated_at=datetime.now(UTC),
                    )
                )
                return
            existing = await session.get(SandboxTaskRunModel, request_id)
            if existing is None:
                raise ValueError("unknown_task_completion")
            if existing.run_id != run_id or existing.completion_json != payload:
                raise ValueError("conflicting_task_completion")
