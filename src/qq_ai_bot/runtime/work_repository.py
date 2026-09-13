"""Durable work state; all mutations are fenced by one scope activation lease."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.subagent_schema import children, media, media_refs
from qq_ai_bot.runtime.work_recovery_schema import recovery
from qq_ai_bot.runtime.work_schema_v1 import WORK_STATES, effects, inputs, journal, scope, work

TERMINAL = frozenset({"completed", "failed", "cancelled"})


class WorkCapacityError(ValueError):
    """A bounded private checkpoint cannot grow without a new context boundary."""


class WorkConflict(RuntimeError):
    """An obsolete activation or revision cannot change durable work."""


@dataclass(frozen=True, slots=True)
class WorkLease:
    conversation_id: str
    generation: int
    cancel_epoch: int
    fence: int
    owner: str
    work_id: str | None = None


def bounded_json(value: Any, limit: int = 65536) -> str:
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(result.encode()) > limit:
        raise ValueError("work_record_too_large")
    return result


class WorkRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _lease_table(lease: WorkLease) -> Any:
        return children if lease.work_id else scope

    @staticmethod
    def _fence(lease: WorkLease) -> Any:
        if lease.work_id:
            from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

            return and_(
                children.c.work_id == lease.work_id,
                children.c.cancel_epoch == lease.cancel_epoch,
                children.c.fence == lease.fence,
                children.c.owner == lease.owner,
                children.c.lease_until > time.time(),
                children.c.archived_at.is_(None),
                select(CanonicalConversationModel.id)
                .where(
                    CanonicalConversationModel.id == lease.conversation_id,
                    CanonicalConversationModel.generation == lease.generation,
                )
                .exists(),
                children.c.root_id.in_(select(work.c.id).where(work.c.state.not_in(TERMINAL))),
            )
        return and_(
            scope.c.conversation_id == lease.conversation_id,
            scope.c.generation == lease.generation,
            scope.c.cancel_epoch == lease.cancel_epoch,
            scope.c.fence == lease.fence,
            scope.c.owner == lease.owner,
            scope.c.lease_until > time.time(),
        )

    async def acquire(
        self, conversation_id: str, generation: int, *, seconds: float = 60
    ) -> WorkLease | None:
        if not 1 <= seconds <= 300:
            raise ValueError("invalid_work_lease_duration")
        now, owner = time.time(), str(uuid4())
        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

        async with self.database.immediate_session() as session:
            actual_generation = await session.scalar(
                select(CanonicalConversationModel.generation).where(
                    CanonicalConversationModel.id == conversation_id
                )
            )
            if actual_generation != generation:
                return None
            await session.execute(
                insert(scope)
                .values(
                    conversation_id=conversation_id,
                    generation=generation,
                )
                .on_conflict_do_nothing(index_elements=[scope.c.conversation_id])
            )
            row = (
                (
                    await session.execute(
                        update(scope)
                        .where(
                            scope.c.conversation_id == conversation_id,
                            scope.c.generation == generation,
                            scope.c.lease_until <= now,
                        )
                        .values(owner=owner, lease_until=now + seconds, fence=scope.c.fence + 1)
                        .returning(scope)
                    )
                )
                .mappings()
                .first()
            )
            return (
                WorkLease(conversation_id, generation, row["cancel_epoch"], row["fence"], owner)
                if row
                else None
            )

    async def renew(self, lease: WorkLease, *, seconds: float = 60) -> bool:
        if not 1 <= seconds <= 300:
            raise ValueError("invalid_work_lease_duration")
        async with self.database.sessions() as session, session.begin():
            return (
                await session.execute(
                    update(self._lease_table(lease))
                    .where(self._fence(lease))
                    .values(lease_until=time.time() + seconds)
                    .returning(self._lease_table(lease).c.fence)
                )
            ).first() is not None

    async def release(self, lease: WorkLease) -> None:
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(self._lease_table(lease))
                .where(self._fence(lease))
                .values(owner=None, lease_until=0)
            )

    async def valid(self, lease: WorkLease) -> bool:
        async with self.database.sessions() as session:
            return (
                await session.execute(
                    select(self._lease_table(lease).c.fence).where(self._fence(lease))
                )
            ).first() is not None

    async def _assert_lease(self, session: Any, lease: WorkLease) -> None:
        # A write obtains SQLite's transaction writer reservation, preventing a
        # cancel/acquire from interleaving between validation and the mutation.
        row = (
            await session.execute(
                update(self._lease_table(lease))
                .where(self._fence(lease))
                .values(fence=self._lease_table(lease).c.fence)
                .returning(self._lease_table(lease).c.fence)
            )
        ).first()
        if row is None:
            raise WorkConflict("work_activation_obsolete")

    async def accept(
        self,
        lease: WorkLease,
        *,
        source_key: str,
        source: dict[str, Any],
        goal: str,
        output_kind: str = "state_change",
        deliver_artifacts: bool = True,
        handoff_from: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= len(goal) <= 8192 or not 1 <= len(source_key) <= 256:
            raise ValueError("invalid_work_goal")
        if output_kind not in {"answer", "artifact", "state_change"}:
            raise ValueError("invalid_work_output_kind")
        source_json, now = bounded_json(source), time.time()
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            existing = await session.scalar(
                select(work.c.id).where(work.c.source_key == source_key)
            )
            if existing is None:
                count = await session.scalar(
                    select(func.count()).select_from(work).where(work.c.state.not_in(TERMINAL))
                )
                if int(count or 0) >= 128:
                    raise WorkCapacityError("active_work_capacity")
                scope_count = await session.scalar(
                    select(func.count())
                    .select_from(work)
                    .where(
                        work.c.state.not_in(TERMINAL),
                        work.c.conversation_id == lease.conversation_id,
                    )
                )
                if int(scope_count or 0) >= 16:
                    raise WorkCapacityError("conversation_work_capacity")
            await session.execute(
                insert(work)
                .values(
                    id=str(uuid4()),
                    conversation_id=lease.conversation_id,
                    generation=lease.generation,
                    source_key=source_key,
                    source_json=source_json,
                    goal=goal,
                    output_kind=output_kind,
                    deliver_artifacts=deliver_artifacts,
                    state="queued" if handoff_from else "running",
                    created=now,
                    updated=now,
                )
                .on_conflict_do_nothing(index_elements=[work.c.source_key])
            )
            row = (
                (await session.execute(select(work).where(work.c.source_key == source_key)))
                .mappings()
                .one()
            )
            if (
                row["conversation_id"] != lease.conversation_id
                or row["generation"] != lease.generation
                or row["source_json"] != source_json
            ):
                raise WorkConflict("work_source_conflict")
            if row["state"] in TERMINAL:
                raise WorkConflict("work_already_terminal")
            if handoff_from is not None:
                previous = (
                    (await session.execute(select(work).where(work.c.id == handoff_from)))
                    .mappings()
                    .one()
                )
                if (
                    previous["id"] == row["id"]
                    or previous["conversation_id"] != lease.conversation_id
                    or previous["generation"] != lease.generation
                    or previous["state"] in TERMINAL
                    or lease.work_id
                ):
                    raise WorkConflict("work_handoff_source_invalid")
                checkpoint = json.loads(previous["checkpoint_json"])
                checkpoint["handoff_work_id"] = row["id"]
                await session.execute(
                    update(work)
                    .where(work.c.id == handoff_from)
                    .values(checkpoint_json=bounded_json(checkpoint), updated=now)
                )
            return dict(row)

    async def by_source(self, source_key: str) -> dict[str, Any] | None:
        async with self.database.sessions() as session:
            row = (
                (await session.execute(select(work).where(work.c.source_key == source_key)))
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def get(self, identity: str) -> dict[str, Any] | None:
        async with self.database.sessions() as session:
            row = (
                (await session.execute(select(work).where(work.c.id == identity)))
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def active(self, conversation_id: str, generation: int) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(work)
                        .where(
                            work.c.conversation_id == conversation_id,
                            work.c.generation == generation,
                            work.c.state.not_in(TERMINAL),
                            work.c.id.not_in(select(children.c.work_id)),
                        )
                        .order_by(work.c.created)
                        .limit(32)
                    )
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]

    async def transition(
        self,
        lease: WorkLease,
        identity: str,
        revision: int,
        state: str,
        *,
        reason: str | None = None,
        goal: str | None = None,
        exit_reason: str | None = None,
    ) -> dict[str, Any]:
        if state not in WORK_STATES or (goal is not None and not 1 <= len(goal) <= 8192):
            raise ValueError("invalid_work_transition")
        if reason is not None and len(reason) > 128:
            raise ValueError("invalid_work_reason")
        values: dict[str, Any] = {
            "state": state,
            "reason": reason,
            "revision": revision + 1,
            "updated": time.time(),
        }
        if goal is not None:
            values["goal"] = goal
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            if state in {"completed", "waiting_user", "waiting_external"}:
                mailbox = await session.scalar(
                    select(inputs.c.id)
                    .where(
                        inputs.c.work_id == identity,
                        inputs.c.state.in_(("pending", "staged")),
                        inputs.c.ready.is_(True),
                    )
                    .limit(1)
                )
                if mailbox is not None:
                    values.update(state="queued", reason="work_input_arrived")
                    if exit_reason is not None:
                        exit_reason = "waiting_input"
            row = (
                (
                    await session.execute(
                        update(work)
                        .where(
                            work.c.id == identity,
                            work.c.conversation_id == lease.conversation_id,
                            work.c.generation == lease.generation,
                            work.c.revision == revision,
                            work.c.state.not_in(TERMINAL),
                        )
                        .values(**values)
                        .returning(work)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise WorkConflict("work_revision_conflict")
            if exit_reason is not None:
                detail = dict(
                    work_id=identity,
                    activation_id=lease.owner,
                    exit_reason=exit_reason,
                    stage="activation",
                    updated=time.time(),
                    not_before=0,
                )
                if exit_reason in {
                    "segment_budget",
                    "completed",
                    "waiting_input",
                    "waiting_external",
                }:
                    detail.update(attempts=0, failure_json="{}")
                await session.execute(
                    insert(recovery)
                    .values(**detail)
                    .on_conflict_do_update(index_elements=[recovery.c.work_id], set_=detail)
                )
            return dict(row)

    async def checkpoint(
        self,
        lease: WorkLease,
        identity: str,
        payload: dict[str, Any] | None,
        *,
        models: int = 0,
        tools: int = 0,
        messages: int = 0,
        active_seconds: float = 0,
        evidence: list[dict[str, Any]] | None = None,
    ) -> None:
        if min(models, tools, messages, active_seconds) < 0:
            raise ValueError("invalid_work_usage")
        serialized = bounded_json(payload, 1024 * 1024) if payload is not None else None
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            if models or tools:
                from qq_ai_bot.runtime.work_budget import charge

                await charge(session, identity, models=models, tools=tools)
            row = (
                await session.execute(
                    update(work)
                    .where(
                        work.c.id == identity,
                        work.c.conversation_id == lease.conversation_id,
                        work.c.generation == lease.generation,
                        work.c.state.not_in(TERMINAL),
                    )
                    .values(
                        checkpoint_json=serialized
                        if serialized is not None
                        else (
                            func.json_set(
                                work.c.checkpoint_json,
                                "$.execution_evidence",
                                func.json(bounded_json(evidence)),
                            )
                            if evidence is not None
                            else work.c.checkpoint_json
                        ),
                        updated=time.time(),
                        model_requests=work.c.model_requests + models,
                        tool_calls=work.c.tool_calls + tools,
                        sent_messages=work.c.sent_messages + messages,
                        active_seconds=work.c.active_seconds + active_seconds,
                    )
                    .returning(work.c.id)
                )
            ).first()
            if row is None:
                raise WorkConflict("work_checkpoint_obsolete")

    async def enqueue(
        self,
        conversation_id: str,
        generation: int,
        source_key: str,
        *,
        kind: str,
        event_id: int | None = None,
        work_id: str | None = None,
        ready: bool = True,
        resume: tuple[WorkLease, dict[str, Any]] | None = None,
    ) -> int:
        from qq_ai_bot.runtime.execution_receipts import PROCESS_ID

        if not 1 <= len(source_key) <= 256 or kind not in {"message", "completion", "control"}:
            raise ValueError("invalid_work_input")
        async with self.database.immediate_session() as session:
            if resume is not None:
                owned, _payload = resume
                await self._assert_lease(session, owned)
                if (
                    owned.work_id
                    or owned.conversation_id != conversation_id
                    or owned.generation != generation
                ):
                    raise WorkConflict("work_resume_scope_mismatch")
            existing = await session.scalar(
                select(inputs.c.id).where(inputs.c.source_key == source_key)
            )
            if existing is None:
                count = await session.scalar(
                    select(func.count())
                    .select_from(inputs)
                    .where(
                        inputs.c.conversation_id == conversation_id,
                        inputs.c.state.in_(("pending", "staged")),
                    )
                )
                if int(count or 0) >= 128:
                    raise WorkCapacityError("work_input_capacity")
            await session.execute(
                insert(inputs)
                .values(
                    conversation_id=conversation_id,
                    generation=generation,
                    source_key=source_key,
                    kind=kind,
                    event_id=event_id,
                    work_id=work_id,
                    ready=ready,
                    payload_json=bounded_json(resume[1], 32768) if resume else "{}",
                    prepare_owner=None if ready else PROCESS_ID,
                    created=time.time(),
                )
                .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
            )
            row = (
                (await session.execute(select(inputs).where(inputs.c.source_key == source_key)))
                .mappings()
                .one()
            )
            if any(
                row[key] != value
                for key, value in {
                    "conversation_id": conversation_id,
                    "generation": generation,
                    "kind": kind,
                    "event_id": event_id,
                    "work_id": work_id,
                }.items()
            ):
                raise WorkConflict("work_input_conflict")
            if resume is not None and row["state"] == "pending":
                updated = await session.scalar(
                    update(work)
                    .where(
                        work.c.id == work_id,
                        work.c.conversation_id == conversation_id,
                        work.c.generation == generation,
                        work.c.state.not_in(TERMINAL),
                    )
                    .values(
                        state="queued",
                        reason="explicit_resume",
                        revision=work.c.revision + 1,
                        updated=time.time(),
                    )
                    .returning(work.c.id)
                )
                if updated is None:
                    raise WorkConflict("work_resume_obsolete")
            return int(row["id"])

    async def prepare_input(self, identity: int, payload: dict[str, Any]) -> None:
        serialized = bounded_json(payload, 32768)
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(inputs)
                .where(
                    inputs.c.id == identity,
                    inputs.c.state == "pending",
                    inputs.c.ready.is_(False),
                )
                .values(payload_json=serialized, ready=True)
            )
            await session.execute(
                update(work)
                .where(
                    work.c.id.in_(
                        select(inputs.c.work_id).where(
                            inputs.c.id == identity, inputs.c.ready.is_(True)
                        )
                    ),
                    work.c.state == "waiting_external",
                )
                .values(
                    state="queued",
                    reason="input_prepared",
                    updated=time.time(),
                    revision=work.c.revision + 1,
                )
            )

    async def pending(
        self, lease: WorkLease, *, limit: int = 8, work_id: str | None = None
    ) -> list[dict[str, Any]]:
        if lease.work_id and work_id not in {None, lease.work_id}:
            raise WorkConflict("worker_mail_not_owned")
        target = work_id or lease.work_id
        async with self.database.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(inputs)
                        .where(
                            inputs.c.conversation_id == lease.conversation_id,
                            inputs.c.generation == lease.generation,
                            inputs.c.state == "pending",
                            inputs.c.work_id == target
                            if target
                            else or_(
                                inputs.c.work_id.is_(None),
                                inputs.c.work_id.not_in(select(children.c.work_id)),
                            ),
                        )
                        .order_by(inputs.c.id)
                        .limit(min(32, max(1, limit)))
                    )
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]

    async def stage(self, lease: WorkLease, ids: list[int], attempt_id: str) -> None:
        if not ids or len(set(ids)) != len(ids) or len(ids) > 32:
            raise ValueError("invalid_work_input_batch")
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            rows = (
                await session.execute(
                    update(inputs)
                    .where(
                        inputs.c.id.in_(ids),
                        inputs.c.conversation_id == lease.conversation_id,
                        inputs.c.generation == lease.generation,
                        inputs.c.state == "pending",
                    )
                    .values(state="staged", attempt_id=attempt_id)
                    .returning(inputs.c.id)
                )
            ).all()
            if len(rows) != len(ids):
                raise WorkConflict("work_input_already_staged")

    async def consume(self, lease: WorkLease, attempt_id: str) -> None:
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            await session.execute(
                update(inputs)
                .where(
                    inputs.c.conversation_id == lease.conversation_id,
                    inputs.c.generation == lease.generation,
                    inputs.c.attempt_id == attempt_id,
                    inputs.c.state == "staged",
                )
                .values(state="consumed")
            )

    async def cancel(
        self,
        conversation_id: str,
        *,
        generation: int | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        """Hard boundary only; ordinary arrivals never revoke activation leases."""
        if session is None:
            async with self.database.sessions() as owned, owned.begin():
                await self.cancel(conversation_id, generation=generation, session=owned)
            return
        await self.cancel_in_session(session, conversation_id, generation=generation)

    @staticmethod
    async def cancel_in_session(
        session: AsyncSession,
        conversation_id: str,
        *,
        generation: int | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "cancel_epoch": scope.c.cancel_epoch + 1,
            "fence": scope.c.fence + 1,
            "owner": None,
            "lease_until": 0,
        }
        if generation is not None:
            values["generation"] = generation
        await session.execute(
            update(scope).where(scope.c.conversation_id == conversation_id).values(**values)
        )
        await session.execute(
            update(children)
            .where(
                children.c.work_id.in_(
                    select(work.c.id).where(work.c.conversation_id == conversation_id)
                )
            )
            .values(owner=None, lease_until=0, cancel_epoch=children.c.cancel_epoch + 1)
        )
        await session.execute(
            update(work)
            .where(
                work.c.conversation_id == conversation_id,
                work.c.state.not_in(TERMINAL),
            )
            .values(
                state="cancelled",
                reason="hard_boundary",
                revision=work.c.revision + 1,
                updated=time.time(),
            )
        )
        await session.execute(
            update(inputs)
            .where(
                inputs.c.conversation_id == conversation_id,
                inputs.c.state.in_(("pending", "staged")),
            )
            .values(state="cancelled")
        )

    @staticmethod
    async def purge_scope(session: AsyncSession, conversation_id: str) -> None:
        """Privacy cleanup runs in the canonical deletion transaction.

        Affected group work can contain the deleted person's quoted content too,
        so discard its snapshots instead of trying to redact model projections.
        """
        identities = select(work.c.id).where(work.c.conversation_id == conversation_id)
        await session.execute(delete(journal).where(journal.c.work_id.in_(identities)))
        await session.execute(delete(effects).where(effects.c.work_id.in_(identities)))
        await session.execute(delete(inputs).where(inputs.c.conversation_id == conversation_id))
        await session.execute(delete(children).where(children.c.work_id.in_(identities)))
        await session.execute(delete(work).where(work.c.conversation_id == conversation_id))
        await session.execute(
            delete(media).where(media.c.sha256.not_in(select(media_refs.c.sha256)))
        )
        await session.execute(delete(scope).where(scope.c.conversation_id == conversation_id))

    async def route_child_completion(self, request_id: str) -> None:
        """Atomically hand a child receipt to its parent, or retire an unparented receipt."""
        from datetime import UTC, datetime

        from qq_ai_bot.sandbox.db_models import SandboxTaskContinuationModel, SandboxTaskRunModel

        async with self.database.immediate_session() as session:
            task = await session.get(SandboxTaskRunModel, request_id)
            receipt = await session.get(SandboxTaskContinuationModel, request_id)
            if task is None or receipt is None or receipt.state != "ready":
                return
            source = json.loads(task.source_json)
            if not source.get("work_id"):
                receipt.state, receipt.reason = "blocked", "legacy_continuation_retired"
                receipt.updated_at = datetime.now(UTC)
                return
            parent = (
                (await session.execute(select(work).where(work.c.id == source.get("work_id"))))
                .mappings()
                .first()
            )
            if parent is None or parent["state"] in TERMINAL:
                receipt.state, receipt.reason = "blocked", "work_terminal_or_deleted"
                return
            if (
                task.source_conversation_id != parent["conversation_id"]
                or source.get("generation") != parent["generation"]
            ):
                raise WorkConflict("work_child_source_mismatch")
            completion = json.loads(task.completion_json or "{}")
            payload = {
                "kind": "sandbox_completion",
                "request_id": request_id,
                "run_id": task.run_id,
                "status": completion.get("status"),
                "pending": False,
                "exit_code": completion.get("exit_code"),
                "detail": "查询原 run_id 的输出与产物，不能重跑原命令。",
            }
            await session.execute(
                insert(inputs)
                .values(
                    conversation_id=parent["conversation_id"],
                    generation=parent["generation"],
                    source_key=f"completion:{request_id}",
                    work_id=parent["id"],
                    kind="completion",
                    ready=True,
                    payload_json=bounded_json({"text": json.dumps(payload, ensure_ascii=False)}),
                    created=time.time(),
                )
                .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
            )
            await session.execute(
                update(work)
                .where(
                    work.c.id == parent["id"],
                    work.c.state == "waiting_external",
                )
                .values(state="queued", revision=work.c.revision + 1, updated=time.time())
            )
            receipt.state, receipt.reason = "observed", "forwarded_to_parent_work"
            receipt.updated_at = datetime.now(UTC)

    async def completed_children(
        self, lease: WorkLease, work_id: str, run_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Read only terminal host receipts belonging to this fenced parent."""
        from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel

        if not run_ids:
            return []
        async with self.database.sessions() as session:
            if await session.scalar(select(scope.c.fence).where(self._fence(lease))) is None:
                raise WorkConflict("work_activation_obsolete")
            rows = await session.scalars(
                select(SandboxTaskRunModel).where(
                    SandboxTaskRunModel.source_conversation_id == lease.conversation_id,
                    SandboxTaskRunModel.run_id.in_(run_ids[:32]),
                    SandboxTaskRunModel.completion_json.is_not(None),
                )
            )
            results = []
            for row in rows:
                source = json.loads(row.source_json)
                if source.get("work_id") != work_id or source.get("generation") != lease.generation:
                    continue
                value = json.loads(row.completion_json or "{}")
                if value.get("status") not in {"completed", "succeeded", "failed", "cancelled"}:
                    continue
                results.append(
                    {
                        "run_id": row.run_id,
                        "pending": False,
                        **{
                            key: value[key]
                            for key in ("status", "exit_code", "error", "artifact_id", "artifacts")
                            if key in value
                        },
                    }
                )
            return results

    async def repair_abandoned_inputs(self, process_id: str) -> None:
        from qq_ai_bot.persistence.models import ChatEventModel

        async with self.database.immediate_session() as session:
            rows = (
                (
                    await session.execute(
                        select(inputs)
                        .where(
                            inputs.c.state == "pending",
                            inputs.c.ready.is_(False),
                            or_(
                                inputs.c.prepare_owner != process_id,
                                inputs.c.created < time.time() - 120,
                            ),
                        )
                        .limit(128)
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                event = (
                    await session.get(ChatEventModel, row["event_id"]) if row["event_id"] else None
                )
                if event is None:
                    await session.execute(
                        update(inputs).where(inputs.c.id == row["id"]).values(state="cancelled")
                    )
                else:
                    content = (
                        "[输入准备因重启中断；附件尚未读取，按 event_id 查询原消息]\n"
                        f"{event.content[:5000]}"
                    )
                    await session.execute(
                        update(inputs)
                        .where(inputs.c.id == row["id"])
                        .values(
                            ready=True,
                            payload_json=bounded_json({"text": content}, 32768),
                        )
                    )

    async def reclaim_terminal(self) -> None:
        """Keep the latest 128 terminal work receipts; never evict an active work."""
        from qq_ai_bot.runtime.subagent_schema import media, media_refs

        async with self.database.sessions() as session:
            orphaned = list(
                await session.scalars(
                    select(media.c.sha256)
                    .where(
                        ~select(media_refs.c.sha256)
                        .where(media_refs.c.sha256 == media.c.sha256)
                        .exists()
                    )
                    .limit(64)
                )
            )
        async with self.database.immediate_session() as session:
            if orphaned:
                await session.execute(
                    delete(media).where(
                        media.c.sha256.in_(orphaned),
                        media.c.sha256.not_in(select(media_refs.c.sha256)),
                    )
                )
            selected = list(
                await session.scalars(
                    select(work.c.id)
                    .where(
                        work.c.state.in_(TERMINAL),
                        func.json_extract(work.c.checkpoint_json, "$.archived").is_(None),
                        # Synchronous callers can return before their work finishes.
                        # Give stable handles seven days for result consumption even
                        # when a busy conversation creates more than 128 other works.
                        or_(
                            func.json_extract(work.c.source_json, "$.delivery_contract")
                            != "return_to_caller",
                            func.json_extract(work.c.source_json, "$.delivery_contract").is_(None),
                            work.c.updated < time.time() - 7 * 86400,
                        ),
                        work.c.id.not_in(select(children.c.work_id)),
                        work.c.id.not_in(select(children.c.root_id)),
                    )
                    .order_by(work.c.updated.desc())
                    .offset(128)
                    .limit(256)
                )
            )
            if not selected:
                return
            await session.execute(delete(journal).where(journal.c.work_id.in_(selected)))
            await session.execute(delete(inputs).where(inputs.c.work_id.in_(selected)))
            await session.execute(delete(effects).where(effects.c.work_id.in_(selected)))
            await session.execute(delete(media_refs).where(media_refs.c.work_id.in_(selected)))
            # Stable invocation identities outlive their detailed result. Keep a
            # small tombstone so replaying an old callback cannot execute anew.
            await session.execute(
                update(work)
                .where(
                    work.c.id.in_(selected),
                    func.json_extract(work.c.source_json, "$.delivery_contract")
                    == "return_to_caller",
                )
                .values(
                    checkpoint_json='{"archived":true}',
                    goal=func.substr(work.c.goal, 1, 512),
                    source_json=func.json_remove(
                        work.c.source_json,
                        "$.instruction",
                        "$.context_data",
                    ),
                )
            )
            await session.execute(
                delete(work).where(
                    work.c.id.in_(selected),
                    func.json_extract(work.c.checkpoint_json, "$.archived").is_(None),
                )
            )

    async def discard_input(self, identity: int) -> None:
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(inputs)
                .where(inputs.c.id == identity, inputs.c.state == "pending")
                .values(state="cancelled", payload_json="{}")
            )

    async def prepare_effect(self, lease: WorkLease, identity: str, key: str, kind: str) -> bool:
        """False means an intent already exists, not that it is safe to send again."""
        now = time.time()
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            row = (
                await session.execute(
                    select(work.c.id).where(
                        work.c.id == identity,
                        work.c.conversation_id == lease.conversation_id,
                        work.c.generation == lease.generation,
                        work.c.state.not_in(TERMINAL),
                    )
                )
            ).first()
            if row is None:
                raise WorkConflict("work_effect_obsolete")
            inserted = (
                await session.execute(
                    insert(effects)
                    .values(
                        effect_key=key,
                        work_id=identity,
                        kind=kind,
                        state="prepared",
                        created=now,
                        updated=now,
                    )
                    .on_conflict_do_nothing(index_elements=[effects.c.effect_key])
                    .returning(effects.c.effect_key)
                )
            ).first()
            return inserted is not None

    async def record_effect(self, key: str, state: str, receipt: dict[str, Any]) -> None:
        # A late receipt must survive cancellation. It records an already-issued
        # effect, never authorizes another one, so no current lease is required.
        if state not in {"accepted", "failed", "unknown"}:
            raise ValueError("invalid_work_effect_state")
        serialized = bounded_json(receipt)
        async with self.database.sessions() as session, session.begin():
            row = (
                await session.execute(
                    update(effects)
                    .where(
                        effects.c.effect_key == key,
                        effects.c.state.in_(("prepared", "unknown")),
                    )
                    .values(state=state, receipt_json=serialized, updated=time.time())
                    .returning(effects.c.effect_key)
                )
            ).first()
            if row is None:
                existing = (
                    (await session.execute(select(effects).where(effects.c.effect_key == key)))
                    .mappings()
                    .first()
                )
                if existing and existing["state"] == "accepted":
                    previous = json.loads(existing["receipt_json"])
                    if state == "unknown":
                        return  # Later bookkeeping failure cannot undo transport acceptance.
                    if state == "accepted" and all(
                        receipt.get(k) == v for k, v in previous.items()
                    ):
                        await session.execute(
                            update(effects)
                            .where(effects.c.effect_key == key)
                            .values(receipt_json=serialized, updated=time.time())
                        )
                        return
                if (
                    not existing
                    or existing["state"] != state
                    or existing["receipt_json"] != serialized
                ):
                    raise WorkConflict("work_effect_receipt_conflict")
