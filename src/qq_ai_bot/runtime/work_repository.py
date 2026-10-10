"""Durable work state; all mutations are fenced by one scope activation lease."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import (
    and_,
    case,
    column,
    delete,
    false,
    func,
    literal_column,
    or_,
    select,
    table,
    text,
    update,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.models import WorkStorageRuntimeConfig
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupEmergencyOverlayModel,
    CanonicalConversationRollupJobModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.domain.messages import ChatImage
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import ConversationReadVersion
from qq_ai_bot.runtime.subagent_schema import children, media, media_refs
from qq_ai_bot.runtime.work_recovery_schema import recovery
from qq_ai_bot.runtime.work_schema_v1 import (
    WORK_STATES,
    effects,
    inputs,
    journal,
    scope,
    work,
)
from qq_ai_bot.runtime.work_wait_schema import waits

if TYPE_CHECKING:
    from qq_ai_bot.runtime.protocol_store import CodeSnapshotBinding, ProtocolStore

TERMINAL = frozenset({"completed", "failed", "cancelled"})

# Owner rows referencing runtime_work (RESTRICT). A lightweight column view keeps
# core Work storage free of plugin ORM imports while honoring reference order.
_OWNER_JOBS = table(
    "plugin_background_turn_jobs",
    column("work_id"),
    column("status"),
    column("last_error_category"),
    column("lease_until"),
)


class WorkCapacityError(ValueError):
    """A model window or physical evidence resource cannot admit this operation."""


class WorkConflict(RuntimeError):
    """An obsolete activation or revision cannot change durable work."""

    @property
    def code(self) -> str:
        """Only stable internal reason slugs may enter recovery records or logs."""
        reason = str(self)
        return (
            reason if re.fullmatch(r"[a-z][a-z0-9_]{1,79}", reason) else "work_conflict_unspecified"
        )


@dataclass(frozen=True, slots=True)
class WorkLease:
    conversation_id: str
    generation: int
    cancel_epoch: int
    fence: int
    owner: str
    work_id: str | None = None


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


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
                children.c.lease_until > (func.julianday("now") - 2440587.5) * 86400,
                children.c.archived_at.is_(None),
                select(CanonicalConversationModel.id)
                .where(
                    CanonicalConversationModel.id == lease.conversation_id,
                    CanonicalConversationModel.generation == lease.generation,
                )
                .exists(),
            )
        return and_(
            scope.c.conversation_id == lease.conversation_id,
            scope.c.generation == lease.generation,
            scope.c.cancel_epoch == lease.cancel_epoch,
            scope.c.fence == lease.fence,
            scope.c.owner == lease.owner,
            scope.c.lease_until > (func.julianday("now") - 2440587.5) * 86400,
        )

    async def acquire(
        self, conversation_id: str, generation: int, *, seconds: float = 60
    ) -> WorkLease | None:
        if not 1 <= seconds <= 300:
            raise ValueError("invalid_work_lease_duration")
        owner = str(uuid4())
        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

        # A busy scope or obsolete generation has no state to change. Inspect
        # them without joining the SQLite writer queue; this is only a rejection
        # fast path, never permission to acquire a lease.
        async with self.database.sessions() as reader:
            observed = (
                await reader.execute(
                    select(
                        CanonicalConversationModel.generation,
                        scope.c.generation.label("scope_generation"),
                        (scope.c.lease_until > (func.julianday("now") - 2440587.5) * 86400).label(
                            "occupied"
                        ),
                    )
                    .outerjoin(scope, scope.c.conversation_id == CanonicalConversationModel.id)
                    .where(CanonicalConversationModel.id == conversation_id)
                )
            ).first()
        if (
            observed is None
            or observed.generation != generation
            or (observed.scope_generation is not None and observed.scope_generation != generation)
            or observed.occupied
        ):
            return None

        async with self.database.immediate_session() as session:
            now = time.time()
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
        if not await self.valid(lease):
            return False
        async with self.database.sessions() as session, session.begin():
            return (
                await session.execute(
                    update(self._lease_table(lease))
                    .where(self._fence(lease))
                    .values(lease_until=(func.julianday("now") - 2440587.5) * 86400 + seconds)
                    .returning(self._lease_table(lease).c.fence)
                )
            ).first() is not None

    async def lease_expiry(self, lease: WorkLease) -> float | None:
        """Read the confirmed deadline for this owner without acquiring a writer."""
        async with self.database.sessions() as session:
            value = await session.scalar(
                select(self._lease_table(lease).c.lease_until).where(self._fence(lease))
            )
        return float(value) if value is not None else None

    async def release(self, lease: WorkLease) -> None:
        # Expiry/cancellation/replacement already removed this activation's
        # authority. Its cleanup must not wait on a writer for a zero-row UPDATE.
        # A live lease still uses the full fence at SQL execution after waiting.
        if not await self.valid(lease):
            return
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

    async def _assert_lease_readonly(self, session: AsyncSession, lease: WorkLease) -> None:
        """Check the original lease in a read snapshot, without authorizing a write.

        The caller owns the snapshot. Actual mutations must still use
        ``_assert_lease`` in their writer transaction, including SQL-time expiry.
        """
        row = (
            await session.execute(
                select(self._lease_table(lease).c.fence).where(self._fence(lease))
            )
        ).first()
        if row is None:
            raise WorkConflict("work_activation_obsolete")

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
        parent_work_id: str | None = None,
        identity: str | None = None,
        handoff_from: str | None = None,
        reporting: str | None = None,
        initial_state: str = "running",
    ) -> dict[str, Any]:
        async with self.database.sessions() as session, session.begin():
            return await self.accept_in_session(
                session,
                lease,
                source_key=source_key,
                source=source,
                goal=goal,
                parent_work_id=parent_work_id,
                identity=identity,
                handoff_from=handoff_from,
                reporting=reporting,
                initial_state=initial_state,
            )

    async def accept_in_session(
        self,
        session: AsyncSession,
        lease: WorkLease,
        *,
        source_key: str,
        source: dict[str, Any],
        goal: str,
        parent_work_id: str | None = None,
        identity: str | None = None,
        handoff_from: str | None = None,
        reporting: str | None = None,
        initial_state: str = "running",
    ) -> dict[str, Any]:
        """Admit in a writer transaction owned by the caller.

        Only an owner that must commit its own durable link together with the
        first admission uses this directly; the lease is asserted here.
        """
        if not goal.strip() or not source_key:
            raise ValueError("invalid_work_goal")
        if reporting is not None and reporting not in {"interactive", "quiet"}:
            raise ValueError("work_reporting_invalid")
        if initial_state not in {"running", "queued"}:
            raise ValueError("invalid_work_initial_state")
        source_json, now = encode_json(source), time.time()
        initial_checkpoint = encode_json(
            {"communication": {"reporting": reporting}} if reporting is not None else {}
        )
        await self._assert_lease(session, lease)
        if parent_work_id is not None:
            parent = (
                (
                    await session.execute(
                        select(work).where(
                            work.c.id == parent_work_id,
                            work.c.conversation_id == lease.conversation_id,
                            work.c.generation == lease.generation,
                            work.c.state.not_in(TERMINAL),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if parent is None or (lease.work_id is not None and lease.work_id != parent_work_id):
                raise WorkConflict("work_parent_source_invalid")
        elif source.get("origin") == "self_initiative" or source.get("principal_kind") == "self":
            if (
                source.get("origin") == "scheduled_automation"
                and source.get("principal_kind") == "self"
            ):
                from qq_ai_bot.persistence.models import AutomationModel, AutomationRunModel

                run = await session.get(AutomationRunModel, source.get("automation_run_id"))
                owner = await session.get(AutomationModel, run.automation_id) if run else None
                if (
                    run is None
                    or owner is None
                    or owner.creator_kind != "self"
                    or run.status != "running"
                    or source.get("automation_id") != owner.id
                    or source.get("conversation_id") != lease.conversation_id
                    or source.get("generation") != lease.generation
                    or source.get("actor_user_id")
                    or source.get("actor_person_id")
                    or source.get("trigger_event_id") is not None
                ):
                    raise WorkConflict("invalid_self_automation_work_admission")
            else:
                from qq_ai_bot.conversation.autonomy_db_models import InitiativeRunModel

                initiative_run = await session.get(
                    InitiativeRunModel, source.get("initiative_run_id")
                )
                if (
                    initiative_run is None
                    or initiative_run.state not in {"accepted", "running"}
                    or source.get("principal_kind") != "self"
                    or source.get("origin") != "self_initiative"
                    or source_key != f"initiative:{initiative_run.id}"
                    or initiative_run.conversation_id != lease.conversation_id
                    or initiative_run.generation != lease.generation
                    or source.get("conversation_id") != initiative_run.conversation_id
                    or source.get("generation") != initiative_run.generation
                    or source.get("presence_id") != initiative_run.presence_id
                    or source.get("space_id") != initiative_run.space_id
                    or source.get("actor_user_id")
                    or source.get("person_id")
                    or source.get("actor_person_id")
                    or source.get("trigger_event_id") is not None
                ):
                    raise WorkConflict("invalid_self_work_admission")
        await session.execute(
            insert(work)
            .values(
                id=identity or str(uuid4()),
                conversation_id=lease.conversation_id,
                generation=lease.generation,
                source_key=source_key,
                source_json=source_json,
                goal=goal,
                parent_work_id=parent_work_id,
                checkpoint_json=initial_checkpoint,
                state="queued" if handoff_from else initial_state,
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
            or row["parent_work_id"] != parent_work_id
        ):
            raise WorkConflict("work_source_conflict")
        if row["state"] in TERMINAL and parent_work_id is None:
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
                .values(checkpoint_json=encode_json(checkpoint), updated=now)
            )
        return dict(row)

    async def derive(
        self, lease: WorkLease, parent_work_id: str, key: str, goal: str
    ) -> dict[str, Any]:
        """Create a business child with the original source and call identity."""
        async with self.database.sessions() as reader:
            brief = await reader.scalar(
                select(children.c.brief_json).where(children.c.work_id == parent_work_id)
            )
        if brief is not None:
            from qq_ai_bot.runtime.subagent_repository import SubagentRepository

            child = await SubagentRepository(self).start(
                lease, parent_work_id, key, {**json.loads(brief), "goal": goal}
            )
            created = await self.get(child)
            assert created is not None
            return created
        async with self.database.immediate_session() as session:
            await self._assert_lease(session, lease)
            parent = (
                (
                    await session.execute(
                        select(work).where(
                            work.c.id == parent_work_id,
                            work.c.conversation_id == lease.conversation_id,
                            work.c.generation == lease.generation,
                        )
                    )
                )
                .mappings()
                .one()
            )
            source = json.loads(parent["source_json"])
            return await self.accept_in_session(
                session,
                lease,
                source_key=key,
                source=source,
                goal=goal,
                parent_work_id=parent_work_id,
                initial_state="queued",
            )

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
        if state not in WORK_STATES or (goal is not None and not goal.strip()):
            raise ValueError("invalid_work_transition")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("invalid_work_reason")
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            return await self.commit_state(
                session,
                lease,
                identity,
                state,
                revision=revision,
                reason=reason,
                goal=goal,
                exit_reason=exit_reason,
            )

    @staticmethod
    def _business_input_clause() -> Any:
        # Input ownership is already typed by its producer. Worker instructions
        # target that child; child results/notifications target their parent.
        return and_(
            or_(
                inputs.c.kind == "message",
                and_(
                    inputs.c.kind == "subagent",
                    func.json_extract(inputs.c.payload_json, "$.child_id") == inputs.c.work_id,
                ),
            ),
            func.coalesce(func.json_extract(inputs.c.payload_json, "$.signal"), 0) == 0,
        )

    async def has_pending_business_inputs(
        self, lease: WorkLease, work_id: str, *, input_ids: tuple[int, ...] | None = None
    ) -> bool:
        async with self.database.sessions() as session:
            await self._assert_lease_readonly(session, lease)
            query = select(inputs.c.id).where(
                inputs.c.work_id == work_id,
                inputs.c.conversation_id == lease.conversation_id,
                inputs.c.generation == lease.generation,
                inputs.c.state.in_(("pending", "staged")),
                self._business_input_clause(),
            )
            if input_ids is not None:
                query = query.where(inputs.c.id.in_(input_ids))
            return await session.scalar(query.limit(1)) is not None

    @staticmethod
    def _owned_execution_clause(identity: str) -> Any:
        from qq_ai_bot.runtime.work_tree import descendants
        from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel

        owned = descendants(identity, include_self=True)
        return or_(
            select(SandboxTaskRunModel.request_id)
            .where(
                func.json_extract(SandboxTaskRunModel.source_json, "$.work_id").in_(owned),
                SandboxTaskRunModel.status == "waiting",
                SandboxTaskRunModel.run_id.is_not(None),
            )
            .exists(),
            select(children.c.work_id)
            .join(work, work.c.id == children.c.work_id)
            .where(
                children.c.work_id.in_(owned),
                children.c.work_id != identity,
                children.c.owner.is_not(None),
                children.c.lease_until > (func.julianday("now") - 2440587.5) * 86400,
                work.c.state.not_in(TERMINAL),
            )
            .exists(),
        )

    async def has_owned_execution(self, lease: WorkLease, identity: str) -> bool:
        async with self.database.sessions() as session:
            await self._assert_lease_readonly(session, lease)
            return bool(await session.scalar(select(self._owned_execution_clause(identity))))

    async def commit_state(
        self,
        session: AsyncSession,
        lease: WorkLease,
        identity: str,
        state: str,
        *,
        revision: int | None,
        reason: str | None = None,
        goal: str | None = None,
        exit_reason: str | None = None,
        recovery_detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The single state writer shared by normal settlement and failure recovery.

        Callers own the transaction and lease assertion. Only newly authorized
        input and genuinely active owned execution can defer completion.
        """
        values: dict[str, Any] = {
            "state": state,
            "reason": reason,
            "revision": work.c.revision + 1 if revision is None else revision + 1,
            "updated": time.time(),
        }
        if goal is not None:
            values["goal"] = goal
        business_arrived = False
        draining = False
        if state in {"completed", "failed", "waiting_user", "waiting_external"}:
            mailbox = (
                await session.execute(
                    select(inputs.c.ready)
                    .where(
                        inputs.c.work_id == identity,
                        inputs.c.state.in_(("pending", "staged")),
                        self._business_input_clause(),
                    )
                    .order_by(inputs.c.id)
                    .limit(1)
                )
            ).first()
            if mailbox is not None:
                business_arrived = True
                # Admitted input keeps a live owner. A failure is retained in
                # recovery facts but cannot strand input on a terminal Work.
                ready = bool(mailbox[0])
                values.update(
                    state="queued" if ready else "waiting_external",
                    reason="work_input_arrived" if ready else "work_input_preparing",
                )
                if exit_reason is not None:
                    exit_reason = "waiting_input" if ready else "waiting_external"
        if values["state"] == "completed":
            if await session.scalar(select(self._owned_execution_clause(identity))):
                draining = True
                values.update(
                    state="waiting_external",
                    reason="work_owned_execution_pending",
                )
                if exit_reason is not None:
                    exit_reason = "waiting_external"
        if values["state"] != "running" and not draining:
            # Settlement consumes the accepted control; its durable payload moves
            # to the existing result/reason slots only when the state it proposed
            # is the state actually committed.
            accepted = "$.accepted_control"
            checkpoint: Any = func.json_remove(work.c.checkpoint_json, accepted)
            if values["state"] == "completed":
                checkpoint = case(
                    (
                        func.json_type(work.c.checkpoint_json, f"{accepted}.result") == "text",
                        func.json_set(
                            checkpoint,
                            "$.sync_result",
                            func.json_extract(work.c.checkpoint_json, f"{accepted}.result"),
                        ),
                    ),
                    else_=checkpoint,
                )
            if values["state"] in {"failed", "waiting_user"}:
                checkpoint = case(
                    (
                        func.json_type(work.c.checkpoint_json, f"{accepted}.reason") == "text",
                        func.json_set(
                            checkpoint,
                            "$.reason",
                            func.json_extract(work.c.checkpoint_json, f"{accepted}.reason"),
                        ),
                    ),
                    else_=checkpoint,
                )
            values["checkpoint_json"] = checkpoint
        conditions = [
            work.c.id == identity,
            work.c.conversation_id == lease.conversation_id,
            work.c.generation == lease.generation,
            work.c.state.not_in(TERMINAL),
        ]
        if revision is not None:
            conditions.append(work.c.revision == revision)
        row = (
            (
                await session.execute(
                    update(work).where(*conditions).values(**values).returning(work)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise WorkConflict("work_revision_conflict")
        if row["state"] in {"failed", "cancelled"} and not business_arrived:
            from qq_ai_bot.runtime.work_management import stop_owned_execution

            await stop_owned_execution(session, identity, reason)
        if recovery_detail is not None:
            detail = {**recovery_detail, "work_id": identity}
            if exit_reason is not None:
                detail["exit_reason"] = exit_reason
        elif exit_reason is not None:
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
        else:
            detail = None
        if detail is not None:
            await session.execute(
                insert(recovery)
                .values(**detail)
                .on_conflict_do_update(index_elements=[recovery.c.work_id], set_=detail)
            )
        if row["state"] in TERMINAL or goal is not None:
            await session.execute(
                update(waits)
                .where(waits.c.work_id == identity, waits.c.status == "active")
                .values(status="cancelled", updated=time.time())
            )
        if row["state"] in TERMINAL and row["parent_work_id"] is not None:
            # Worker notifications already belong to their original scheduler.
            # Plain business children use the same persistent parent inbox.
            worker = await session.scalar(
                select(children.c.work_id).where(children.c.work_id == identity)
            )
            if worker is None:
                await session.execute(
                    insert(inputs)
                    .values(
                        conversation_id=row["conversation_id"],
                        generation=row["generation"],
                        source_key=f"work-result:{identity}:{row['revision']}",
                        work_id=row["parent_work_id"],
                        kind="completion",
                        ready=True,
                        payload_json=encode_json(
                            {
                                "text": encode_json(
                                    {
                                        "work_id": identity,
                                        "state": row["state"],
                                        "reason": row["reason"],
                                        "result": json.loads(row["checkpoint_json"]).get(
                                            "sync_result"
                                        ),
                                    }
                                )
                            }
                        ),
                        created=time.time(),
                    )
                    .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
                )
                await session.execute(
                    update(work)
                    .where(
                        work.c.id == row["parent_work_id"],
                        work.c.state == "waiting_external",
                        ~select(waits.c.id)
                        .where(waits.c.work_id == work.c.id, waits.c.status == "active")
                        .exists(),
                    )
                    .values(state="queued", revision=work.c.revision + 1, updated=time.time())
                )
        return dict(row)

    async def accept_control(
        self, lease: WorkLease, identity: str, control: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Persist, or retire, the one host-owned accepted lifecycle decision."""
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            return await self.set_accepted_control(session, lease, identity, control)

    async def set_accepted_control(
        self,
        session: AsyncSession,
        lease: WorkLease,
        identity: str,
        control: dict[str, Any] | None,
    ) -> dict[str, Any]:
        path = "$.accepted_control"
        checkpoint = (
            func.json_set(work.c.checkpoint_json, path, func.json(encode_json(control)))
            if control is not None
            else func.json_remove(work.c.checkpoint_json, path)
        )
        row = (
            (
                await session.execute(
                    update(work)
                    .where(
                        work.c.id == identity,
                        work.c.conversation_id == lease.conversation_id,
                        work.c.generation == lease.generation,
                        work.c.state.not_in(TERMINAL),
                    )
                    .values(checkpoint_json=checkpoint, updated=time.time())
                    .returning(work)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise WorkConflict("work_checkpoint_obsolete")
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
    ) -> None:
        if min(models, tools, messages) < 0:
            raise ValueError("invalid_work_usage")
        serialized = encode_json(payload) if payload is not None else None
        replacement: Any = serialized
        if serialized is not None:
            for retained in ("communication", "context_note", "accepted_control"):
                path = f"$.{retained}"
                replacement = case(
                    (
                        func.json_type(work.c.checkpoint_json, path) == "object",
                        func.json_set(
                            replacement, path, func.json_extract(work.c.checkpoint_json, path)
                        ),
                    ),
                    else_=replacement,
                )
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
                        checkpoint_json=replacement
                        if serialized is not None
                        else work.c.checkpoint_json,
                        updated=time.time(),
                        model_requests=work.c.model_requests + models,
                        tool_calls=work.c.tool_calls + tools,
                        sent_messages=work.c.sent_messages + messages,
                    )
                    .returning(work.c.id)
                )
            ).first()
            if row is None:
                raise WorkConflict("work_checkpoint_obsolete")

    @staticmethod
    def encode_communication_updates(updates: dict[str, Any]) -> str:
        if set(updates) != {"reporting"} or updates["reporting"] not in (
            None,
            "interactive",
            "quiet",
        ):
            raise ValueError("work_reporting_invalid")
        return encode_json({"communication": updates})

    async def patch_context_note(
        self, lease: WorkLease, identity: str, expected_revision: int, note: dict[str, Any]
    ) -> dict[str, Any]:
        """CAS an optional note without altering task state, waiting or budget."""
        from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
        from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository

        encoded = encode_json(note)
        handles = tuple(note["artifact_handles"])
        async with self.database.immediate_session() as session:
            await self._assert_lease(session, lease)
            privacy = int(
                await session.scalar(
                    select(ExecutionTraceStateModel.privacy_generation).where(
                        ExecutionTraceStateModel.id == 1
                    )
                )
                or 0
            )
            if privacy != note["privacy_generation"]:
                raise WorkConflict("work_context_note_source_changed")
            if not await session.scalar(
                select(CanonicalConversationModel.id).where(
                    CanonicalConversationModel.id == lease.conversation_id,
                    CanonicalConversationModel.generation == lease.generation,
                    CanonicalConversationModel.prompt_source_revision == note["source_revision"],
                )
            ):
                raise WorkConflict("work_context_note_source_changed")
            row = (
                (
                    await session.execute(
                        update(work)
                        .where(
                            work.c.id == identity,
                            work.c.conversation_id == lease.conversation_id,
                            work.c.generation == lease.generation,
                            work.c.state.not_in(TERMINAL),
                            func.coalesce(
                                func.json_extract(
                                    work.c.checkpoint_json, "$.context_note.revision"
                                ),
                                0,
                            )
                            == expected_revision,
                        )
                        .values(
                            checkpoint_json=func.json_set(
                                work.c.checkpoint_json, "$.context_note", func.json(encoded)
                            ),
                            updated=time.time(),
                        )
                        .returning(work)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise WorkConflict("work_context_note_obsolete")
            await ToolArtifactRepository.add_refs(session, "work_note", identity, handles)
            from qq_ai_bot.tool_results.schema import artifact_refs

            await session.execute(
                delete(artifact_refs).where(
                    artifact_refs.c.owner_kind == "work_note",
                    artifact_refs.c.owner_id == identity,
                    artifact_refs.c.handle_id.not_in(handles),
                )
            )
            return dict(row)

    async def patch_communication(
        self, lease: WorkLease, identity: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Patch bounded host-owned communication facts without changing lifecycle."""
        serialized = self.encode_communication_updates(updates)
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            row = (
                (
                    await session.execute(
                        update(work)
                        .where(
                            work.c.id == identity,
                            work.c.conversation_id == lease.conversation_id,
                            work.c.generation == lease.generation,
                            work.c.state.not_in(TERMINAL),
                        )
                        .values(
                            checkpoint_json=func.json_patch(work.c.checkpoint_json, serialized),
                            updated=time.time(),
                        )
                        .returning(work)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise WorkConflict("work_checkpoint_obsolete")
            return dict(row)

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

        if not source_key or kind not in {"message", "completion", "control"}:
            raise ValueError("invalid_work_input")
        serialized = encode_json(resume[1]) if resume else "{}"

        def matches(row: Any) -> bool:
            return all(
                row[key] == value
                for key, value in {
                    "conversation_id": conversation_id,
                    "generation": generation,
                    "kind": kind,
                    "event_id": event_id,
                    "work_id": work_id,
                }.items()
            )

        # Explicit resume also changes the Work state and retains its writer/CAS.
        if resume is None:
            async with self.database.sessions() as reader:
                existing_row = (
                    (await reader.execute(select(inputs).where(inputs.c.source_key == source_key)))
                    .mappings()
                    .first()
                )
                if existing_row is not None:
                    if not matches(existing_row):
                        raise WorkConflict("work_input_conflict")
                    return int(existing_row["id"])
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
                    payload_json=serialized,
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
            if not matches(row):
                raise WorkConflict("work_input_conflict")
            if resume is not None and row["payload_json"] != serialized:
                raise WorkConflict("work_input_conflict")
            if resume is not None and row["state"] == "pending":
                updated = await session.scalar(
                    update(work)
                    .where(
                        work.c.id == work_id,
                        work.c.conversation_id == conversation_id,
                        work.c.generation == generation,
                        work.c.state.not_in(("completed", "cancelled")),
                        func.json_extract(work.c.checkpoint_json, "$.archived").is_(None),
                    )
                    .values(
                        state="queued",
                        reason="explicit_resume",
                        checkpoint_json=func.json_remove(
                            work.c.checkpoint_json, "$.accepted_control"
                        ),
                        revision=work.c.revision + 1,
                        updated=time.time(),
                    )
                    .returning(work.c.id)
                )
                if updated is None:
                    raise WorkConflict("work_resume_obsolete")
                await session.execute(
                    update(waits)
                    .where(waits.c.work_id == work_id, waits.c.status == "active")
                    .values(status="cancelled", updated=time.time())
                )
            return int(row["id"])

    async def prepare_input(
        self,
        identity: int,
        payload: dict[str, Any],
        *,
        images: tuple[ChatImage, ...] = (),
        before_publish: Callable[[AsyncSession, int], Awaitable[None]] | None = None,
    ) -> bool:
        from qq_ai_bot.runtime.work_media import externalize

        # A retry acknowledges the original durable input, never replaces it.
        async with self.database.sessions() as reader:
            if before_publish is None and await reader.scalar(
                select(inputs.c.ready).where(
                    inputs.c.id == identity,
                    inputs.c.state.in_(("pending", "staged", "consumed")),
                    inputs.c.ready.is_(True),
                )
            ):
                return True
        blobs: dict[str, bytes] = {}
        prepared = externalize({**payload, "images": [asdict(image) for image in images]}, blobs)
        serialized = encode_json(prepared)
        live_owner = (
            select(work.c.id)
            .join(
                CanonicalConversationModel,
                CanonicalConversationModel.id == work.c.conversation_id,
            )
            .where(
                work.c.id == inputs.c.work_id,
                work.c.generation == inputs.c.generation,
                work.c.conversation_id == inputs.c.conversation_id,
                work.c.state.not_in(TERMINAL),
                CanonicalConversationModel.generation == inputs.c.generation,
            )
            .exists()
        )
        async with self.database.immediate_session() as session:
            changed = (
                await session.execute(
                    update(inputs)
                    .where(
                        inputs.c.id == identity,
                        inputs.c.state == "pending",
                        inputs.c.ready.is_(False),
                        or_(inputs.c.work_id.is_(None), live_owner),
                    )
                    .values(payload_json=serialized, ready=True)
                    .returning(inputs.c.work_id)
                )
            ).first()
            if changed is None:
                # A wakeup may already carry a ready control signal, or a retry
                # may arrive after the original input was staged/consumed.
                # Acknowledge that durable input without replacing its payload.
                ready = bool(
                    await session.scalar(
                        select(inputs.c.ready).where(
                            inputs.c.id == identity,
                            inputs.c.state.in_(("pending", "staged", "consumed")),
                            inputs.c.ready.is_(True),
                        )
                    )
                )
                if ready and before_publish is not None:
                    await before_publish(session, identity)
                return ready
            work_id = changed.work_id
            if before_publish is not None:
                await before_publish(session, identity)
            if blobs:
                if work_id is None:
                    raise WorkConflict("work_input_media_owner_missing")
                await session.execute(
                    insert(media).on_conflict_do_nothing(index_elements=[media.c.sha256]),
                    [{"sha256": digest, "content": data} for digest, data in blobs.items()],
                )
                await session.execute(
                    insert(media_refs).on_conflict_do_nothing(
                        index_elements=[media_refs.c.work_id, media_refs.c.sha256]
                    ),
                    [{"work_id": work_id, "sha256": digest} for digest in blobs],
                )
            await session.execute(
                update(work)
                .where(
                    work.c.id.in_(
                        select(inputs.c.work_id).where(
                            inputs.c.id == identity, inputs.c.ready.is_(True)
                        )
                    ),
                    work.c.state.in_(("waiting_external", "waiting_user")),
                )
                .values(
                    state="queued",
                    reason="input_prepared",
                    updated=time.time(),
                    revision=work.c.revision + 1,
                )
            )
        return True

    async def input_images(self, item: dict[str, Any]) -> tuple[ChatImage, ...]:
        """Hydrate media owned by the original input, independent of its activation."""
        from qq_ai_bot.runtime.work_media import hydrate, references

        payload = json.loads(item["payload_json"])
        encoded = payload.get("images", [])
        refs = references(encoded)
        if not refs:
            return tuple(ChatImage(**image) for image in encoded)
        async with self.database.sessions() as session:
            blobs = {
                row.sha256: bytes(row.content)
                for row in await session.execute(
                    select(media)
                    .join(media_refs, media_refs.c.sha256 == media.c.sha256)
                    .where(
                        media.c.sha256.in_(refs),
                        media_refs.c.work_id == item["work_id"],
                    )
                )
            }
        try:
            return tuple(ChatImage(**image) for image in hydrate(encoded, blobs))
        except (KeyError, ValueError) as exc:
            raise WorkConflict("work_input_media_missing") from exc

    async def defer_context_rollup(
        self,
        lease: WorkLease,
        identity: str,
        version: ConversationReadVersion,
        coverage: int,
        timeout_seconds: float,
        *,
        token_budget: int | None = None,
    ) -> bool:
        """Park only pre-history work; the existing rollup job owns preparation."""
        async with self.database.immediate_session() as session:
            await self._assert_lease(session, lease)
            current = (
                (
                    await session.execute(
                        select(work).where(
                            work.c.id == identity,
                            work.c.conversation_id == lease.conversation_id,
                            work.c.generation == lease.generation,
                            work.c.state.not_in(TERMINAL),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            source = await session.get(CanonicalConversationModel, lease.conversation_id)
            if (
                current is None
                or source is None
                or (
                    source.id,
                    source.generation,
                    source.starts_after_event_id,
                    source.prompt_source_revision,
                )
                != (
                    version.conversation_id,
                    version.generation,
                    version.starts_after_event_id,
                    version.prompt_source_revision,
                )
            ):
                raise WorkConflict("work_context_source_changed")
            if current["model_requests"] or await session.scalar(
                select(journal.c.work_id).where(journal.c.work_id == identity)
            ):
                raise WorkConflict("work_journal_source_changed")
            checkpoint = json.loads(current["checkpoint_json"])
            previous = checkpoint.get("context_rollup", {})
            deadline = previous.get("deadline", time.time() + timeout_seconds)
            job = await session.get(CanonicalConversationRollupJobModel, source.id)
            if deadline <= time.time() or (
                job is not None and job.generation == source.generation and job.last_error_category
            ):
                return False  # Reuse the existing bounded extractive fallback.
            now = datetime.now(UTC)
            await session.execute(
                insert(CanonicalConversationRollupJobModel)
                .values(
                    conversation_id=source.id,
                    generation=source.generation,
                    signal_revision=1,
                    status="pending",
                    failure_count=0,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["conversation_id"],
                    where=CanonicalConversationRollupJobModel.generation != source.generation,
                    set_={
                        "generation": source.generation,
                        "signal_revision": CanonicalConversationRollupJobModel.signal_revision + 1,
                        "status": "pending",
                        "failure_count": 0,
                        "lease_owner": None,
                        "lease_token": None,
                        "lease_until": None,
                        "next_attempt_at": now,
                        "last_error_category": None,
                        "updated_at": now,
                    },
                )
            )
            checkpoint["context_rollup"] = {
                "coverage": coverage,
                "deadline": deadline,
                "starts_after": source.starts_after_event_id,
                "token_budget": token_budget,
            }
            await session.execute(
                update(work)
                .where(work.c.id == identity)
                .values(checkpoint_json=encode_json(checkpoint))
            )
        return True

    async def finish_context_rollup(self, lease: WorkLease, identity: str) -> None:
        async with self.database.immediate_session() as session:
            await self._assert_lease(session, lease)
            await session.execute(
                update(work)
                .where(
                    work.c.id == identity,
                    work.c.conversation_id == lease.conversation_id,
                    work.c.generation == lease.generation,
                    work.c.state.not_in(TERMINAL),
                )
                .values(
                    checkpoint_json=func.json_remove(work.c.checkpoint_json, "$.context_rollup")
                )
            )

    async def wake_context_rollups(self) -> None:
        """Discover one bounded completion page; empty polls never take a writer."""
        semantic = CanonicalConversationRollupModel
        overlay = CanonicalConversationRollupEmergencyOverlayModel
        job = CanonicalConversationRollupJobModel
        source = CanonicalConversationModel
        coverage = func.json_extract(work.c.checkpoint_json, "$.context_rollup.coverage")
        deadline = func.json_extract(work.c.checkpoint_json, "$.context_rollup.deadline")
        eligible = (
            select(work.c.id)
            .join(source, source.id == work.c.conversation_id)
            .outerjoin(semantic, semantic.conversation_id == source.id)
            .outerjoin(overlay, overlay.conversation_id == source.id)
            .outerjoin(job, job.conversation_id == source.id)
            .where(
                work.c.state == "waiting_external",
                work.c.generation == source.generation,
                coverage.is_not(None),
                func.json_extract(work.c.checkpoint_json, "$.context_rollup.starts_after")
                == source.starts_after_event_id,
                or_(
                    (semantic.generation == source.generation)
                    & (semantic.covered_through_event_id > coverage),
                    (overlay.generation == source.generation)
                    & (overlay.covered_through_event_id > coverage),
                    job.conversation_id.is_(None),
                    job.last_error_category.is_not(None),
                    deadline <= time.time(),
                ),
            )
            .order_by(work.c.updated)
            .limit(32)
        )
        async with self.database.sessions() as reader:
            identities = tuple(await reader.scalars(eligible))
        if not identities:
            return
        async with self.database.immediate_session() as writer:
            await writer.execute(
                update(work)
                .where(work.c.id.in_(identities), work.c.id.in_(eligible))
                .values(
                    state="queued",
                    reason="context_prepared",
                    revision=work.c.revision + 1,
                    updated=time.time(),
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
                checkpoint_json=func.json_remove(work.c.checkpoint_json, "$.accepted_control"),
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
        from qq_ai_bot.conversation.observation_models import (
            ContextObservationModel,
            ContextSelectionModel,
        )
        from qq_ai_bot.runtime.protocol_schema import refs as protocol_refs
        from qq_ai_bot.tool_results.schema import artifact_refs

        identities = select(work.c.id).where(work.c.conversation_id == conversation_id)
        observation_ids = select(ContextObservationModel.id).where(
            ContextObservationModel.conversation_id == conversation_id
        )
        await session.execute(
            delete(artifact_refs).where(
                (
                    artifact_refs.c.owner_kind.in_(("observation", "summary"))
                    & artifact_refs.c.owner_id.in_(observation_ids)
                )
                | (
                    (artifact_refs.c.owner_kind == "work_note")
                    & artifact_refs.c.owner_id.in_(identities)
                )
            )
        )
        await session.execute(
            delete(ContextObservationModel).where(
                ContextObservationModel.conversation_id == conversation_id
            )
        )
        await session.execute(
            delete(ContextSelectionModel).where(
                ContextSelectionModel.conversation_id == conversation_id
            )
        )
        await session.execute(delete(protocol_refs).where(protocol_refs.c.work_id.in_(identities)))
        from qq_ai_bot.persistence.models import ToolArtifactModel

        await session.execute(
            update(ToolArtifactModel)
            .where(ToolArtifactModel.work_id.in_(identities))
            .values(deleting=True)
        )
        await session.execute(delete(journal).where(journal.c.work_id.in_(identities)))
        await session.execute(delete(waits).where(waits.c.work_id.in_(identities)))
        await session.execute(delete(effects).where(effects.c.work_id.in_(identities)))
        await session.execute(delete(inputs).where(inputs.c.conversation_id == conversation_id))
        await session.execute(delete(children).where(children.c.work_id.in_(identities)))
        # Owner tables that reference runtime_work with RESTRICT are retired in
        # this same privacy transaction first: a live Job is cancelled (never
        # left pending with a null link that would look like a new Job), then
        # dereferenced, so the Work rows can be removed.
        owner_jobs = _OWNER_JOBS
        await session.execute(
            update(owner_jobs)
            .where(
                owner_jobs.c.work_id.in_(identities),
                owner_jobs.c.status.in_(("pending", "processing")),
            )
            .values(status="cancelled", last_error_category="privacy_purged", lease_until=None)
        )
        await session.execute(
            update(owner_jobs).where(owner_jobs.c.work_id.in_(identities)).values(work_id=None)
        )
        await session.execute(
            update(work)
            .where(work.c.conversation_id == conversation_id)
            .values(parent_work_id=None)
        )
        await session.execute(delete(work).where(work.c.conversation_id == conversation_id))
        await session.execute(
            delete(media).where(media.c.sha256.not_in(select(media_refs.c.sha256)))
        )
        await session.execute(delete(scope).where(scope.c.conversation_id == conversation_id))

    async def confirm_child_completion(self, request_id: str) -> bool:
        """Confirm the original model's receipt and retire only its duplicate wakeup."""
        from qq_ai_bot.sandbox.db_models import SandboxTaskContinuationModel, SandboxTaskRunModel

        # Empty/repeated confirmations need no writer. This indexed candidate
        # read only avoids work; the transaction below rechecks every authority.
        matching_input = (
            select(inputs.c.id)
            .where(
                inputs.c.source_key == f"completion:{request_id}",
                inputs.c.kind == "completion",
                inputs.c.state == "pending",
            )
            .exists()
        )
        async with self.database.sessions() as reader:
            candidate = await reader.scalar(
                select(SandboxTaskRunModel.request_id)
                .join(
                    SandboxTaskContinuationModel,
                    SandboxTaskContinuationModel.request_id == SandboxTaskRunModel.request_id,
                )
                .where(
                    SandboxTaskRunModel.request_id == request_id,
                    SandboxTaskRunModel.status == "completed",
                    SandboxTaskRunModel.run_id.is_not(None),
                    or_(
                        SandboxTaskContinuationModel.state == "ready",
                        and_(
                            SandboxTaskContinuationModel.state == "observed",
                            or_(
                                SandboxTaskContinuationModel.reason == "forwarded_to_parent_work",
                                and_(
                                    SandboxTaskContinuationModel.reason == "original_turn_observed",
                                    matching_input,
                                ),
                            ),
                        ),
                    ),
                )
            )
        if candidate is None:
            return False
        async with self.database.immediate_session() as session:
            task = await session.get(SandboxTaskRunModel, request_id)
            receipt = await session.get(SandboxTaskContinuationModel, request_id)
            if (
                task is None
                or receipt is None
                or task.status != "completed"
                or not task.run_id
                or receipt.state not in {"ready", "observed"}
                or (
                    receipt.state == "observed"
                    and receipt.reason not in {"forwarded_to_parent_work", "original_turn_observed"}
                )
            ):
                return False
            source = json.loads(task.source_json)
            completion = json.loads(task.completion_json or "{}")
            if (
                not isinstance(source, dict)
                or not isinstance(completion, dict)
                or completion.get("run_id") != task.run_id
                or completion.get("pending") is not False
                or completion.get("status") not in {"succeeded", "failed", "cancelled"}
            ):
                return False
            parent_id = source.get("work_id")
            parent = None
            if parent_id:
                parent = (
                    (await session.execute(select(work).where(work.c.id == parent_id)))
                    .mappings()
                    .first()
                )
                if (
                    parent is None
                    or source.get("conversation_id") != task.source_conversation_id
                    or task.source_conversation_id != parent["conversation_id"]
                    or type(source.get("generation")) is not int
                    or source["generation"] != parent["generation"]
                ):
                    return False
            changed = receipt.state != "observed" or receipt.reason != "original_turn_observed"
            if changed:
                receipt.state, receipt.reason = "observed", "original_turn_observed"
                receipt.updated_at = datetime.now(UTC)
            if parent is not None:
                # A fast command may have been forwarded before the original
                # response consumed its tool result. Keep unseen/staged inputs
                # and every other source; only this confirmed wakeup is redundant.
                cancelled = await session.execute(
                    update(inputs)
                    .where(
                        inputs.c.source_key == f"completion:{request_id}",
                        inputs.c.work_id == parent["id"],
                        inputs.c.conversation_id == parent["conversation_id"],
                        inputs.c.generation == parent["generation"],
                        inputs.c.kind == "completion",
                        inputs.c.state == "pending",
                    )
                    .values(state="cancelled")
                    .returning(inputs.c.id)
                )
                changed = cancelled.scalar_one_or_none() is not None or changed
            return changed

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
                    payload_json=encode_json({"text": json.dumps(payload, ensure_ascii=False)}),
                    created=time.time(),
                )
                .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
            )
            await session.execute(
                update(work)
                .where(
                    work.c.id == parent["id"],
                    work.c.state == "waiting_external",
                    ~select(waits.c.id)
                    .where(waits.c.work_id == work.c.id, waits.c.status == "active")
                    .exists(),
                )
                .values(state="queued", revision=work.c.revision + 1, updated=time.time())
            )
            receipt.state, receipt.reason = "observed", "forwarded_to_parent_work"
            receipt.updated_at = datetime.now(UTC)

    async def completed_children(
        self,
        lease: WorkLease,
        work_id: str,
        run_ids: list[str],
        *,
        request_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Read only terminal host receipts belonging to this fenced parent."""
        from qq_ai_bot.runtime.effect_outcomes import execution_finished
        from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel

        request_ids = request_ids or []
        if not run_ids and not request_ids:
            return []
        identities = []
        if run_ids:
            identities.append(SandboxTaskRunModel.run_id.in_(run_ids[:32]))
        if request_ids:
            identities.append(SandboxTaskRunModel.request_id.in_(request_ids[:32]))
        source_fields = ("work_id", "conversation_id", "generation")
        completion_fields = (
            "run_id",
            "status",
            "pending",
            "uncertain",
            "exit_code",
            "error",
            "artifact_id",
            "artifacts",
        )
        async with self.database.sessions() as session:
            if (
                await session.scalar(
                    select(self._lease_table(lease).c.fence).where(self._fence(lease))
                )
                is None
            ):
                raise WorkConflict("work_activation_obsolete")
            rows = await session.execute(
                select(
                    SandboxTaskRunModel.request_id,
                    SandboxTaskRunModel.run_id,
                    func.json_extract(
                        SandboxTaskRunModel.source_json,
                        *(f"$.{field}" for field in source_fields),
                    ).label("source"),
                    func.json_extract(
                        SandboxTaskRunModel.completion_json,
                        *(f"$.{field}" for field in completion_fields),
                    ).label("completion"),
                )
                .where(
                    SandboxTaskRunModel.source_conversation_id == lease.conversation_id,
                    or_(*identities),
                    SandboxTaskRunModel.completion_json.is_not(None),
                )
                .order_by(SandboxTaskRunModel.request_id)
                .limit(64)
            )
            results = []
            for row in rows:
                source = dict(zip(source_fields, json.loads(row.source), strict=True))
                if (
                    source.get("work_id") != work_id
                    or source.get("conversation_id") != lease.conversation_id
                    or type(source.get("generation")) is not int
                    or source["generation"] != lease.generation
                ):
                    continue
                value = {
                    key: value
                    for key, value in zip(
                        completion_fields, json.loads(row.completion), strict=True
                    )
                    if value is not None
                }
                if (
                    not isinstance(row.run_id, str)
                    or value.get("run_id") != row.run_id
                    or not execution_finished(value)
                ):
                    continue
                results.append(
                    {
                        "run_id": row.run_id,
                        "request_id": row.request_id,
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

        async with self.database.sessions() as discovery:
            candidate_ids = tuple(
                await discovery.scalars(
                    select(inputs.c.id)
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
        if not candidate_ids:
            return
        async with self.database.immediate_session() as session:
            rows = (
                (
                    await session.execute(
                        select(inputs)
                        .where(
                            inputs.c.state == "pending",
                            inputs.c.id.in_(candidate_ids),
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
                            payload_json=encode_json({"text": content}),
                        )
                    )
                    if row["work_id"]:
                        await session.execute(
                            update(work)
                            .where(
                                work.c.id == row["work_id"],
                                work.c.state.in_(("waiting_external", "waiting_user")),
                            )
                            .values(
                                state="queued",
                                reason="input_repaired",
                                revision=work.c.revision + 1,
                                updated=time.time(),
                            )
                        )

    async def reclaim_terminal(self) -> None:
        """Keep the latest 128 terminal work receipts; never evict an active work."""
        from qq_ai_bot.runtime.subagent_schema import media, media_refs

        def terminal_query() -> Any:
            return (
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
                    # A live owner Job closes from its Work first; archive later.
                    work.c.id.not_in(
                        select(_OWNER_JOBS.c.work_id).where(
                            _OWNER_JOBS.c.work_id.is_not(None),
                            _OWNER_JOBS.c.status.in_(("pending", "processing")),
                        )
                    ),
                )
                .order_by(work.c.updated.desc())
                .offset(128)
                .limit(256)
            )

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
            terminal_candidate = await session.scalar(terminal_query().limit(1))
        if not orphaned and terminal_candidate is None:
            return
        async with self.database.immediate_session() as session:
            if orphaned:
                await session.execute(
                    delete(media).where(
                        media.c.sha256.in_(orphaned),
                        media.c.sha256.not_in(select(media_refs.c.sha256)),
                    )
                )
            selected = list(await session.scalars(terminal_query()))
            if not selected:
                return
            from qq_ai_bot.runtime.protocol_schema import refs as protocol_refs

            await session.execute(
                delete(protocol_refs).where(protocol_refs.c.work_id.in_(selected))
            )
            await session.execute(delete(journal).where(journal.c.work_id.in_(selected)))
            await session.execute(delete(waits).where(waits.c.work_id.in_(selected)))
            await session.execute(delete(inputs).where(inputs.c.work_id.in_(selected)))
            await session.execute(delete(effects).where(effects.c.work_id.in_(selected)))
            await session.execute(delete(media_refs).where(media_refs.c.work_id.in_(selected)))
            # Stable invocation identities outlive their detailed result. Keep a
            # small tombstone so replaying an old callback cannot execute anew.
            await session.execute(
                update(work)
                .where(
                    work.c.id.in_(selected),
                    or_(
                        func.json_extract(work.c.source_json, "$.delivery_contract")
                        == "return_to_caller",
                        work.c.id.in_(
                            select(work.c.parent_work_id).where(work.c.parent_work_id.is_not(None))
                        ),
                    ),
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
                    # A referenced Work keeps its row; never CASCADE/SET NULL a Job.
                    work.c.id.not_in(
                        select(_OWNER_JOBS.c.work_id).where(_OWNER_JOBS.c.work_id.is_not(None))
                    ),
                )
            )

    async def discard_input(self, identity: int) -> None:
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(inputs)
                .where(inputs.c.id == identity, inputs.c.state == "pending")
                .values(state="cancelled", payload_json="{}")
            )

    async def prepare_effect(
        self,
        lease: WorkLease,
        identity: str,
        key: str,
        kind: str,
        *,
        outcome: dict[str, Any] | None = None,
        invocation: dict[str, Any] | None = None,
        composition: dict[str, Any] | None = None,
    ) -> bool:
        """False means an intent already exists, not that it is safe to send again."""
        now = time.time()
        receipt = encode_json(
            {
                **({"outcome": outcome} if outcome is not None else {}),
                **({"invocation": invocation} if invocation is not None else {}),
                **({"composition": composition} if composition is not None else {}),
            }
        )
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
                        receipt_json=receipt,
                        created=now,
                        updated=now,
                    )
                    .on_conflict_do_nothing(index_elements=[effects.c.effect_key])
                    .returning(effects.c.effect_key)
                )
            ).first()
            return inserted is not None

    async def publish_code_boundary(
        self,
        lease: WorkLease,
        identity: str,
        parent_key: str,
        *,
        expected_revision: int,
        composition: dict[str, Any],
        child: dict[str, Any] | None,
        store: ProtocolStore,
        binding: CodeSnapshotBinding,
        side_effecting: bool,
    ) -> bool:
        """T1 publishes a private prepared snapshot and at most one original child intent.

        Without a child this is a pure program checkpoint (e.g. a future wait):
        the same CAS and publication, and no business intent.
        """
        if (
            binding.work_id != identity
            or binding.operation_id != parent_key
            or binding.conversation_id != lease.conversation_id
            or binding.generation != lease.generation
        ):
            raise WorkConflict("code_boundary_identity_conflict")
        if child is not None and (
            child.get("version") != 1
            or child.get("parent_effect_key") != parent_key
            or child.get("owner_execution_id") != identity
            or child.get("dispatch_started") is not False
            or child.get("budget_admitted") is not False
            or not isinstance(child.get("child_ordinal"), int)
            or not isinstance(child.get("feed_index"), int)
            or not isinstance(child.get("engine_call_id"), str)
        ):
            raise WorkConflict("code_boundary_identity_conflict")
        async with self.database.sessions() as reader:
            original = (
                (
                    await reader.execute(
                        select(effects).where(
                            effects.c.effect_key == parent_key,
                            effects.c.work_id == identity,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if (
            original is None
            or original["kind"] != "code_composition"
            or original["state"] not in {"prepared", "unknown"}
        ):
            raise WorkConflict("code_composition_closed")
        previous = json.loads(original["receipt_json"])
        saved = previous.get("composition", {})
        if saved.get("version") != 1 or saved.get("snapshot_revision") != expected_revision:
            raise WorkConflict("code_checkpoint_conflict")
        for field in (
            "script_id",
            "media_privacy_generation",
            "code_ref",
            "inputs_ref",
            "api_revision",
            "engine_digest",
            "dump_format",
        ):
            if field in composition and composition[field] != saved.get(field):
                raise WorkConflict("code_composition_binding_conflict")
        if (
            saved.get("api_revision") != binding.api_revision
            or saved.get("engine_digest") != binding.engine_digest
            or saved.get("dump_format") != binding.dump_format
        ):
            raise WorkConflict("code_composition_binding_conflict")
        next_composition = {
            **saved,
            **composition,
            "version": 1,
            "snapshot_revision": expected_revision + 1,
        }
        snapshot_ref = next_composition.get("snapshot_ref")
        if snapshot_ref not in store.prepared_refs:
            raise WorkConflict("code_snapshot_not_prepared")
        store.decode_code_snapshot(await store.get_bytes(snapshot_ref), binding)
        output_ref = next_composition.get("output_ref")
        if output_ref is not None:
            if output_ref not in store.prepared_refs:
                raise WorkConflict("code_output_not_prepared")
            store.decode_code_snapshot(await store.get_bytes(output_ref), binding)
        parent_receipt = encode_json({**previous, "composition": next_composition})
        child_receipt = (
            encode_json(
                {
                    "invocation": child,
                    "outcome": {
                        "tool": child["tool_id"],
                        "side_effecting": side_effecting,
                        "ok": False,
                        "pending": False,
                        "uncertain": False,
                        "executed": False,
                    },
                }
            )
            if child is not None
            else None
        )
        from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel

        async with store.publication(identity) as prepared_objects:
            async with self.database.immediate_session() as writer:
                await self._assert_lease(writer, lease)
                source = await writer.get(CanonicalConversationModel, lease.conversation_id)
                privacy = (
                    await writer.scalar(
                        select(ExecutionTraceStateModel.privacy_generation).where(
                            ExecutionTraceStateModel.id == 1,
                        )
                    )
                    or 0
                )
                if (
                    source is None
                    or source.generation != binding.generation
                    or source.prompt_source_revision != binding.source_revision
                    or privacy != binding.privacy_generation
                ):
                    raise WorkConflict("code_boundary_authority_changed")
                if not await writer.scalar(
                    select(work.c.id).where(
                        work.c.id == identity,
                        work.c.conversation_id == lease.conversation_id,
                        work.c.generation == lease.generation,
                        work.c.state.not_in(TERMINAL),
                    )
                ):
                    raise WorkConflict("work_effect_obsolete")
                changed = await writer.scalar(
                    update(effects)
                    .where(
                        effects.c.effect_key == parent_key,
                        effects.c.work_id == identity,
                        effects.c.state.in_(("prepared", "unknown")),
                        effects.c.receipt_json == original["receipt_json"],
                    )
                    .values(receipt_json=parent_receipt, updated=time.time())
                    .returning(effects.c.effect_key)
                )
                if changed is None:
                    raise WorkConflict("code_checkpoint_conflict")
                try:
                    if child is not None:
                        await writer.execute(
                            insert(effects).values(
                                effect_key=child["operation_id"],
                                work_id=identity,
                                kind="tool",
                                state="prepared",
                                receipt_json=child_receipt,
                                created=time.time(),
                                updated=time.time(),
                            )
                        )
                except IntegrityError as exc:
                    # Same ordinal/engine call or operation ID: one original child only.
                    raise WorkConflict("code_child_identity_conflict") from exc
                await store.publish_refs(writer, identity, prepared_objects)
        return True

    async def composition_children(self, identity: str, parent_key: str) -> list[dict[str, Any]]:
        """Exact parent index lookup; never a bounded display page or a text scan."""
        async with self.database.sessions() as reader:
            rows = (
                (
                    await reader.execute(
                        select(effects.c.effect_key, effects.c.state, effects.c.receipt_json)
                        .where(
                            effects.c.work_id == identity,
                            func.json_extract(
                                effects.c.receipt_json, "$.invocation.parent_effect_key"
                            )
                            == parent_key,
                            func.json_extract(effects.c.receipt_json, "$.invocation.version") == 1,
                        )
                        .order_by(effects.c.effect_key)
                    )
                )
                .mappings()
                .all()
            )
        return [
            {
                "effect_key": row["effect_key"],
                "state": row["state"],
                **json.loads(row["receipt_json"]),
            }
            for row in rows
        ]

    async def undispatched_intent(self, identity: str, key: str) -> bool:
        async with self.database.sessions() as reader:
            row = (
                (
                    await reader.execute(
                        select(effects.c.state, effects.c.receipt_json).where(
                            effects.c.effect_key == key, effects.c.work_id == identity
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None or row["state"] != "prepared":
            return False
        metadata = json.loads(row["receipt_json"]).get("invocation")
        return (
            isinstance(metadata, dict)
            and metadata.get("version") == 1
            and metadata.get("dispatch_started") is False
        )

    async def validate_invocation(self, key: str, expected: dict[str, Any]) -> None:
        """The fingerprint detects changed content, never determines an effect ID."""
        async with self.database.sessions() as reader:
            previous_json = await reader.scalar(
                select(effects.c.receipt_json).where(effects.c.effect_key == key)
            )
        if previous_json is None:
            return
        previous = json.loads(previous_json).get("invocation")
        if previous is None:
            return  # Historical receipts are not assigned fabricated metadata.
        stable = {
            k: v
            for k, v in expected.items()
            if k not in {"dispatch_started", "budget_admitted", "revision", "original_domain_ref"}
        }
        if any(previous.get(k) != v for k, v in stable.items()):
            raise WorkConflict("invocation_content_conflict")

    async def admit_dispatch(
        self, lease: WorkLease, identity: str, key: str, *, charge: bool = True
    ) -> bool:
        """T2: one dispatch marker and all root/run usage commit together.

        ``charge=False`` is for lifecycle controls and local artifact readback.
        They still publish the same dispatch marker and original receipt.
        """
        async with self.database.sessions() as reader:
            original = (
                (
                    await reader.execute(
                        select(effects).where(
                            effects.c.effect_key == key, effects.c.work_id == identity
                        )
                    )
                )
                .mappings()
                .first()
            )
        if original is None:
            raise WorkConflict("invocation_intent_missing")
        receipt = json.loads(original["receipt_json"])
        invocation = receipt.get("invocation")
        if not isinstance(invocation, dict) or invocation.get("version") != 1:
            raise WorkConflict("invocation_metadata_missing")
        if invocation.get("dispatch_started") or original["state"] != "prepared":
            return False
        receipt["invocation"] = {
            **invocation,
            "dispatch_started": True,
            "budget_admitted": charge,
            "revision": invocation["revision"] + 1,
        }
        serialized = encode_json(receipt)
        from qq_ai_bot.runtime.work_budget import charge as charge_budget

        async with self.database.immediate_session() as writer:
            await self._assert_lease(writer, lease)
            changed = await writer.scalar(
                update(effects)
                .where(
                    effects.c.effect_key == key,
                    effects.c.work_id == identity,
                    effects.c.state == "prepared",
                    effects.c.receipt_json == original["receipt_json"],
                )
                .values(receipt_json=serialized, updated=time.time())
                .returning(effects.c.effect_key)
            )
            if changed is None:
                return False
            if not charge:
                return True
            await charge_budget(writer, identity, models=0, tools=1)
            updated = await writer.scalar(
                update(work)
                .where(
                    work.c.id == identity,
                    work.c.conversation_id == lease.conversation_id,
                    work.c.generation == lease.generation,
                    work.c.state.not_in(TERMINAL),
                )
                .values(tool_calls=work.c.tool_calls + 1, updated=time.time())
                .returning(work.c.id)
            )
            if updated is None:
                raise WorkConflict("work_effect_obsolete")
        return True

    @staticmethod
    async def bind_domain_receipt(
        writer: AsyncSession, identity: str, key: str, reference: str
    ) -> None:
        """Bind the original domain intent before dispatch, in its prepare transaction."""
        if not reference or len(reference) > 256:
            raise WorkConflict("effect_domain_reference_invalid")
        prior = func.json_extract(effects.c.receipt_json, "$.invocation.original_domain_ref")
        changed = await writer.scalar(
            update(effects)
            .where(
                effects.c.effect_key == key,
                effects.c.work_id == identity,
                effects.c.work_id.in_(
                    select(work.c.id).where(
                        work.c.state.not_in(TERMINAL),
                        work.c.generation
                        == select(CanonicalConversationModel.generation)
                        .where(CanonicalConversationModel.id == work.c.conversation_id)
                        .scalar_subquery(),
                    )
                ),
                func.json_extract(effects.c.receipt_json, "$.invocation.version") == 1,
                func.json_extract(effects.c.receipt_json, "$.invocation.dispatch_started") == 1,
                or_(prior.is_(None), prior == reference),
            )
            .values(
                receipt_json=func.json_set(
                    effects.c.receipt_json,
                    "$.invocation.original_domain_ref",
                    reference,
                    "$.invocation.revision",
                    func.json_extract(effects.c.receipt_json, "$.invocation.revision") + 1,
                ),
                updated=time.time(),
            )
            .returning(effects.c.effect_key)
        )
        if changed is None:
            raise WorkConflict("effect_domain_reference_conflict")

    @staticmethod
    def _effect_scope(identity: str) -> Any:
        from qq_ai_bot.runtime.work_tree import descendants

        return effects.c.work_id.in_(descendants(identity, include_self=True))

    @staticmethod
    def _lifecycle_role_clause() -> Any:
        # Only an explicit JSON boolean false proves an observation. Legacy
        # missing/null roles remain conservative, rather than inventing safety.
        return (
            func.coalesce(
                func.json_type(effects.c.receipt_json, "$.outcome.side_effecting"), "missing"
            )
            != "false"
        )

    @staticmethod
    def _known_native_final_clause() -> Any:
        receipt = effects.c.receipt_json

        def field_type(name: str) -> Any:
            return func.coalesce(func.json_type(receipt, "$." + name), "missing")

        domain = and_(
            effects.c.kind == "final",
            field_type("outcome") == "missing",
            field_type("result") == "missing",
            field_type("status") == "missing",
            field_type("ok") == "missing",
        )
        accepted = and_(
            effects.c.state == "accepted",
            field_type("transport_accepted") == "true",
            field_type("error") == "missing",
            field_type("pending").in_(("missing", "false")),
            field_type("uncertain").in_(("missing", "false")),
            field_type("executed").in_(("missing", "true")),
            field_type("mutation_committed").in_(("missing", "true")),
        )
        refused = and_(
            effects.c.state == "failed",
            field_type("error") == "text",
            func.coalesce(func.json_extract(receipt, "$.error"), "") == "delivery_not_dispatched",
            field_type("executed") == "false",
            field_type("mutation_committed") == "false",
            field_type("transport_accepted") == "missing",
            field_type("pending").in_(("missing", "false")),
            field_type("uncertain").in_(("missing", "false")),
        )
        return and_(domain, or_(accepted, refused))

    @staticmethod
    def _unknown_historical_outcome_clause() -> Any:
        receipt = effects.c.receipt_json
        kind = func.coalesce(func.json_type(receipt, "$.outcome"), "missing")
        value = func.json_extract(receipt, "$.outcome")
        absent = or_(kind.in_(("missing", "null")), value == "{}")
        malformed: list[Any] = [~kind.in_(("object", "missing", "null"))]
        for field in (
            "ok",
            "pending",
            "uncertain",
            "side_effecting",
            "executed",
            "retryable",
            "mutation_committed",
        ):
            allowed = (
                ("missing", "true", "false", "null")
                if field == "mutation_committed"
                else ("missing", "true", "false")
            )
            malformed.append(
                ~func.coalesce(func.json_type(receipt, "$.outcome." + field), "missing").in_(
                    allowed
                )
            )
        malformed.append(
            ~func.coalesce(func.json_type(receipt, "$.outcome.status"), "missing").in_(
                ("missing", "null", "text")
            )
        )
        meaningful = or_(
            func.coalesce(func.json_type(receipt, "$.outcome.ok"), "missing").in_(
                ("true", "false")
            ),
            func.coalesce(func.json_type(receipt, "$.outcome.pending"), "missing") == "true",
            func.coalesce(func.json_type(receipt, "$.outcome.uncertain"), "missing") == "true",
            func.coalesce(func.json_type(receipt, "$.outcome.side_effecting"), "missing")
            == "false",
        )
        malformed.append(and_(~absent, ~meaningful))
        # Migration 0101 gave every legacy receipt a canonical outcome; one that
        # is still absent (other than an exact final transport receipt) is unknown.
        return and_(
            ~WorkRepository._known_native_final_clause(),
            or_(*malformed, absent),
        )

    @staticmethod
    def _unresolved_clause(*, pending: bool = True, uncertain: bool = True) -> Any:
        clauses = []
        if pending:
            clauses.append(
                or_(
                    func.json_extract(effects.c.receipt_json, "$.outcome.pending") == 1,
                    func.json_extract(effects.c.receipt_json, "$.outcome.status").in_(
                        ("running", "queued", "waiting")
                    ),
                )
            )
        if uncertain:
            clauses.extend(
                (
                    func.json_extract(effects.c.receipt_json, "$.outcome.uncertain") == 1,
                    func.json_extract(effects.c.receipt_json, "$.outcome.status").in_(
                        ("unknown", "uncertain")
                    ),
                    and_(
                        effects.c.state.in_(("prepared", "unknown")),
                    ),
                )
            )
        malformed = WorkRepository._unknown_historical_outcome_clause()
        if uncertain:
            clauses.append(malformed)
        unresolved = or_(*clauses) if clauses else false()
        # A versioned intent whose T2 never committed was never dispatched: it is
        # not an unknown effect. Legacy prepared rows keep the conservative fence.
        never_dispatched = and_(
            effects.c.state == "prepared",
            # NULL-safe: a legacy row without metadata must stay inside the fence.
            func.coalesce(func.json_extract(effects.c.receipt_json, "$.invocation.version"), 0)
            == 1,
            func.coalesce(
                func.json_extract(effects.c.receipt_json, "$.invocation.dispatch_started"), 1
            )
            == 0,
        )
        unresolved = and_(unresolved, ~never_dispatched)
        # Only a recognized parent is aggregate state, never a business leaf.
        # Legacy/unknown versions retain the conservative historical fence.
        return and_(
            or_(WorkRepository._lifecycle_role_clause(), malformed),
            unresolved,
            or_(
                effects.c.kind != "code_composition",
                func.coalesce(func.json_extract(effects.c.receipt_json, "$.composition.version"), 0)
                != 1,
            ),
        )

    async def effect_evidence(
        self,
        lease: WorkLease,
        identity: str,
        *,
        only_unresolved: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Exact durable facts; callers must not use a presentation page as completeness."""
        result: list[dict[str, Any]] = []
        cursor = ""
        async with self.database.sessions() as session:
            if not await session.scalar(
                select(self._lease_table(lease).c.fence).where(self._fence(lease))
            ):
                raise WorkConflict("work_activation_obsolete")
            while True:
                query = select(
                    effects.c.effect_key,
                    effects.c.work_id,
                    effects.c.state,
                    effects.c.kind,
                    effects.c.receipt_json,
                ).where(self._effect_scope(identity))
                if only_unresolved:
                    query = query.where(self._unresolved_clause())
                if limit is not None:
                    query = query.order_by(
                        effects.c.updated.desc(), effects.c.effect_key.desc()
                    ).limit(limit)
                else:
                    query = (
                        query.where(effects.c.effect_key > cursor)
                        .order_by(effects.c.effect_key)
                        .limit(128)
                    )
                rows = (await session.execute(query)).mappings().all()
                if not rows:
                    break
                for row in rows:
                    from qq_ai_bot.runtime.effect_outcomes import historical_evidence

                    outcome = historical_evidence(
                        json.loads(row["receipt_json"]), state=row["state"], kind=row["kind"]
                    )
                    outcome.update(effect_key=row["effect_key"], work_id=row["work_id"])
                    result.append(outcome)
                cursor = rows[-1]["effect_key"]
                if limit is not None:
                    break
        return result

    async def resolve_run_effects(
        self,
        lease: WorkLease,
        identity: str,
        run_id: str,
        outcome: dict[str, Any],
        *,
        effect_key: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """Use an owned run receipt; preserve every original invocation and its mutating role."""
        from qq_ai_bot.runtime.effect_outcomes import execution_finished
        from qq_ai_bot.sandbox.environment_tools import EXECUTION_TOOLS

        if not execution_finished(outcome) or outcome.get("run_id") != run_id:
            return
        if effect_key is not None and (
            request_id is None or outcome.get("request_id") != request_id
        ):
            return
        if lease.work_id is not None and identity != lease.work_id:
            raise WorkConflict("work_effect_obsolete")
        owned = and_(
            work.c.id == identity,
            work.c.conversation_id == lease.conversation_id,
            work.c.generation == lease.generation,
        )
        # A terminal child's late receipt is still an issued execution fact.
        # Resolving it neither revives that Work nor grants another dispatch.
        target = (
            and_(effects.c.effect_key == effect_key, effects.c.state == "accepted")
            if effect_key is not None
            else func.json_extract(effects.c.receipt_json, literal_column("'$.outcome.run_id'"))
            == run_id
        )
        prepared: list[tuple[str, str, str]] = []
        cursor = ""
        async with self.database.sessions() as reader:
            await reader.execute(text("BEGIN"))
            await self._assert_lease_readonly(reader, lease)
            if await reader.scalar(select(work.c.id).where(owned)) is None:
                raise WorkConflict("work_effect_obsolete")
            while True:
                rows = (
                    (
                        await reader.execute(
                            select(effects.c.effect_key, effects.c.receipt_json)
                            .where(
                                effects.c.work_id == identity,
                                target,
                                self._lifecycle_role_clause(),
                                effects.c.effect_key > cursor,
                            )
                            .order_by(effects.c.effect_key)
                            .limit(128)
                        )
                    )
                    .mappings()
                    .all()
                )
                if not rows:
                    break
                for row in rows:
                    receipt = json.loads(row["receipt_json"])
                    previous = receipt.get("outcome", {})
                    if effect_key is not None and (
                        previous.get("request_id") != request_id
                        or previous.get("run_id") not in (None, run_id)
                        or previous.get("tool") not in EXECUTION_TOOLS
                    ):
                        continue
                    if previous.get("tool") not in (
                        EXECUTION_TOOLS | {"terminal_write", "terminal_control", "cancel_code_run"}
                    ):
                        continue
                    merged = {**previous, **outcome}
                    merged["side_effecting"] = True
                    merged["tool"] = previous.get("tool", outcome.get("tool"))
                    # A readonly poll's False describes the poll, never whether
                    # the original execution committed. Preserve unknown too.
                    merged["mutation_committed"] = previous.get("mutation_committed")
                    if merged == previous:
                        continue
                    receipt["outcome"] = merged
                    encoded = encode_json(receipt)
                    if encoded != row["receipt_json"]:
                        prepared.append((row["effect_key"], row["receipt_json"], encoded))
                cursor = rows[-1]["effect_key"]
        if not prepared:
            return
        # Receipts are independent facts; a partial page commit keeps the
        # unresolved originals without granting any dispatch authority.
        for offset in range(0, len(prepared), 128):
            async with self.database.immediate_session() as writer:
                await self._assert_lease(writer, lease)
                for key, previous_json, next_json in prepared[offset : offset + 128]:
                    await writer.execute(
                        update(effects)
                        .where(
                            effects.c.effect_key == key,
                            effects.c.work_id == identity,
                            effects.c.receipt_json == previous_json,
                            select(work.c.id).where(owned).exists(),
                        )
                        .values(receipt_json=next_json, updated=time.time())
                    )

    async def record_effect(
        self,
        key: str,
        state: str,
        receipt: dict[str, Any],
        *,
        prepared_protocol: tuple[dict[str, Any], ...] = (),
        protocol_policy: WorkStorageRuntimeConfig | None = None,
        media_source: tuple[str, int, int] | None = None,
    ) -> None:
        # A late receipt must survive cancellation. It records an already-issued
        # effect, never authorizes another one, so no current lease is required.
        if state not in {"accepted", "failed", "unknown"}:
            raise ValueError("invalid_work_effect_state")
        for _attempt in range(4):
            async with self.database.sessions() as reader:
                existing = (
                    (await reader.execute(select(effects).where(effects.c.effect_key == key)))
                    .mappings()
                    .first()
                )
            if existing is None:
                raise WorkConflict("work_effect_receipt_conflict")
            previous = json.loads(existing["receipt_json"])
            candidate = dict(receipt)
            # The ordinary outcome path cannot replace Host-owned identity or
            # snapshot metadata. Decode and serialize before opening the writer.
            for field in ("invocation", "composition"):
                if field in previous:
                    if field in candidate and candidate[field] != previous[field]:
                        raise WorkConflict("work_effect_metadata_conflict")
                    candidate[field] = previous[field]
            existing_outcome = previous.get("outcome", {})
            if "outcome" not in candidate:
                if "result" in candidate:
                    from qq_ai_bot.capabilities.results import normalize_legacy_result
                    from qq_ai_bot.runtime.effect_outcomes import execution_evidence

                    original = normalize_legacy_result(
                        candidate["result"],
                        provider_id="legacy",
                        tool_name=existing_outcome.get("tool", "legacy_tool"),
                    )
                    candidate["outcome"] = execution_evidence(
                        original,
                        tool=original.tool_name,
                        side_effecting=existing_outcome.get(
                            "side_effecting", original.mutation_committed is not False
                        ),
                    )
                if existing_outcome:
                    candidate["outcome"] = {
                        **existing_outcome,
                        **candidate.get("outcome", {}),
                        "uncertain": (
                            state == "unknown" and existing_outcome.get("side_effecting", True)
                        )
                        or candidate.get("outcome", {}).get("uncertain", False),
                    }
            if existing["state"] == "accepted":
                if state == "unknown":
                    return  # Bookkeeping failure cannot undo known acceptance.
                # Compare one canonical meaning: a migrated outcome and a late
                # legacy-shaped rewrite of the same receipt are not a conflict.
                # The UPDATE CAS below still compares the original stored bytes.
                from qq_ai_bot.runtime.effect_outcomes import historical_evidence

                def canonical(value: dict[str, Any]) -> dict[str, Any]:
                    outcome = historical_evidence(value, state="accepted")
                    return {
                        **{k: v for k, v in value.items() if k != "outcome"},
                        "outcome": {
                            k: outcome.get(k)
                            for k in ("ok", "pending", "uncertain", "executed", "status", "run_id")
                        },
                    }

                before, after = canonical(previous), canonical(candidate)
                if state != "accepted" or any(after.get(k) != v for k, v in before.items()):
                    raise WorkConflict("work_effect_receipt_conflict")
                if after == before:
                    return
            elif existing["state"] not in {"prepared", "unknown"}:
                if existing["state"] == state and candidate == previous:
                    return
                raise WorkConflict("work_effect_receipt_conflict")
            metadata = candidate.get("invocation")
            if isinstance(metadata, dict) and metadata.get("version") == 1:
                candidate["invocation"] = {**metadata, "revision": metadata["revision"] + 1}
            serialized = encode_json(candidate)
            async with self.database.immediate_session() as writer:
                if media_source is not None:
                    from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel

                    conversation_id, generation, privacy_generation = media_source
                    source = await writer.get(CanonicalConversationModel, conversation_id)
                    privacy = (
                        await writer.scalar(
                            select(ExecutionTraceStateModel.privacy_generation).where(
                                ExecutionTraceStateModel.id == 1
                            )
                        )
                        or 0
                    )
                    if (
                        source is None
                        or source.generation != generation
                        or privacy != privacy_generation
                    ):
                        raise WorkConflict("work_effect_media_source_changed")
                changed = await writer.scalar(
                    update(effects)
                    .where(
                        effects.c.effect_key == key,
                        effects.c.state == existing["state"],
                        effects.c.receipt_json == existing["receipt_json"],
                    )
                    .values(state=state, receipt_json=serialized, updated=time.time())
                    .returning(effects.c.effect_key)
                )
                if changed is not None and prepared_protocol:
                    from qq_ai_bot.runtime.protocol_store import ProtocolStore

                    await ProtocolStore(self.database, policy=protocol_policy).publish_refs(
                        writer, existing["work_id"], prepared_protocol
                    )
            if changed is not None:
                return
        raise WorkConflict("work_effect_receipt_conflict")
