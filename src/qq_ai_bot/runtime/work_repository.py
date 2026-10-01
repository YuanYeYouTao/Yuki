"""Durable work state; all mutations are fenced by one scope activation lease."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

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

TERMINAL = frozenset({"completed", "failed", "cancelled"})


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
                children.c.lease_until > (func.julianday("now") - 2440587.5) * 86400,
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
            scope.c.lease_until > (func.julianday("now") - 2440587.5) * 86400,
        )

    async def acquire(
        self, conversation_id: str, generation: int, *, seconds: float = 60
    ) -> WorkLease | None:
        if not 1 <= seconds <= 300:
            raise ValueError("invalid_work_lease_duration")
        owner = str(uuid4())
        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

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
        reporting: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= len(goal) <= 8192 or not 1 <= len(source_key) <= 256:
            raise ValueError("invalid_work_goal")
        if output_kind not in {"answer", "artifact", "state_change"}:
            raise ValueError("invalid_work_output_kind")
        if reporting is not None and reporting not in {"interactive", "quiet"}:
            raise ValueError("work_reporting_invalid")
        source_json, now = bounded_json(source), time.time()
        initial_checkpoint = bounded_json(
            {
                "communication": {
                    "input_feedback_through_id": 0,
                    **({"reporting": reporting} if reporting is not None else {}),
                }
            }
        )
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            if source.get("origin") == "self_initiative" or source.get("principal_kind") == "self":
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
                    checkpoint_json=initial_checkpoint,
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
                mailbox = (
                    await session.execute(
                        select(inputs.c.ready)
                        .where(
                            inputs.c.work_id == identity,
                            inputs.c.state.in_(("pending", "staged")),
                        )
                        .order_by(inputs.c.id)
                        .limit(1)
                    )
                ).first()
                if mailbox is not None:
                    # Attachment preparation can finish after this activation.
                    # Its admitted input must retain a live owner until then.
                    ready = bool(mailbox[0])
                    values.update(
                        state="queued" if ready else "waiting_external",
                        reason="work_input_arrived" if ready else "work_input_preparing",
                    )
                    if exit_reason is not None:
                        exit_reason = "waiting_input" if ready else "waiting_external"
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
            if row["state"] in TERMINAL:
                await session.execute(
                    update(waits)
                    .where(waits.c.work_id == identity, waits.c.status == "active")
                    .values(status="cancelled", updated=time.time())
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
                        checkpoint_json=case(
                            (
                                func.json_type(work.c.checkpoint_json, "$.communication")
                                == "object",
                                func.json_set(
                                    serialized,
                                    "$.communication",
                                    func.json_extract(work.c.checkpoint_json, "$.communication"),
                                ),
                            ),
                            else_=serialized,
                        )
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

    @staticmethod
    def encode_communication_updates(updates: dict[str, Any]) -> str:
        """Prepare a small host patch before any writer admission."""
        allowed = {
            "reporting",
            "start_feedback_given",
            "final_feedback_given",
            "input_feedback_through_id",
            "stage_feedback_batch",
        }
        if not updates or set(updates) - allowed:
            raise ValueError("work_communication_field_invalid")
        for key, value in updates.items():
            if value is None:
                continue
            if key == "reporting":
                if not isinstance(value, str) or value not in {"interactive", "quiet"}:
                    raise ValueError("work_reporting_invalid")
            elif key.endswith("_given"):
                if not isinstance(value, bool):
                    raise ValueError("work_communication_marker_invalid")
            elif key == "input_feedback_through_id":
                if type(value) is not int or value < 0:
                    raise ValueError("work_communication_marker_invalid")
            elif not isinstance(value, str) or len(value) > 64:
                raise ValueError("work_communication_marker_invalid")
        return bounded_json({"communication": updates}, 2048)

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

    async def communication_inputs(
        self, lease: WorkLease, identity: str, *, after_id: int = 0, limit: int = 8
    ) -> list[dict[str, int]]:
        """Original displayed human inputs, independent of the recent journal window."""
        from qq_ai_bot.persistence.models import ChatEventModel

        if type(after_id) is not int or after_id < 0 or not 1 <= limit <= 128:
            raise ValueError("work_communication_page_invalid")
        async with self.database.sessions() as session:
            if not await session.scalar(
                select(self._lease_table(lease).c.fence).where(self._fence(lease))
            ):
                raise WorkConflict("work_activation_obsolete")
            rows = (
                (
                    await session.execute(
                        select(inputs.c.id, inputs.c.event_id)
                        .join(ChatEventModel, ChatEventModel.id == inputs.c.event_id)
                        .where(
                            inputs.c.work_id == identity,
                            inputs.c.conversation_id == lease.conversation_id,
                            inputs.c.generation == lease.generation,
                            inputs.c.id > after_id,
                            inputs.c.state.in_(("staged", "consumed")),
                            inputs.c.kind == "message",
                            func.coalesce(func.json_extract(inputs.c.payload_json, "$.signal"), 0)
                            == 0,
                            ChatEventModel.direction == "inbound",
                        )
                        .order_by(inputs.c.id)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            return [{"id": row["id"], "event_id": row["event_id"]} for row in rows]

    async def communication_reports(
        self,
        lease: WorkLease,
        identity: str,
        target: dict[str, Any],
        *,
        kind: str | None = None,
        event_ids: tuple[int, ...] = (),
        delivered_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Query original sends; child effects and unrelated targets never qualify."""
        if kind is not None and kind not in {"start", "progress", "reply", "final"}:
            raise ValueError("work_report_kind_invalid")
        if len(event_ids) > 128:
            raise ValueError("work_communication_page_invalid")
        async with self.database.sessions() as session:
            if not await session.scalar(
                select(self._lease_table(lease).c.fence).where(self._fence(lease))
            ):
                raise WorkConflict("work_activation_obsolete")
            clauses = [
                effects.c.work_id == identity,
                func.json_extract(effects.c.receipt_json, "$.outcome.tool") == "send_message",
                func.json_type(effects.c.receipt_json, "$.outcome.work_report") == "object",
            ]
            if kind is not None:
                clauses.append(
                    func.json_extract(effects.c.receipt_json, "$.outcome.work_report.kind") == kind
                )
            if event_ids:
                links = func.json_each(
                    effects.c.receipt_json, "$.outcome.work_report.reply_to_event_ids"
                ).table_valued("value")
                clauses.append(select(links.c.value).where(links.c.value.in_(event_ids)).exists())
            for field, value in target.items():
                clauses.append(
                    func.coalesce(
                        func.json_extract(
                            effects.c.receipt_json, f"$.outcome.report_target.{field}"
                        ),
                        func.json_extract(
                            effects.c.receipt_json, f"$.outcome.delivery_target.{field}"
                        ),
                    )
                    == value
                )
            if delivered_only:
                clauses.extend(
                    (
                        effects.c.state == "accepted",
                        func.json_extract(effects.c.receipt_json, "$.outcome.delivered_message")
                        == 1,
                    )
                )
            query = (
                select(effects.c.effect_key, effects.c.state, effects.c.receipt_json)
                .where(*clauses)
                .order_by(effects.c.effect_key)
            )
            if event_ids:
                # Each queried input gets its own existence witness. A display
                # page dominated by another input cannot hide a later reply.
                witnessed = {}
                for event_id in dict.fromkeys(event_ids):
                    item = (
                        (
                            await session.execute(
                                query.where(
                                    select(links.c.value).where(links.c.value == event_id).exists()
                                ).limit(1)
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if item is not None:
                        witnessed[item["effect_key"]] = item
                rows = list(witnessed.values())
            else:
                rows = list((await session.execute(query.limit(256))).mappings().all())
            result = []
            for row in rows:
                evidence = json.loads(row["receipt_json"]).get("outcome", {})
                if (evidence.get("report_target") or evidence.get("delivery_target")) != target:
                    continue
                if row["state"] in {"prepared", "unknown"}:
                    evidence["uncertain"] = True
                if delivered_only and not (
                    row["state"] == "accepted"
                    and evidence.get("delivered_message")
                    and evidence.get("delivery_target") == target
                ):
                    continue
                result.append({**evidence, "effect_key": row["effect_key"], "state": row["state"]})
            return result

    async def communication_consumed_watermark(self, lease: WorkLease, identity: str) -> int:
        """Legacy baseline, never evidence that an input has received a reply."""
        async with self.database.sessions() as session:
            if not await session.scalar(
                select(self._lease_table(lease).c.fence).where(self._fence(lease))
            ):
                raise WorkConflict("work_activation_obsolete")
            return int(
                await session.scalar(
                    select(inputs.c.id)
                    .where(
                        inputs.c.work_id == identity,
                        inputs.c.conversation_id == lease.conversation_id,
                        inputs.c.generation == lease.generation,
                        inputs.c.state == "consumed",
                    )
                    .order_by(inputs.c.id.desc())
                    .limit(1)
                )
                or 0
            )

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
            if resume is not None and row["payload_json"] != bounded_json(resume[1], 32768):
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

    async def prepare_input(
        self, identity: int, payload: dict[str, Any], *, images: tuple[ChatImage, ...] = ()
    ) -> bool:
        from qq_ai_bot.runtime.work_media import externalize

        blobs: dict[str, bytes] = {}
        prepared = externalize({**payload, "images": [asdict(image) for image in images]}, blobs)
        serialized = bounded_json(prepared, 32768)
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
                return bool(
                    await session.scalar(
                        select(inputs.c.ready).where(
                            inputs.c.id == identity,
                            inputs.c.state.in_(("pending", "staged", "consumed")),
                            inputs.c.ready.is_(True),
                        )
                    )
                )
            work_id = changed.work_id
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
                .values(checkpoint_json=bounded_json(checkpoint, 1024 * 1024))
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
        from qq_ai_bot.runtime.protocol_schema import refs as protocol_refs

        identities = select(work.c.id).where(work.c.conversation_id == conversation_id)
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
            if (
                await session.scalar(
                    select(self._lease_table(lease).c.fence).where(self._fence(lease))
                )
                is None
            ):
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
                            payload_json=bounded_json({"text": content}, 32768),
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
                    work.c.id.not_in(select(children.c.root_id)),
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

    async def prepare_effect(
        self,
        lease: WorkLease,
        identity: str,
        key: str,
        kind: str,
        *,
        outcome: dict[str, Any] | None = None,
    ) -> bool:
        """False means an intent already exists, not that it is safe to send again."""
        now = time.time()
        receipt = bounded_json({"outcome": outcome} if outcome is not None else {})
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

    @staticmethod
    def _effect_scope(identity: str) -> Any:
        return or_(
            effects.c.work_id == identity,
            effects.c.work_id.in_(select(children.c.work_id).where(children.c.root_id == identity)),
        )

    @staticmethod
    def _unresolved_clause(*, pending: bool = True, uncertain: bool = True) -> Any:
        clauses = []
        if pending:
            clauses.append(func.json_extract(effects.c.receipt_json, "$.outcome.pending") == 1)
        if uncertain:
            clauses.extend(
                (
                    func.json_extract(effects.c.receipt_json, "$.outcome.uncertain") == 1,
                    and_(
                        effects.c.state.in_(("prepared", "unknown")),
                        func.coalesce(
                            func.json_extract(effects.c.receipt_json, "$.outcome.side_effecting"), 1
                        )
                        == 1,
                    ),
                )
            )
        return or_(*clauses) if clauses else False

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
                    func.json_extract(effects.c.receipt_json, "$.outcome").label("outcome"),
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
                    outcome = json.loads(row["outcome"] or "{}")
                    if not outcome:
                        from qq_ai_bot.capabilities.results import normalize_legacy_result
                        from qq_ai_bot.runtime.effect_outcomes import execution_evidence

                        raw = await session.scalar(
                            select(effects.c.receipt_json).where(
                                effects.c.effect_key == row["effect_key"]
                            )
                        )
                        legacy = normalize_legacy_result(
                            json.loads(raw or "{}").get("result", {}),
                            provider_id="legacy",
                            tool_name="legacy_tool",
                        )
                        outcome = execution_evidence(
                            legacy, tool="legacy_tool", side_effecting=True
                        )
                    if row["state"] in {"prepared", "unknown"}:
                        outcome["uncertain"] = outcome.get("side_effecting", True)
                    outcome.update(effect_key=row["effect_key"], work_id=row["work_id"])
                    result.append(outcome)
                cursor = rows[-1]["effect_key"]
                if limit is not None:
                    break
        return result

    async def has_unresolved_effects(
        self,
        lease: WorkLease,
        identity: str,
        *,
        pending: bool = True,
        uncertain: bool = True,
    ) -> bool:
        if not pending and not uncertain:
            return False
        async with self.database.sessions() as session:
            if not await session.scalar(
                select(self._lease_table(lease).c.fence).where(self._fence(lease))
            ):
                raise WorkConflict("work_activation_obsolete")
            return bool(
                await session.scalar(
                    select(effects.c.effect_key)
                    .where(
                        self._effect_scope(identity),
                        self._unresolved_clause(pending=pending, uncertain=uncertain),
                    )
                    .limit(1)
                )
            )

    async def resolve_run_effects(
        self,
        lease: WorkLease,
        identity: str,
        run_id: str,
        outcome: dict[str, Any],
    ) -> None:
        """Use an owned run receipt; preserve every original invocation and its mutating role."""
        async with self.database.sessions() as reader:
            rows = (
                (
                    await reader.execute(
                        select(effects).where(
                            effects.c.work_id == identity,
                            func.json_extract(effects.c.receipt_json, "$.outcome.run_id") == run_id,
                        )
                    )
                )
                .mappings()
                .all()
            )
        prepared = []
        for row in rows:
            receipt = json.loads(row["receipt_json"])
            previous = receipt.get("outcome", {})
            merged = {**previous, **outcome}
            merged["side_effecting"] = previous.get("side_effecting", False) or outcome.get(
                "side_effecting", False
            )
            merged["tool"] = previous.get("tool", outcome.get("tool"))
            receipt["outcome"] = merged
            prepared.append((row["effect_key"], row["receipt_json"], bounded_json(receipt)))
        async with self.database.immediate_session() as writer:
            await self._assert_lease(writer, lease)
            for key, previous_json, next_json in prepared:
                await writer.execute(
                    update(effects)
                    .where(
                        effects.c.effect_key == key,
                        effects.c.receipt_json == previous_json,
                    )
                    .values(receipt_json=next_json, updated=time.time())
                )

    async def record_effect(self, key: str, state: str, receipt: dict[str, Any]) -> None:
        # A late receipt must survive cancellation. It records an already-issued
        # effect, never authorizes another one, so no current lease is required.
        if state not in {"accepted", "failed", "unknown"}:
            raise ValueError("invalid_work_effect_state")
        # Invocation state is never inferred from its model-facing projection.
        if "outcome" not in receipt:
            async with self.database.sessions() as reader:
                prior = await reader.scalar(
                    select(effects.c.receipt_json).where(effects.c.effect_key == key)
                )
            existing_outcome = json.loads(prior or "{}").get("outcome", {})
            if "result" in receipt:
                from qq_ai_bot.capabilities.results import normalize_legacy_result
                from qq_ai_bot.runtime.effect_outcomes import execution_evidence

                original = normalize_legacy_result(
                    receipt["result"],
                    provider_id="legacy",
                    tool_name=existing_outcome.get("tool", "legacy_tool"),
                )
                receipt = {
                    **receipt,
                    "outcome": execution_evidence(
                        original,
                        tool=original.tool_name,
                        side_effecting=existing_outcome.get(
                            "side_effecting", original.mutation_committed is not False
                        ),
                    ),
                }
            if existing_outcome:
                receipt = {
                    **receipt,
                    "outcome": {
                        **existing_outcome,
                        **receipt.get("outcome", {}),
                        "uncertain": (
                            state == "unknown" and existing_outcome.get("side_effecting", True)
                        )
                        or receipt.get("outcome", {}).get("uncertain", False),
                    },
                }
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
