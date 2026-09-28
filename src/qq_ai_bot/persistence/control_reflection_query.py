"""Self-reflection observability over original cycles, runs, watermarks and requests."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, literal, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from qq_ai_bot.config import Settings
from qq_ai_bot.control_plane.paging import Page, PageRequest
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import (
    ActivityView,
    ControlQueryError,
    QueryResourceKind,
    ReflectionQueryFilter,
)
from qq_ai_bot.memory.self_reflection.control import ReflectionControlRepository
from qq_ai_bot.memory.self_reflection.db_models import (
    InitiativeReflectionCursorModel,
    InitiativeReflectionWindowModel,
)
from qq_ai_bot.persistence.control_activity_query import _stamp
from qq_ai_bot.persistence.control_execution_query import _key, _page
from qq_ai_bot.persistence.control_memory_query import _id
from qq_ai_bot.persistence.control_paging import numbered_statement
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemorySelfReflectionCycleModel as Cycle,
)
from qq_ai_bot.persistence.models import (
    MemorySelfReflectionRequestModel as Request,
)
from qq_ai_bot.persistence.models import (
    MemorySelfReflectionResultModel as Result,
)
from qq_ai_bot.persistence.models import (
    MemorySelfReflectionRunModel as Run,
)
from qq_ai_bot.persistence.models import (
    MemorySelfReflectionStateModel as State,
)


class ControlReflectionQueryAdapter:
    def __init__(
        self,
        database: Database,
        reader: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        settings: Settings | None,
    ) -> None:
        self._database, self._reader, self._settings = database, reader, settings

    async def read_self_reflection_health(self) -> ActivityView:
        if self._settings is None:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        try:
            snapshot = await ReflectionControlRepository(self._database, self._settings).snapshot()
        except SQLAlchemyError as exc:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE)) from exc
        except (ValueError, TypeError) as exc:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from exc
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        async with self._reader() as session:
            batches = (
                (
                    await session.execute(
                        select(
                            Run.status,
                            func.count(Run.id).label("batches"),
                            func.sum(Run.processed_events).label("processed_events"),
                            func.sum(Run.proposal_count).label("proposals"),
                            func.sum(Run.committed_count).label("committed"),
                        )
                        .where(Run.started_at >= cutoff)
                        .group_by(Run.status)
                    )
                )
                .mappings()
                .all()
            )
            calls = (
                (
                    await session.execute(
                        select(
                            Request.status,
                            func.count(Request.id).label("requests"),
                            func.count(Request.output_tokens).label("known_usage_requests"),
                            func.sum(Request.output_tokens).label("output_tokens"),
                        )
                        .where(Request.created_at >= cutoff)
                        .group_by(Request.status)
                    )
                )
                .mappings()
                .all()
            )
        snapshot["last_24h"] = {
            "since": cutoff.isoformat(),
            "batches_by_status": [dict(row) for row in batches],
            "requests_by_status": [dict(row) for row in calls],
        }
        return ActivityView("self_reflection", snapshot)

    async def list_self_reflection_history(
        self, request: PageRequest, *, section: str, scope: ReflectionQueryFilter | None = None
    ) -> Page[ActivityView]:
        scope = scope or ReflectionQueryFilter()
        if type(scope) is not ReflectionQueryFilter or section not in {
            "states",
            "runs",
            "cycles",
            "requests",
            "results",
            "receipt_cursors",
        }:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        if section in {"states", "receipt_cursors"} and (
            scope.run_id is not None or scope.cycle_id is not None
        ):
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        partition = json.dumps(
            [
                section,
                scope.person_id.text if scope.person_id else None,
                scope.space_id.text if scope.space_id else None,
                scope.cycle_id,
                scope.run_id,
            ]
        )
        key = _key(request, QueryResourceKind.REFLECTION, partition)
        names: tuple[str, ...]
        owner: type[State] | type[Run] | None
        model: type[Request] | type[Result]
        marker: InstrumentedAttribute[str] | InstrumentedAttribute[int]
        after: str | int
        numbered_order: tuple[ColumnElement[Any], ...]
        if section == "states":
            names = (
                "id",
                "canonical_person_id",
                "canonical_space_id",
                "last_event_id",
                "latest_event_id",
                "pending_events",
                "pending_characters",
                "pending_since",
                "has_yuki_reply",
                "has_tool_result",
                "high_value_signal",
                "last_policy_reason",
                "last_policy_event_id",
                "updated_at",
            )
            stmt = select(*(getattr(State, name) for name in names))
            owner = State
            marker = State.id
        elif section == "runs":
            names = (
                "id",
                "canonical_person_id",
                "canonical_space_id",
                "scheduled_slot",
                "trigger_reason",
                "first_event_id",
                "last_event_id",
                "status",
                "proposal_count",
                "committed_count",
                "error_category",
                "started_at",
                "completed_at",
                "cycle_id",
                "attempt_count",
                "retry_state",
                "next_attempt_at",
                "processed_events",
                "processed_characters",
            )
            stmt = select(
                *(getattr(Run, name) for name in names),
                InitiativeReflectionWindowModel.initiative_run_id,
                InitiativeReflectionWindowModel.first_receipt_id,
                InitiativeReflectionWindowModel.last_receipt_id,
            ).outerjoin(
                InitiativeReflectionWindowModel,
                InitiativeReflectionWindowModel.reflection_run_id == Run.id,
            )
            owner = Run
            marker = Run.id
        elif section == "cycles":
            stmt = select(
                Cycle.id,
                Cycle.trigger,
                Cycle.status,
                Cycle.source_event_id,
                Cycle.conversation_id,
                Cycle.created_at,
                Cycle.started_at,
                Cycle.completed_at,
                Cycle.delivery_state,
            )
            if scope.person_id or scope.space_id or scope.run_id:
                predicate = select(Run.id).where(Run.cycle_id == Cycle.id)
                if scope.person_id:
                    predicate = predicate.where(Run.canonical_person_id == scope.person_id.text)
                if scope.space_id:
                    predicate = predicate.where(Run.canonical_space_id == scope.space_id.text)
                if scope.run_id:
                    predicate = predicate.where(Run.id == scope.run_id)
                stmt = stmt.where(predicate.exists())
            if scope.cycle_id:
                stmt = stmt.where(Cycle.id == scope.cycle_id)
            owner = None
            marker = Cycle.id
        elif section == "receipt_cursors":
            # Original receipt cursor is per initiative, independent of chat watermarks.
            from qq_ai_bot.conversation.autonomy_db_models import InitiativeRunModel

            stmt = select(
                InitiativeReflectionCursorModel.initiative_run_id,
                InitiativeReflectionCursorModel.last_receipt_id,
                InitiativeRunModel.space_id.label("canonical_space_id"),
            ).join(
                InitiativeRunModel,
                InitiativeRunModel.id == InitiativeReflectionCursorModel.initiative_run_id,
            )
            if scope.person_id:
                raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
            if scope.space_id:
                stmt = stmt.where(InitiativeRunModel.space_id == scope.space_id.text)
            owner = None
            marker = InitiativeReflectionCursorModel.initiative_run_id
        else:
            if section == "requests":
                model = Request
                names = (
                    "id",
                    "run_id",
                    "local_date",
                    "created_at",
                    "status",
                    "attempt_kind",
                    "output_tokens",
                )
            else:
                model = Result
                names = ("id", "run_id", "fact_id", "result_kind", "result_index", "created_at")
            stmt = select(*(getattr(model, name) for name in names)).join(
                Run, Run.id == model.run_id
            )
            owner = Run
            marker = model.id
        if owner is not None:
            if scope.person_id:
                stmt = stmt.where(owner.canonical_person_id == scope.person_id.text)
            if scope.space_id:
                stmt = stmt.where(owner.canonical_space_id == scope.space_id.text)
            if owner is Run:
                if scope.run_id:
                    stmt = stmt.where(Run.id == scope.run_id)
                if scope.cycle_id:
                    stmt = stmt.where(Run.cycle_id == scope.cycle_id)
        if key is not None:
            if section == "cycles":
                try:
                    created, identity = json.loads(key)
                    at = datetime.fromisoformat(created)
                    if (
                        at.tzinfo is None
                        or type(identity) is not str
                        or not identity.isascii()
                        or not 1 <= len(identity) <= 64
                    ):
                        raise ValueError("invalid cycle cursor")
                except (ValueError, TypeError) as exc:
                    raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
                stmt = stmt.where(
                    tuple_(Cycle.created_at, Cycle.id) < tuple_(literal(at), literal(identity))
                )
            elif section == "receipt_cursors":
                if not key.isascii() or len(key) > 64:
                    raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
                after = key
                stmt = stmt.where(marker < after)
            else:
                if (
                    not key.isascii()
                    or not key.isdecimal()
                    or len(key) > 19
                    or str(int(key)) != key
                ):
                    raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
                after = _id(int(key))
                stmt = stmt.where(marker < after)
        if section == "states":
            numbered_order = (State.updated_at.desc(), State.id.desc())
        elif section == "runs":
            numbered_order = (Run.started_at.desc(), Run.id.desc())
        elif section == "cycles":
            numbered_order = (Cycle.created_at.desc(), Cycle.id.desc())
        elif section == "receipt_cursors":
            numbered_order = (
                InitiativeRunModel.created_at.desc(),
                InitiativeReflectionCursorModel.initiative_run_id.desc(),
            )
        else:
            numbered_order = (model.created_at.desc(), model.id.desc())
        async with self._reader() as session:
            rows = (
                (
                    await session.execute(
                        (
                            sql_window := await numbered_statement(
                                session,
                                stmt.order_by(
                                    *(
                                        [Cycle.created_at.desc(), Cycle.id.desc()]
                                        if section == "cycles"
                                        else [marker.desc()]
                                    )
                                ).limit(request.limit + 1),
                                request,
                                order=numbered_order,
                            )
                        ).statement
                    )
                )
                .mappings()
                .all()
            )
        items = []
        for row in rows[: request.limit]:
            fields = {
                name: _stamp(value) if isinstance(value, datetime) else value
                for name, value in row.items()
            }
            if section == "runs":
                fields["source_kind"] = "initiative_tools" if row["initiative_run_id"] else "chat"
                if row["initiative_run_id"]:
                    fields["first_event_id"] = fields["last_event_id"] = None
            resource = str(fields.get("id", fields.get("initiative_run_id")))
            items.append(ActivityView(resource, fields))
        keyname = "initiative_run_id" if section == "receipt_cursors" else "id"
        next_key = None
        if len(rows) >= request.limit:
            last = rows[request.limit - 1]
            next_key = (
                json.dumps([_stamp(last["created_at"]), last["id"]], separators=(",", ":"))
                if section == "cycles"
                else str(last[keyname])
            )
        return _page(
            items,
            rows,
            request,
            QueryResourceKind.REFLECTION,
            partition,
            next_key,
            total=sql_window.total,
            number=request.number,
        )
