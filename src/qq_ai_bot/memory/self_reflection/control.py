"""Content-free durable cycles and physical provider request budgets."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, literal, or_, select, update
from sqlalchemy.dialects.sqlite import insert

from qq_ai_bot.config import Settings
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemorySelfReflectionRuntimeModel,
)
from qq_ai_bot.persistence.models import (
    MemorySelfReflectionCycleModel as Cycle,
)
from qq_ai_bot.persistence.models import (
    MemorySelfReflectionRequestModel as Request,
)
from qq_ai_bot.persistence.models import (
    MemorySelfReflectionRunModel as Run,
)
from qq_ai_bot.persistence.models import (
    MemorySelfReflectionStateModel as State,
)


class ReflectionDailyLimit(RuntimeError):
    pass


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ReflectionControlRepository:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database, self.settings = database, settings

    async def reserve_request(self, run_id: int, attempt_kind: str = "initial") -> int:
        now = datetime.now(UTC)
        day = (
            now.astimezone(ZoneInfo(self.settings.memory_self_reflection_timezone))
            .date()
            .isoformat()
        )
        async with self.database.sessions() as session, session.begin():
            # INSERT..SELECT makes the daily limit atomic, including repairs and transport retries.
            count = (
                select(func.count(Request.id)).where(Request.local_date == day).scalar_subquery()
            )
            stmt = (
                insert(Request)
                .from_select(
                    ["run_id", "local_date", "created_at", "status", "attempt_kind"],
                    select(
                        literal(run_id),
                        literal(day),
                        literal(now),
                        literal("reserved"),
                        literal(attempt_kind),
                    ).where(count < self.settings.memory_self_reflection_max_daily_calls),
                )
                .returning(Request.id)
            )
            request_id = await session.scalar(stmt)
            if request_id is None:
                raise ReflectionDailyLimit("daily_limit_reached")
            return int(request_id)

    async def finish_request(self, request_id: int, status: str, tokens: int | None) -> None:
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(Request)
                .where(Request.id == request_id)
                .values(status=status, output_tokens=tokens)
            )

    async def snapshot(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        day = (
            now.astimezone(ZoneInfo(self.settings.memory_self_reflection_timezone))
            .date()
            .isoformat()
        )
        groups = {
            name: {"events": 0, "conversations": 0}
            for name in (
                "actionable",
                "waiting_retry",
                "isolated",
                "policy_ineligible",
                "recent_not_due",
                "processing",
            )
        }
        async with self.database.sessions() as session:
            states = (
                await session.scalars(
                    select(State).where(
                        or_(State.pending_events > 0, State.last_policy_reason.is_not(None))
                    )
                )
            ).all()
            runs = (await session.scalars(select(Run).where(Run.status != "completed"))).all()
            calls = int(
                await session.scalar(
                    select(func.count(Request.id)).where(Request.local_date == day)
                )
                or 0
            )
            ingress = int(
                await session.scalar(
                    select(MemorySelfReflectionRuntimeModel.ingress_events_total).where(
                        MemorySelfReflectionRuntimeModel.id == 1
                    )
                )
                or 0
            )
            processed = int(
                await session.scalar(
                    select(func.sum(Run.processed_events)).where(Run.status == "completed")
                )
                or 0
            )
            request_metrics = (
                await session.execute(
                    select(
                        Request.attempt_kind,
                        Request.status,
                        func.count(Request.id),
                        func.sum(Request.output_tokens),
                    ).group_by(Request.attempt_kind, Request.status)
                )
            ).all()
            recent_cycles = (
                await session.scalars(
                    select(Cycle)
                    .where(Cycle.completed_at >= now - timedelta(hours=24))
                    .order_by(Cycle.created_at.asc())
                )
            ).all()
            delivery_metrics = (
                await session.execute(
                    select(Cycle.delivery_state, func.count(Cycle.id))
                    .where(Cycle.trigger == "manual")
                    .group_by(Cycle.delivery_state)
                )
            ).all()
        oldest = 0.0
        scopes: list[dict[str, Any]] = []
        for state in states:
            if state.pending_events == 0:
                groups["policy_ineligible"]["conversations"] += 1
                continue
            own = [
                r
                for r in runs
                if r.conversation_key_hash == state.conversation_key_hash
                and r.last_event_id > state.last_event_id
            ]
            counts = {name: 0 for name in groups}
            for r in own:
                category = (
                    "processing"
                    if r.status == "processing"
                    else "isolated"
                    if r.retry_state == "isolated"
                    else "waiting_retry"
                    if r.next_attempt_at and utc(r.next_attempt_at) > now
                    else None
                )
                if category:
                    counts[category] += r.processed_events
            free = max(0, state.pending_events - sum(counts.values()))
            age = (
                max(0.0, (now - utc(state.pending_since)).total_seconds())
                if state.pending_since
                else 0.0
            )
            eligible = state.has_yuki_reply or state.has_tool_result
            due = (
                state.pending_events >= self.settings.memory_self_reflection_event_threshold
                or state.pending_characters
                >= self.settings.memory_self_reflection_character_threshold
                or age >= self.settings.memory_self_reflection_max_wait_seconds
            )
            kind = (
                "policy_ineligible" if not eligible else "actionable" if due else "recent_not_due"
            )
            counts[kind] += free
            for name, events in counts.items():
                groups[name]["events"] += events
                groups[name]["conversations"] += int(events > 0)
            if counts["actionable"]:
                oldest = max(oldest, age)
                scopes.append(
                    {
                        "owner_id": state.canonical_space_id or state.canonical_person_id,
                        "events": counts["actionable"],
                    }
                )
        rate_window = 0.0
        ingress_rate = drain_rate = None
        if recent_cycles:
            baseline = recent_cycles[0]
            rate_window = max(0.0, (now - utc(baseline.created_at)).total_seconds())
            before = json.loads(baseline.report_json).get("before", {})
            if rate_window >= 60:
                ingress_rate = (
                    max(0, ingress - before.get("ingress_events_total", ingress))
                    * 3600
                    / rate_window
                )
                drain_rate = (
                    max(0, processed - before.get("processed_events_total", processed))
                    * 3600
                    / rate_window
                )
        recent_reports = [json.loads(r.report_json) for r in recent_cycles[-3:]]
        not_decreasing = len(recent_reports) == 3 and all(
            r.get("after", {}).get("actionable", {}).get("events", 0)
            >= max(1, r.get("before", {}).get("actionable", {}).get("events", 0))
            for r in recent_reports
        )
        return {
            "rate_window_seconds": rate_window,
            "ingress_events_per_hour": ingress_rate,
            "drain_events_per_hour": drain_rate,
            "three_cycles_without_decrease": not_decreasing,
            **groups,
            "ingress_events_total": ingress,
            "processed_events_total": processed,
            "provider_requests": [
                {"attempt_kind": r[0], "status": r[1], "count": r[2], "output_tokens": r[3]}
                for r in request_metrics
            ],
            "report_deliveries": {r[0]: r[1] for r in delivery_metrics},
            "calls_today": calls,
            "daily_limit": self.settings.memory_self_reflection_max_daily_calls,
            "oldest_actionable_age_seconds": oldest,
            "top_scopes": sorted(scopes, key=lambda x: int(x["events"]), reverse=True)[:5],
        }

    async def enqueue(
        self,
        *,
        trigger: str,
        key: str,
        source_event_id: int | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        before = await self.snapshot()
        now = datetime.now(UTC)
        async with self.database.sessions() as session:
            if trigger == "manual":
                event = await session.get(ChatEventModel, source_event_id)
                if event is None or event.canonical_conversation_id != conversation_id:
                    raise ValueError("manual reflection requires its original internal event")
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                insert(Cycle)
                .values(
                    id="sr_" + uuid.uuid4().hex,
                    source_key=key,
                    trigger=trigger,
                    source_event_id=source_event_id,
                    conversation_id=conversation_id,
                    status="queued",
                    created_at=now,
                    report_json=json.dumps(
                        {
                            "before": before,
                            "bounds": {
                                "batches": self.settings.memory_self_reflection_max_batches_per_run,
                                "per_conversation": (
                                    self.settings.memory_self_reflection_max_batches_per_conversation_per_run
                                ),
                                "events": self.settings.memory_self_reflection_max_events,
                                "characters": self.settings.memory_self_reflection_max_characters,
                            },
                        }
                    ),
                    delivery_state="pending" if trigger == "manual" else "not_required",
                )
                .on_conflict_do_nothing(index_elements=["source_key"])
            )
            row = await session.scalar(select(Cycle).where(Cycle.source_key == key))
            assert row is not None
            return self.record(row)

    @staticmethod
    def record(row: Cycle) -> dict[str, Any]:
        return {
            "id": row.id,
            "trigger": row.trigger,
            "status": row.status,
            "source_event_id": row.source_event_id,
            "conversation_id": row.conversation_id,
            "created_at": utc(row.created_at).isoformat(),
            "started_at": utc(row.started_at).isoformat() if row.started_at else None,
            "completed_at": utc(row.completed_at).isoformat() if row.completed_at else None,
            "delivery_state": row.delivery_state,
            **json.loads(row.report_json),
        }

    async def get(self, run_id: str | None = None) -> dict[str, Any] | None:
        async with self.database.sessions() as session:
            query = (
                select(Cycle).where(Cycle.id == run_id)
                if run_id
                else select(Cycle)
                .where(Cycle.trigger == "manual")
                .order_by(Cycle.created_at.desc())
                .limit(1)
            )
            row = await session.scalar(query)
            return self.record(row) if row else None

    async def claim(self) -> dict[str, Any] | None:
        async with self.database.sessions() as session, session.begin():
            # One serial worker; running survives process restarts and keeps its ID/budget.
            row = await session.scalar(
                select(Cycle)
                .where(Cycle.status.in_(("running", "queued")))
                .order_by(Cycle.created_at)
                .limit(1)
            )
            if row is None:
                return None
            row.status = "running"
            row.started_at = row.started_at or datetime.now(UTC)
            return self.record(row)

    async def finish(self, cycle_id: str, report: dict[str, Any]) -> dict[str, Any]:
        after = await self.snapshot()
        async with self.database.sessions() as session, session.begin():
            row = await session.get(Cycle, cycle_id)
            assert row is not None
            row.status = "partial_failed" if report.get("failed_batches") else "completed"
            row.completed_at = datetime.now(UTC)
            row.report_json = json.dumps(
                {
                    **json.loads(row.report_json),
                    **report,
                    "after": after,
                    "duration_seconds": (
                        row.completed_at - utc(row.started_at or row.created_at)
                    ).total_seconds(),
                }
            )
            return self.record(row)

    async def retry_due(self) -> bool:
        async with self.database.sessions() as session:
            return bool(
                await session.scalar(
                    select(Run.id)
                    .where(Run.retry_state == "waiting", Run.next_attempt_at <= datetime.now(UTC))
                    .limit(1)
                )
            )

    async def cycle_runs(self, cycle_id: str) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            rows = (await session.scalars(select(Run).where(Run.cycle_id == cycle_id))).all()
            return [
                {
                    "id": r.id,
                    "owner": r.conversation_key_hash,
                    "status": r.status,
                    "events": r.processed_events,
                    "characters": r.processed_characters,
                    "proposals": r.proposal_count,
                    "committed": r.committed_count,
                    "error": r.error_category,
                    "first_event_id": r.first_event_id,
                    "last_event_id": r.last_event_id,
                    "retry_state": r.retry_state,
                    "next_attempt_at": utc(r.next_attempt_at).isoformat()
                    if r.next_attempt_at
                    else None,
                }
                for r in rows
            ]

    async def pending_reports(self) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            rows = (
                await session.scalars(
                    select(Cycle)
                    .where(
                        Cycle.trigger == "manual",
                        Cycle.status.in_(("completed", "partial_failed", "failed")),
                        Cycle.delivery_state == "pending",
                    )
                    .order_by(Cycle.created_at)
                    .limit(5)
                )
            ).all()
            return [self.record(r) for r in rows]

    async def delivered(self, cycle_id: str, receipt: dict[str, Any]) -> None:
        state = receipt.get("status", "uncertain")
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(Cycle)
                .where(Cycle.id == cycle_id)
                .values(
                    delivery_state="delivered"
                    if state == "succeeded"
                    else "unknown"
                    if state in ("uncertain", "executing")
                    else "failed",
                    delivery_receipt_json=json.dumps(receipt),
                )
            )

    async def drain_active(self) -> bool:
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(Cycle)
                .where(Cycle.trigger == "drain")
                .order_by(Cycle.created_at.desc())
                .limit(1)
            )
            if row is None:
                return False
            report = json.loads(row.report_json)
            return bool(
                report.get("after", report.get("before", {})).get("actionable", {}).get("events", 0)
                >= self.settings.memory_self_reflection_drain_low_events
            )

    async def resume_batch(self, run_id: int) -> bool:
        async with self.database.sessions() as session, session.begin():
            row = await session.get(Run, run_id)
            if row is None or row.status != "failed":
                return False
            row.retry_state = "waiting"
            row.next_attempt_at = datetime.now(UTC)
            return True
