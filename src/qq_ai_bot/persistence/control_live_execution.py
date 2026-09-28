"""Bounded, read-only current execution and event-to-turn projections."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import TypedDict, cast

from sqlalchemy import DateTime, Integer, String, bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.control_plane.paging import Page, PageRequest
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import ActivityView, ControlQueryError
from qq_ai_bot.domain.identity import ConversationId
from qq_ai_bot.execution_trace.db_models import ExecutionTraceEntryModel as Trace
from qq_ai_bot.execution_trace.recorder import LiveTraceSpan, TraceRecorder
from qq_ai_bot.persistence.models import ChatEventModel


def _stamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


class _StepRow(TypedDict):
    id: int
    kind: str
    created_at: str | None
    payload_status: str


async def _steps(
    session: AsyncSession, *, conversation_id: str, turn_id: str, observed_at: datetime
) -> tuple[list[_StepRow], bool, str | None, str | None]:
    # SQLite otherwise chooses the conversation index and scans unrelated
    # turns in a busy group. Keep both the turn and conversation checks.
    statement = (
        text(
            "SELECT id, kind, created_at, payload_status, origin "
            "FROM execution_trace_entries INDEXED BY ix_execution_trace_turn_id "
            "WHERE conversation_id = :conversation_id AND turn_id = :turn_id "
            "AND expires_at > :observed_at ORDER BY id DESC LIMIT 33"
        )
        .bindparams(bindparam("observed_at", type_=DateTime(timezone=True)))
        .columns(
            id=Integer,
            kind=String,
            created_at=DateTime(timezone=True),
            payload_status=String,
            origin=String,
        )
    )
    result = await session.execute(
        statement,
        {
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "observed_at": observed_at,
        },
    )
    rows = result.mappings().all()
    latest_kind = rows[0]["kind"] if rows else None
    origin = next((row["origin"] for row in rows if row["origin"]), None)
    steps: list[_StepRow] = [
        {
            "id": row["id"],
            "kind": row["kind"],
            "created_at": _stamp(row["created_at"]),
            "payload_status": row["payload_status"],
        }
        for row in reversed(rows[:32])
    ]
    return steps, len(rows) > 32, latest_kind, origin


def _completion(kind: str | None) -> str:
    if kind in {"chat_processing_error", "turn_error"}:
        return "failed"
    if kind in {"chat_processing_end", "turn_end"}:
        return "completed"
    return "evidence_insufficient"


async def _terminal_kind(
    session: AsyncSession, *, conversation_id: str, turn_id: str, observed_at: datetime
) -> str | None:
    statement = text(
        "SELECT kind FROM execution_trace_entries INDEXED BY ix_execution_trace_turn_id "
        "WHERE conversation_id = :conversation_id AND turn_id = :turn_id "
        "AND expires_at > :observed_at "
        "AND kind IN ('chat_processing_end', 'chat_processing_error', 'turn_end', 'turn_error') "
        "ORDER BY id DESC LIMIT 1"
    ).bindparams(bindparam("observed_at", type_=DateTime(timezone=True)))
    return cast(
        str | None,
        await session.scalar(
            statement,
            {
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "observed_at": observed_at,
            },
        ),
    )


async def read_conversation_execution(
    reader: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    conversation_id: ConversationId,
    recorder: TraceRecorder | None,
) -> ActivityView:
    """Active means an actual process-local Runner span, never an unmatched old start."""
    observed_at = datetime.now(UTC)
    live = recorder.live_spans(conversation_id.text) if recorder else ()
    active_by_turn: dict[str, list[LiveTraceSpan]] = {}
    for span in live:
        active_by_turn.setdefault(span.turn_id, []).append(span)
    async with reader() as session:
        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

        if await session.get(CanonicalConversationModel, conversation_id.text) is None:
            raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
        # Ordinary tool/model steps far outnumber roots in a busy group.
        # Keep the predicate literal so SQLite can select the partial index.
        root_statement = (
            text(
                "SELECT turn_id, created_at FROM execution_trace_entries "
                "INDEXED BY ix_execution_trace_roots "
                "WHERE conversation_id = :conversation_id AND expires_at > :observed_at "
                "AND kind IN ('chat_processing_start', 'turn_start') "
                "ORDER BY id DESC LIMIT 6"
            )
            .bindparams(bindparam("observed_at", type_=DateTime(timezone=True)))
            .columns(turn_id=String, created_at=DateTime(timezone=True))
        )
        root_rows = (
            await session.execute(
                root_statement,
                {"conversation_id": conversation_id.text, "observed_at": observed_at},
            )
        ).all()
        recent_ids = list(dict.fromkeys(row.turn_id for row in root_rows))[:3]
        active: list[dict[str, object]] = []
        for turn_id, spans in list(active_by_turn.items())[:8]:
            steps, truncated, latest_kind, origin = await _steps(
                session,
                conversation_id=conversation_id.text,
                turn_id=turn_id,
                observed_at=observed_at,
            )
            active.append(
                {
                    "turn_id": turn_id,
                    "original_conversation_id": conversation_id.text,
                    "origin": origin or next((span.origin for span in spans if span.origin), None),
                    "started_at": _stamp(min(span.started_at for span in spans)),
                    "last_step_at": steps[-1]["created_at"] if steps else None,
                    "latest_kind": latest_kind,
                    "status": "active",
                    "steps": steps,
                    "steps_truncated": truncated,
                    "evidence": "live_runner_span",
                }
            )
        recent: list[dict[str, object]] = []
        recent_latest_id: dict[str, int] = {}
        for turn_id in recent_ids:
            if turn_id in active_by_turn:
                continue
            steps, truncated, latest_kind, origin = await _steps(
                session,
                conversation_id=conversation_id.text,
                turn_id=turn_id,
                observed_at=observed_at,
            )
            first = min(row.created_at for row in root_rows if row.turn_id == turn_id)
            recent_latest_id[turn_id] = steps[-1]["id"] if steps else 0
            recent.append(
                {
                    "turn_id": turn_id,
                    "original_conversation_id": conversation_id.text,
                    "origin": origin,
                    "started_at": _stamp(first),
                    "last_step_at": steps[-1]["created_at"] if steps else None,
                    "latest_kind": latest_kind,
                    "status": _completion(
                        await _terminal_kind(
                            session,
                            conversation_id=conversation_id.text,
                            turn_id=turn_id,
                            observed_at=observed_at,
                        )
                    ),
                    "steps": steps,
                    "steps_truncated": truncated,
                }
            )
        recent.sort(
            key=lambda item: recent_latest_id[str(item["turn_id"])],
            reverse=True,
        )
        recent = recent[:2]
    state = (
        "active"
        if active
        else "evidence_insufficient"
        if recorder is None or (recent and recent[0]["status"] == "evidence_insufficient")
        else "idle"
    )
    return ActivityView(
        conversation_id.text,
        {
            "conversation_id": conversation_id.text,
            "observed_at": _stamp(observed_at),
            "state": state,
            "active": active,
            "recent": recent,
            "coverage_note": (
                "diagnostic_history_is_bounded_and_may_be_incomplete"
                if state == "evidence_insufficient"
                or any(item["steps_truncated"] for item in [*active, *recent])
                else None
            ),
        },
    )


async def list_event_turns(
    reader: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    request: PageRequest,
    *,
    conversation_id: ConversationId,
    event_id: int,
    direction: str,
) -> Page[ActivityView]:
    """Only trusted ledger/source/delivery IDs can associate an event with turns."""
    if type(event_id) is not int or event_id < 1 or direction not in {"inbound", "outbound"}:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    if request.cursor is not None or request.limit > 20:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    observed_at = datetime.now(UTC)
    async with reader() as session:
        event = await session.get(ChatEventModel, event_id)
        if event is None or event.canonical_conversation_id != conversation_id.text:
            raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
        if event.direction != direction:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        if direction == "outbound" and (
            event.author_kind != "yuki" or event.suppression_status != "keeper"
        ):
            return Page((), snapshot_at=observed_at, total=0, number=request.number or 1)
        if direction == "inbound":
            candidate_statement = (
                text(
                    "SELECT turn_id, conversation_id, MIN(created_at) AS created_at, "
                    "MAX(id) AS last_id FROM execution_trace_entries "
                    "INDEXED BY ix_execution_trace_source_event "
                    "WHERE source_event_id = :source_event_id "
                    "AND source_event_id IS NOT NULL "
                    "AND conversation_id = :conversation_id "
                    "AND expires_at > :observed_at "
                    "GROUP BY turn_id, conversation_id"
                )
                .bindparams(
                    bindparam("source_event_id", event_id),
                    bindparam("conversation_id", conversation_id.text),
                    bindparam("observed_at", observed_at, type_=DateTime(timezone=True)),
                )
                .columns(
                    turn_id=String,
                    conversation_id=String,
                    created_at=DateTime(timezone=True),
                    last_id=Integer,
                )
            )
            candidates = candidate_statement.subquery("event_turn_candidates")
        else:
            candidates = (
                select(
                    Trace.turn_id,
                    Trace.conversation_id,
                    func.min(Trace.created_at).label("created_at"),
                    func.max(Trace.id).label("last_id"),
                )
                .where(
                    Trace.delivered_event_id == event_id,
                    Trace.kind == "social_delivery",
                    Trace.expires_at > observed_at,
                )
                .group_by(Trace.turn_id, Trace.conversation_id)
                .subquery("event_turn_candidates")
            )
        # Check each distinct candidate once. Correlating this check to every
        # trace row made SQLite rescan a busy conversation for each step.
        # A delivery can land elsewhere; the root must share the *original*
        # conversation as well as the turn token.
        rows = (
            select(candidates)
            .where(
                text(
                    "EXISTS (SELECT 1 FROM execution_trace_entries AS root "
                    "INDEXED BY ix_execution_trace_turn_id "
                    "WHERE root.turn_id = event_turn_candidates.turn_id "
                    "AND root.conversation_id = event_turn_candidates.conversation_id "
                    "AND root.expires_at > :root_observed_at "
                    "AND root.kind IN ('chat_processing_start', 'turn_start'))"
                ).bindparams(
                    bindparam("root_observed_at", observed_at, type_=DateTime(timezone=True))
                )
            )
            .subquery()
        )
        total = int(await session.scalar(select(func.count()).select_from(rows)) or 0)
        selected = (
            (
                await session.execute(
                    select(rows)
                    .order_by(rows.c.last_id.desc())
                    .offset(((request.number or 1) - 1) * request.limit)
                    .limit(request.limit)
                )
            )
            .mappings()
            .all()
        )
        items = []
        for row in selected:
            first_statement = (
                text(
                    "SELECT created_at, origin FROM execution_trace_entries "
                    "INDEXED BY ix_execution_trace_turn_id "
                    "WHERE turn_id = :turn_id AND conversation_id = :conversation_id "
                    "AND expires_at > :observed_at ORDER BY id ASC LIMIT 1"
                )
                .bindparams(bindparam("observed_at", type_=DateTime(timezone=True)))
                .columns(created_at=DateTime(timezone=True), origin=String)
            )
            turn_first = (
                (
                    await session.execute(
                        first_statement,
                        {
                            "turn_id": row["turn_id"],
                            "conversation_id": row["conversation_id"],
                            "observed_at": observed_at,
                        },
                    )
                )
                .mappings()
                .first()
            )
            terminal = await _terminal_kind(
                session,
                conversation_id=row["conversation_id"],
                turn_id=row["turn_id"],
                observed_at=observed_at,
            )
            items.append(
                ActivityView(
                    row["turn_id"],
                    {
                        "turn_id": row["turn_id"],
                        "origin": turn_first["origin"] if turn_first else None,
                        "created_at": _stamp(
                            turn_first["created_at"] if turn_first else row["created_at"]
                        ),
                        "original_conversation_id": row["conversation_id"],
                        "trace_status": _completion(terminal),
                    },
                )
            )
    return Page(items, snapshot_at=observed_at, total=total, number=request.number or 1)
