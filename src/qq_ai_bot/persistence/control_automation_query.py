"""Original automation definitions and paged run/step metadata, never execution payloads."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.control_plane.paging import Page, PageRequest
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import ActivityView, ControlQueryError, QueryResourceKind
from qq_ai_bot.persistence.control_execution_query import _key, _page
from qq_ai_bot.persistence.control_paging import numbered_statement
from qq_ai_bot.persistence.models import AutomationModel, AutomationRunModel, AutomationStepRunModel
from qq_ai_bot.persistence.unit_of_work import state_revision


def _stamp(value: datetime | None) -> str | None:
    return (
        (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)).isoformat()
        if value is not None
        else None
    )


def _id(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))


class ControlAutomationQueryAdapter:
    def __init__(self, reader: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> None:
        self._reader = reader

    async def list_automation_runs(
        self, request: PageRequest, *, automation_id: int
    ) -> Page[ActivityView]:
        _id(automation_id)
        model = AutomationRunModel
        kind = QueryResourceKind.AUTOMATION_RUN
        scope = str(automation_id)
        key = _key(request, kind, scope)
        stmt = select(
            model.id,
            model.status,
            model.scheduled_for,
            model.actual_started_at.label("started_at"),
            model.finished_at,
            model.llm_calls.label("model_calls"),
            model.tool_calls,
            model.messages_sent.label("sent_messages"),
            model.steps_completed,
            model.error_category,
            model.created_at,
        ).where(model.automation_id == automation_id)
        if key:
            marker = self._marker(key)
            stmt = stmt.where(model.id < marker)
        async with self._reader() as session:
            if (
                await session.scalar(
                    select(AutomationModel.id).where(AutomationModel.id == automation_id)
                )
                is None
            ):
                raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
            rows = (
                (
                    await session.execute(
                        (
                            sql_window := await numbered_statement(
                                session,
                                stmt.order_by(model.id.desc()).limit(request.limit + 1),
                                request,
                                order=(model.created_at.desc(), model.id.desc()),
                            )
                        ).statement
                    )
                )
                .mappings()
                .all()
            )
        items = [
            ActivityView(
                str(row["id"]),
                {
                    name: _stamp(value) if isinstance(value, datetime) else value
                    for name, value in row.items()
                },
            )
            for row in rows[: request.limit]
        ]
        return _page(
            items,
            rows,
            request,
            kind,
            scope,
            str(rows[request.limit - 1]["id"]) if len(rows) >= request.limit else None,
            total=sql_window.total,
            number=request.number,
        )

    async def list_automation_steps(
        self, request: PageRequest, *, automation_id: int, run_id: int | None = None
    ) -> Page[ActivityView]:
        _id(automation_id)
        if run_id is not None:
            _id(run_id)
        model = AutomationStepRunModel
        kind = QueryResourceKind.AUTOMATION_STEP
        scope = f"{automation_id}:{run_id}"
        key = _key(request, kind, scope)
        stmt = (
            select(
                model.id,
                model.run_id,
                model.step_id,
                model.capability,
                model.status,
                model.started_at,
                model.finished_at,
                model.error_category,
            )
            .join(AutomationRunModel, AutomationRunModel.id == model.run_id)
            .where(AutomationRunModel.automation_id == automation_id)
        )
        if run_id is not None:
            stmt = stmt.where(model.run_id == run_id)
        if key:
            stmt = stmt.where(model.id < self._marker(key))
        async with self._reader() as session:
            if (
                await session.scalar(
                    select(AutomationModel.id).where(AutomationModel.id == automation_id)
                )
                is None
            ):
                raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
            if (
                run_id is not None
                and await session.scalar(
                    select(AutomationRunModel.id).where(
                        AutomationRunModel.id == run_id,
                        AutomationRunModel.automation_id == automation_id,
                    )
                )
                is None
            ):
                raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
            rows = (
                (
                    await session.execute(
                        (
                            sql_window := await numbered_statement(
                                session,
                                stmt.order_by(model.id.desc()).limit(request.limit + 1),
                                request,
                                order=(model.started_at.desc(), model.id.desc()),
                            )
                        ).statement
                    )
                )
                .mappings()
                .all()
            )
        items = [
            ActivityView(
                str(row["id"]),
                {
                    name: _stamp(value) if isinstance(value, datetime) else value
                    for name, value in row.items()
                },
            )
            for row in rows[: request.limit]
        ]
        return _page(
            items,
            rows,
            request,
            kind,
            scope,
            str(rows[request.limit - 1]["id"]) if len(rows) >= request.limit else None,
            total=sql_window.total,
            number=request.number,
        )

    @staticmethod
    def _marker(key: str) -> int:
        if not key.isascii() or not key.isdecimal() or len(key) > 19:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        marker = int(key)
        if str(marker) != key or not 1 <= marker <= 2**63 - 1:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        return marker

    async def read_automation(self, automation_id: int) -> ActivityView:
        if type(automation_id) is not int or not 1 <= automation_id <= 2**63 - 1:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        async with self._reader() as session:
            names = (
                "id",
                "name",
                "updated_at",
                "status",
                "creator_kind",
                "canonical_creator_person_id",
                "canonical_target_person_id",
                "canonical_target_space_id",
                "timezone",
                "schedule_json",
                "script_json",
                "script_hash",
                "next_run_at",
                "last_run_at",
                "run_count",
                "consecutive_failures",
            )
            table = AutomationModel.__table__
            row = (
                (
                    await session.execute(
                        select(*[table.c[name] for name in names]).where(
                            table.c.id == automation_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
            # Do not expose authority snapshots, transport IDs or raw result payloads.
            return ActivityView(
                str(row["id"]),
                {
                    "name": row["name"],
                    "revision": state_revision(row["updated_at"]),
                    "status": row["status"],
                    "creator_kind": row["creator_kind"],
                    "creator_person_id": row["canonical_creator_person_id"],
                    "target_person_id": row["canonical_target_person_id"],
                    "target_space_id": row["canonical_target_space_id"],
                    "timezone": row["timezone"],
                    "schedule": json.loads(row["schedule_json"]),
                    "script": json.loads(row["script_json"]),
                    "script_hash": row["script_hash"],
                    "next_run_at": _stamp(row["next_run_at"]),
                    "last_run_at": _stamp(row["last_run_at"]),
                    "run_count": row["run_count"],
                    "consecutive_failures": row["consecutive_failures"],
                },
            )
