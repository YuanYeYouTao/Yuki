"""Bounded diagnostic writes outside all model/tool execution transactions."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, exists, func, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.correlation import (
    CANONICAL_KIND_MISMATCH,
    MISSING_CANONICAL_CONVERSATION,
)
from qq_ai_bot.execution_trace.db_models import ExecutionTraceEntryModel, ExecutionTraceStateModel
from qq_ai_bot.execution_trace.payload import (
    EncodedPayload,
    HTTPResponseSnapshot,
    PayloadCapacityError,
    encode_payload,
    freeze_payload,
)
from qq_ai_bot.execution_trace.phases import current_metrics, model_detail
from qq_ai_bot.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel, PresenceModel
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.diagnostic_writer import DiagnosticWriter
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.observability import current_runtime_turn_correlation

logger = logging.getLogger(__name__)


async def _require_trace_source(
    session: AsyncSession, conversation_id: str | None, source_event_id: int | None
) -> None:
    if conversation_id is None and source_event_id is None:
        return
    token = str(conversation_id).strip() if conversation_id is not None else None
    if token == "":
        raise CanonicalIdentityError(MISSING_CANONICAL_CONVERSATION)
    kind_checks = (
        [
            exists(select(model.id).where(model.id == token))
            for model in (
                PresenceModel,
                CanonicalPersonModel,
                CanonicalSpaceModel,
                CanonicalConversationModel,
            )
        ]
        if token is not None
        else [literal(False)] * 4
    )
    statement = select(*kind_checks)
    if source_event_id is not None:
        # An outer join retains the missing-event row. Its ID distinguishes a
        # missing source from a legacy source whose canonical relation is NULL.
        anchor = select(literal(1).label("trace_anchor")).subquery()
        statement = statement.add_columns(
            ChatEventModel.id, ChatEventModel.canonical_conversation_id
        ).select_from(anchor.outerjoin(ChatEventModel, ChatEventModel.id == source_event_id))
    else:
        statement = statement.add_columns(literal(None), literal(None))
    presence, person, space, conversation, event_id, event_conversation_id = (
        await session.execute(statement)
    ).one()
    # Preserve the original kind/missing-conversation priority over source errors.
    if token is not None:
        if presence or person or space:
            raise CanonicalIdentityError(CANONICAL_KIND_MISMATCH)
        if not conversation:
            raise CanonicalIdentityError(MISSING_CANONICAL_CONVERSATION)
    if source_event_id is not None and (
        event_id is None or event_conversation_id != conversation_id
    ):
        raise ValueError("invalid_trace_source_event")


async def _require_trace_delivery(
    session: AsyncSession, conversation_id: str | None, delivery: tuple[str, int]
) -> None:
    from qq_ai_bot.social.db_models import SocialOperationModel

    anchor = select(literal(1).label("delivery_anchor")).subquery()
    statement = select(
        SocialOperationModel.id,
        SocialOperationModel.status,
        SocialOperationModel.event_id,
        SocialOperationModel.source_conversation_id,
        ChatEventModel.id,
        ChatEventModel.direction,
        ChatEventModel.author_kind,
        ChatEventModel.suppression_status,
        ChatEventModel.canonical_conversation_id,
    ).select_from(
        anchor.outerjoin(SocialOperationModel, SocialOperationModel.id == delivery[0]).outerjoin(
            ChatEventModel, ChatEventModel.id == delivery[1]
        )
    )
    (
        receipt_id,
        status,
        receipt_event,
        receipt_conversation,
        event_id,
        direction,
        author,
        keeper,
        owner,
    ) = (await session.execute(statement)).one()
    if (
        receipt_id is None
        or status != "succeeded"
        or receipt_event != delivery[1]
        or receipt_conversation != conversation_id
        or event_id is None
        or direction != "outbound"
        or author != "yuki"
        or keeper != "keeper"
        or owner is None
    ):
        raise ValueError("invalid_trace_delivery")


def _encode_payload_timed(payload: object, limit: int) -> tuple[EncodedPayload, float]:
    started = time.perf_counter()
    encoded = encode_payload(payload, limit)
    return encoded, time.perf_counter() - started


def _log_slow_preparation(
    kind: str,
    turn_id: str,
    total: float,
    encode_call: float,
    encode_execution: float,
    source_validation: float,
) -> None:
    if total >= 1.0:
        logger.warning(
            "execution_trace_slow_prepare kind=%s turn_id=%s prepare_seconds=%.6f "
            "encode_call_inclusive_seconds=%.6f encode_execution_seconds=%.6f "
            "source_validation_seconds=%.6f",
            kind,
            turn_id,
            total,
            encode_call,
            encode_execution,
            source_validation,
        )


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


@dataclass(frozen=True, slots=True)
class LiveTraceSpan:
    """Process-local observation of a span that has actually entered its context."""

    turn_id: str
    operation_id: str
    conversation_id: str | None
    family: str
    origin: str | None
    started_at: datetime


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
        writer: DiagnosticWriter | None = None,
    ) -> None:
        if not 1 <= retention_days <= 365 or not 1024 <= max_payload_bytes <= 64 * 1024 * 1024:
            raise ValueError("invalid trace retention or payload limit")
        self.database = database
        self.retention_days = retention_days
        self.max_payload_bytes = max_payload_bytes
        self.writer = writer
        self.record_failures = 0
        self._live_spans: dict[str, LiveTraceSpan] = {}

    def live_spans(self, conversation_id: str) -> tuple[LiveTraceSpan, ...]:
        """A read-only, restart-ephemeral view; never used for recovery or effects."""
        return tuple(
            span
            for span in self._live_spans.values()
            if span.conversation_id == conversation_id
            and span.family in {"chat_processing", "turn"}
        )

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
            original_privacy_generation = scope.coverage.privacy_generation
            if original_privacy_generation is None:
                return
            now = datetime.now(UTC)
            control = current_work_control.get()
            conversation_id = scope.conversation_id
            source_event_id = scope.source_event_id
            work_id = None
            activation_id = None
            generation = None
            if control is not None:
                conversation_id = conversation_id or control.lease.conversation_id
                activation_id = control.lease.owner
                generation = control.lease.generation
                work_id = str(control.current["id"]) if control.current else None
                candidate = control.source.get("trigger_event_id")
                if type(candidate) is int and candidate > 0:
                    source_event_id = candidate
            # No await between admission, bounded independent copy and submit.
            # The consumer never reads the producer's scope/control/ContextVars.
            capacity = self.writer.capacity() if self.writer else 32 * 1024 * 1024
            if capacity == 0:
                if self.writer:
                    self.writer.drop(kind)
                scope.coverage.failures += 1
                self.record_failures += 1
                return
            snapshot_started = time.perf_counter()
            try:
                with model_detail("trace_snapshot"):
                    snapshot, reserved = freeze_payload(payload, capacity)
            except PayloadCapacityError:
                if self.writer:
                    self.writer.drop(kind)
                scope.coverage.failures += 1
                self.record_failures += 1
                return
            finally:
                if self.writer:
                    self.writer.record_phase(
                        "snapshot_call", time.perf_counter() - snapshot_started
                    )
            values = tuple(
                dict(
                    conversation_id=conversation_id,
                    turn_id=scope.turn_id,
                    operation_id=scope.operation_id,
                    parent_operation_id=scope.parent_operation_id,
                    work_id=work_id,
                    activation_id=activation_id,
                    execution_id=scope.execution_id,
                    source_event_id=source_event_id,
                    delivered_event_id=delivery[1] if delivery else None,
                    generation=generation,
                    origin=scope.origin,
                    kind=kind,
                    created_at=now,
                    expires_at=now + timedelta(days=self.retention_days),
                ).items()
            )
            coverage = scope.coverage
            if self.writer:
                if not self.writer.submit(
                    kind,
                    reserved,
                    lambda: self._prepare_and_commit(
                        coverage, original_privacy_generation, values, snapshot, delivery
                    ),
                ):
                    coverage.failures += 1
                    self.record_failures += 1
                return
            await self._prepare_and_commit(
                coverage, original_privacy_generation, values, snapshot, delivery
            )
        except Exception as exc:
            scope.coverage.failures += 1
            self.record_failures += 1
            logger.error(
                "execution_trace_write_failed kind=%s category=%s coverage_incomplete=true",
                kind,
                type(exc).__name__,
            )

    async def _prepare_and_commit(
        self,
        coverage: TraceCoverage,
        original_privacy_generation: int,
        frozen: tuple[tuple[str, Any], ...],
        payload: object,
        delivery: tuple[str, int] | None,
    ) -> None:
        values = dict(frozen)
        started = time.perf_counter()
        source_validation = encode_call = encode_execution = 0.0
        prepared_seconds: float | None = None
        try:
            source_started = time.perf_counter()
            try:
                async with self.database.sessions() as session:
                    await _require_trace_source(
                        session, values["conversation_id"], values["source_event_id"]
                    )
                    if delivery is not None:
                        await _require_trace_delivery(session, values["conversation_id"], delivery)
            finally:
                source_validation = time.perf_counter() - source_started
                if self.writer:
                    self.writer.record_phase("source_validation", source_validation)
            encoding_started = time.perf_counter()
            # Shield the actual worker. On shutdown, join it before releasing
            # the item's reservation; cancellation alone cannot stop a thread.
            worker = asyncio.create_task(
                asyncio.to_thread(_encode_payload_timed, payload, self.max_payload_bytes)
            )
            try:
                encoded, encode_execution = await asyncio.shield(worker)
            except asyncio.CancelledError:
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if worker.done() and not worker.cancelled():
                    worker.exception()
                raise
            encode_call = time.perf_counter() - encoding_started
            if self.writer:
                self.writer.record_phase("encode_call_inclusive", encode_call)
                self.writer.record_phase("encode_execution", encode_execution)
            values.update(
                payload_status=encoded.status,
                payload_gzip=encoded.compressed,
                payload_sha256=encoded.sha256,
                payload_bytes=encoded.size,
            )
            prepared_seconds = time.perf_counter() - started
            write_started = time.perf_counter()
            try:
                await self._insert(
                    coverage, original_privacy_generation, tuple(values.items()), delivery=delivery
                )
            finally:
                if self.writer:
                    self.writer.record_phase(
                        "diagnostic_write", time.perf_counter() - write_started
                    )
        except (Exception, asyncio.CancelledError) as exc:
            if self.writer is not None or isinstance(exc, asyncio.CancelledError):
                coverage.failures += 1
                self.record_failures += 1
            raise
        finally:
            _log_slow_preparation(
                values["kind"],
                values["turn_id"],
                prepared_seconds if prepared_seconds is not None else time.perf_counter() - started,
                encode_call,
                encode_execution,
                source_validation,
            )

    async def _insert(
        self,
        coverage: TraceCoverage,
        original_privacy_generation: int,
        frozen: tuple[tuple[str, Any], ...],
        *,
        delivery: tuple[str, int] | None = None,
    ) -> None:
        values = dict(frozen)
        table = ExecutionTraceEntryModel.__table__
        privacy_generation = (
            select(ExecutionTraceStateModel.privacy_generation)
            .where(ExecutionTraceStateModel.id == 1)
            .scalar_subquery()
        )
        guarded_values = select(
            *(literal(value, type_=table.c[key].type) for key, value in values.items())
        ).where(func.coalesce(privacy_generation, 0) == original_privacy_generation)
        # Re-check ownership in the conditional INSERT as well: deletion or
        # re-ownership between the read session and this writer cannot refill it.
        if values["conversation_id"] is not None:
            guarded_values = guarded_values.where(
                exists(
                    select(CanonicalConversationModel.id).where(
                        CanonicalConversationModel.id == str(values["conversation_id"]).strip()
                    )
                )
            )
        if values["source_event_id"] is not None:
            guarded_values = guarded_values.where(
                exists(
                    select(ChatEventModel.id).where(
                        ChatEventModel.id == values["source_event_id"],
                        ChatEventModel.canonical_conversation_id == values["conversation_id"],
                    )
                )
            )
        if delivery is not None:
            from qq_ai_bot.social.db_models import SocialOperationModel

            guarded_values = guarded_values.where(
                exists(
                    select(SocialOperationModel.id).where(
                        SocialOperationModel.id == delivery[0],
                        SocialOperationModel.status == "succeeded",
                        SocialOperationModel.event_id == delivery[1],
                        SocialOperationModel.source_conversation_id == values["conversation_id"],
                    )
                ),
                exists(
                    select(ChatEventModel.id).where(
                        ChatEventModel.id == delivery[1],
                        ChatEventModel.direction == "outbound",
                        ChatEventModel.author_kind == "yuki",
                        ChatEventModel.suppression_status == "keeper",
                        ChatEventModel.canonical_conversation_id.is_not(None),
                    )
                ),
            )
        # The erasure fence and insert are one SQL statement; an in-flight
        # response cannot recreate a deleted prompt after privacy erasure.
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                insert(ExecutionTraceEntryModel).from_select(list(values), guarded_values)
            )
            if getattr(result, "rowcount", 0) == 0:
                coverage.failures += 1
                self.record_failures += 1

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
    metric_token = current_metrics.set({}) if parent is None else None
    token = current_trace.set(scope)
    recorder._live_spans[scope.operation_id] = LiveTraceSpan(
        turn_id=scope.turn_id,
        operation_id=scope.operation_id,
        conversation_id=scope.conversation_id,
        family=family,
        origin=scope.origin,
        started_at=datetime.now(UTC),
    )
    failures_before = scope.coverage.failures
    try:
        await record_trace(f"{family}_start", payload)
        try:
            yield span
        except (Exception, asyncio.CancelledError) as exc:
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
            metrics = current_metrics.get()
            if parent is None and metrics:
                await record_trace("phase_metrics", {"phase_version": 1, **metrics})
            await record_trace(
                f"{family}_end",
                {
                    "result": span.result,
                    "record_failures": scope.coverage.failures - failures_before,
                },
            )
    finally:
        recorder._live_spans.pop(scope.operation_id, None)
        current_trace.reset(token)
        if metric_token is not None:
            current_metrics.reset(metric_token)


async def record_http_response(response: Any) -> None:
    # Bytes are immutable and independently bounded before JSON parsing. The
    # consumer never retains a response/client/session or transport credentials.
    await record_trace(
        "provider_response", HTTPResponseSnapshot(response.status_code, response.content)
    )
