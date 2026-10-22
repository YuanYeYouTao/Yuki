"""Durable receipt handoff; stored sources are anchors, never renewed authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError

from yuki.persistence.database import Database
from yuki.sandbox.db_models import SandboxTaskContinuationModel, SandboxTaskRunModel

if TYPE_CHECKING:
    from yuki.config import Settings


def canonical_json(value: Any, *, limit: int) -> str:
    serialized = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if len(serialized.encode()) > limit:
        raise ValueError("sandbox_task_payload_too_large")
    return serialized


class SandboxTaskRepository:
    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        lookup_plugin_host: Callable[[str], Any] | None = None,
    ) -> None:
        self.database = database
        self._settings = settings
        self._lookup_plugin_host = lookup_plugin_host

    async def validate_source(self, task: SandboxTaskRunModel, request_id: str) -> None:
        from yuki.services.execution_sources import recover_execution_source

        await recover_execution_source(
            self.database,
            task.source_conversation_id,
            json.loads(task.source_json),
            request_id=request_id,
            lookup_plugin_host=self._lookup_plugin_host,
        )

    async def prepare(
        self, request_id: str, arguments: dict[str, Any], source: dict[str, Any]
    ) -> SandboxTaskRunModel:
        """Called by the host before socket submission, with host-derived source data."""
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
            raise ValueError("invalid_request_id")
        if not isinstance(source.get("conversation_id"), str):
            raise ValueError("invalid_task_source")
        conversation_id = str(UUID(source["conversation_id"]))
        from yuki.runtime.work_activation import current_work_control

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
            # Anchor the terminal receipt to the original validated Work lease.
            source = {
                **source,
                "conversation_id": control.lease.conversation_id,
                "generation": control.lease.generation,
            }
        if (
            not owned_work
            and not (
                isinstance(source.get("parent_execution_id"), str) and source["parent_execution_id"]
            )
            and (type(source.get("trigger_event_id")) is not int or source["trigger_event_id"] <= 0)
        ):
            raise ValueError("invalid_task_event_anchor")
        source_json = canonical_json(source, limit=65536)
        serialized = canonical_json(arguments, limit=262144)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
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
            async with self.database.immediate_session() as session:
                if owned_work:
                    from yuki.runtime.work_schema_v1 import work

                    assert control is not None
                    await control.repository._assert_lease(session, control.lease)
                    live = await session.scalar(
                        select(work.c.id).where(
                            work.c.id == source["work_id"],
                            work.c.generation == source["generation"],
                            work.c.state.not_in(("completed", "failed", "cancelled")),
                        )
                    )
                    if live is None:
                        raise ValueError("invalid_task_work_source")
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
            changed = await session.scalar(
                update(SandboxTaskRunModel)
                .where(SandboxTaskRunModel.request_id == request_id)
                .values(progress_json=payload)
                .returning(SandboxTaskRunModel.request_id)
            )
            if changed is None:
                raise ValueError("sandbox_task_source_deleted")

    async def by_peer(self, peer_id: str) -> SandboxTaskRunModel | None:
        async with self.database.sessions() as session:
            return cast(
                SandboxTaskRunModel | None,
                await session.scalar(
                    select(SandboxTaskRunModel)
                    .where(
                        func.json_extract(SandboxTaskRunModel.progress_json, "$.peer_id") == peer_id
                    )
                    .order_by(SandboxTaskRunModel.created_at)
                    .limit(1)
                ),
            )

    async def peers(self) -> tuple[str, ...]:
        peer = func.json_extract(SandboxTaskRunModel.progress_json, "$.peer_id")
        async with self.database.sessions() as session:
            return tuple(
                await session.scalars(
                    select(peer)
                    .where(peer.is_not(None), SandboxTaskRunModel.status == "waiting")
                    .distinct()
                )
            )

    async def receive_permission(self, event: dict[str, Any]) -> None:
        """Commit the original callback and wait delivery before acknowledging its cursor."""
        from yuki.conversation.canonical_db_models import CanonicalConversationModel
        from yuki.runtime.work_repository import TERMINAL, WorkRepository, encode_json
        from yuki.runtime.work_schema_v1 import inputs, work
        from yuki.runtime.work_wait import WorkWaitRepository

        async with self.database.immediate_session() as session:
            task = await session.get(SandboxTaskRunModel, event["request_id"])
            if task is None:
                raise ValueError("unknown_external_agent_request")
            progress = json.loads(task.progress_json)
            if progress.get("peer_id") != event["peer_id"]:
                raise ValueError("external_agent_event_peer_mismatch")
            if task.run_id not in (None, event["run_id"]):
                raise ValueError("external_agent_event_run_mismatch")
            task.run_id = event["run_id"]
            source = json.loads(task.source_json)
            owner = (
                (await session.execute(select(work).where(work.c.id == source["work_id"])))
                .mappings()
                .first()
            )
            if owner is None or owner["state"] in TERMINAL:
                return
            generation = await session.scalar(
                select(CanonicalConversationModel.generation).where(
                    CanonicalConversationModel.id == task.source_conversation_id
                )
            )
            if generation != source["generation"] or generation != owner["generation"]:
                return
            payload = {
                "text": encode_json({"kind": "acp_permission", **event}),
                "signal": True,
                "external_run_id": event["run_id"],
            }
            created = await session.scalar(
                insert(inputs)
                .values(
                    conversation_id=owner["conversation_id"],
                    generation=owner["generation"],
                    source_key=f"acp-permission:{event['peer_id']}:{event['event_id']}",
                    work_id=owner["id"],
                    kind="control",
                    ready=True,
                    payload_json=encode_json(payload),
                    created=datetime.now(UTC).timestamp(),
                )
                .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
                .returning(inputs.c.id)
            )
            if created is not None:
                await WorkWaitRepository(WorkRepository(self.database)).signal_external_action(
                    session, owner["id"], event["run_id"]
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
        if str(event.get("request_id", "")).startswith("control:"):
            from yuki.sandbox.control_completions import record_control_completion

            await record_control_completion(self.database, event)
            return
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
