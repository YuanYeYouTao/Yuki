"""Bounded diagnostic writes outside all model/tool execution transactions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, insert, literal, select

from qq_ai_bot.conversation.correlation import require_live_conversation
from qq_ai_bot.execution_trace.db_models import ExecutionTraceEntryModel, ExecutionTraceStateModel
from qq_ai_bot.execution_trace.payload import encode_payload
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.observability import current_runtime_turn_correlation

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TraceCoverage:
    privacy_generation: int | None
    failures: int = 0


@dataclass(frozen=True, slots=True)
class TraceScope:
    recorder: TraceRecorder
    coverage: TraceCoverage
    turn_id: str
    operation_id: str
    parent_operation_id: str | None
    conversation_id: str | None
    execution_id: str | None
    origin: str | None
    source_event_id: int | None


current_trace: ContextVar[TraceScope | None] = ContextVar(
    "execution_diagnostic_scope", default=None
)


class TraceRecorder:
    def __init__(
        self,
        database: Database,
        *,
        retention_days: int = 30,
        max_payload_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if not 1 <= retention_days <= 365 or not 1024 <= max_payload_bytes <= 64 * 1024 * 1024:
            raise ValueError("invalid trace retention or payload limit")
        self.database = database
        self.retention_days = retention_days
        self.max_payload_bytes = max_payload_bytes
        self.record_failures = 0

    async def coverage(self) -> TraceCoverage:
        try:
            async with self.database.sessions() as session:
                generation = await session.scalar(
                    select(ExecutionTraceStateModel.privacy_generation).where(
                        ExecutionTraceStateModel.id == 1
                    )
                )
            return TraceCoverage(generation or 0)
        except Exception as exc:
            self.record_failures += 1
            logger.error(
                "execution_trace_write_failed kind=privacy_guard "
                "category=%s coverage_incomplete=true",
                type(exc).__name__,
            )
            return TraceCoverage(None, 1)

    async def append(
        self,
        scope: TraceScope,
        kind: str,
        payload: object,
        *,
        delivery: tuple[str, int] | None = None,
    ) -> None:
        from qq_ai_bot.runtime.work_activation import current_work_control

        try:
            if scope.coverage.privacy_generation is None:
                return
            now = datetime.now(UTC)
            control = current_work_control.get()
            conversation_id = scope.conversation_id
            source_event_id = scope.source_event_id
            work_id = None
            activation_id = None
            generation = None
            delivered_event_id = None
            if control is not None:
                conversation_id = conversation_id or control.lease.conversation_id
                activation_id = control.lease.owner
                generation = control.lease.generation
                work_id = str(control.current["id"]) if control.current else None
                candidate = control.source.get("trigger_event_id")
                if type(candidate) is int and candidate > 0:
                    source_event_id = candidate
            encoded = await asyncio.to_thread(encode_payload, payload, self.max_payload_bytes)
            # Resolve trusted identifiers before adding a row or taking the writer.
            async with self.database.sessions() as session:
                if conversation_id is not None:
                    await require_live_conversation(session, conversation_id)
                if source_event_id is not None:
                    event = await session.get(ChatEventModel, source_event_id)
                    if event is None or event.canonical_conversation_id != conversation_id:
                        raise ValueError("invalid_trace_source_event")
                if delivery is not None:
                    from qq_ai_bot.social.db_models import SocialOperationModel

                    operation_id, event_id = delivery
                    receipt = await session.get(SocialOperationModel, operation_id)
                    event = await session.get(ChatEventModel, event_id)
                    if (
                        receipt is None
                        or receipt.status != "succeeded"
                        or receipt.event_id != event_id
                        or receipt.source_conversation_id != conversation_id
                        or event is None
                        or event.direction != "outbound"
                        or event.author_kind != "yuki"
                        or event.suppression_status != "keeper"
                        or event.canonical_conversation_id is None
                    ):
                        raise ValueError("invalid_trace_delivery")
                    delivered_event_id = event.id
            values = dict(
                conversation_id=conversation_id,
                turn_id=scope.turn_id,
                operation_id=scope.operation_id,
                parent_operation_id=scope.parent_operation_id,
                work_id=work_id,
                activation_id=activation_id,
                execution_id=scope.execution_id,
                source_event_id=source_event_id,
                delivered_event_id=delivered_event_id,
                generation=generation,
                origin=scope.origin,
                kind=kind,
                payload_status=encoded.status,
                payload_gzip=encoded.compressed,
                payload_sha256=encoded.sha256,
                payload_bytes=encoded.size,
                created_at=now,
                expires_at=now + timedelta(days=self.retention_days),
            )
            table = ExecutionTraceEntryModel.__table__
            privacy_generation = (
                select(ExecutionTraceStateModel.privacy_generation)
                .where(ExecutionTraceStateModel.id == 1)
                .scalar_subquery()
            )
            guarded_values = select(
                *(literal(value, type_=table.c[key].type) for key, value in values.items())
            ).where(func.coalesce(privacy_generation, 0) == scope.coverage.privacy_generation)
            # The erasure fence and insert are one SQL statement; an in-flight
            # response cannot recreate a deleted prompt after privacy erasure.
            async with self.database.sessions() as session, session.begin():
                result = await session.execute(
                    insert(ExecutionTraceEntryModel).from_select(list(values), guarded_values)
                )
                if getattr(result, "rowcount", 0) == 0:
                    scope.coverage.failures += 1
        except Exception as exc:
            scope.coverage.failures += 1
            self.record_failures += 1
            logger.error(
                "execution_trace_write_failed kind=%s category=%s coverage_incomplete=true",
                kind,
                type(exc).__name__,
            )

    async def cleanup_expired(self, *, now: datetime | None = None) -> int:
        cutoff = now or datetime.now(UTC)
        deleted = 0
        while True:
            async with self.database.sessions() as session, session.begin():
                selected = (
                    select(ExecutionTraceEntryModel.id)
                    .where(ExecutionTraceEntryModel.expires_at <= cutoff)
                    .order_by(ExecutionTraceEntryModel.expires_at, ExecutionTraceEntryModel.id)
                    .limit(500)
                )
                result = await session.execute(
                    delete(ExecutionTraceEntryModel).where(
                        ExecutionTraceEntryModel.id.in_(selected)
                    )
                )
                batch = int(getattr(result, "rowcount", 0) or 0)
            deleted += batch
            if batch < 500:
                return deleted
            # Drain this fixed expiry window, releasing the writer between batches.
            await asyncio.sleep(0)


async def record_trace(kind: str, payload: object) -> None:
    scope = current_trace.get()
    if scope is not None:
        await scope.recorder.append(scope, kind, payload)


async def record_confirmed_delivery(operation_id: str, event_id: int) -> None:
    """Post-commit diagnostic link; never controls or retries the actual send."""
    scope = current_trace.get()
    if scope is not None:
        await scope.recorder.append(
            scope,
            "social_delivery",
            {"social_operation_id": operation_id, "event_id": event_id},
            delivery=(operation_id, event_id),
        )


@dataclass(slots=True)
class TraceSpan:
    family: str
    result: object = None


@asynccontextmanager
async def trace_span(
    family: str,
    payload: object,
    *,
    recorder: TraceRecorder | None = None,
    conversation_id: str | None = None,
    execution_id: str | None = None,
    origin: str | None = None,
    source_event_id: int | None = None,
) -> AsyncIterator[TraceSpan]:
    parent = current_trace.get()
    recorder = recorder or (parent.recorder if parent else None)
    span = TraceSpan(family)
    if recorder is None:
        yield span
        return
    correlation = current_runtime_turn_correlation()
    scope = TraceScope(
        recorder=recorder,
        coverage=parent.coverage if parent else await recorder.coverage(),
        turn_id=parent.turn_id if parent else (correlation.turn_id if correlation else uuid4().hex),
        operation_id=uuid4().hex,
        parent_operation_id=parent.operation_id if parent else None,
        conversation_id=conversation_id or (parent.conversation_id if parent else None),
        execution_id=execution_id or (parent.execution_id if parent else None),
        origin=origin or (parent.origin if parent else None),
        source_event_id=source_event_id or (parent.source_event_id if parent else None),
    )
    token = current_trace.set(scope)
    failures_before = scope.coverage.failures
    try:
        await record_trace(f"{family}_start", payload)
        try:
            yield span
        except BaseException as exc:
            from qq_ai_bot.runtime.activation_outcome import classify_failure

            await record_trace(
                f"{family}_error",
                {
                    "error_category": type(exc).__name__,
                    "failure": asdict(classify_failure(exc)),
                    "record_failures": scope.coverage.failures - failures_before,
                },
            )
            raise
        else:
            await record_trace(
                f"{family}_end",
                {
                    "result": span.result,
                    "record_failures": scope.coverage.failures - failures_before,
                },
            )
    finally:
        current_trace.reset(token)


async def record_http_response(response: Any) -> None:
    try:
        body = response.json()
    except ValueError:
        body = {"trace_omitted": "non_json_response", "bytes": len(response.content)}
    await record_trace(
        "provider_response",
        {"http_status": response.status_code, "dispatch": "response_received", "body": body},
    )
