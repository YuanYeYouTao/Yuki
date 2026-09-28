"""One Work's bounded diagnostics from original durable rows, never recovery payloads."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.control_plane.paging import Cursor, Page, PageRequest
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import ActivityView, ControlQueryError
from qq_ai_bot.persistence.control_paging import numbered_statement
from qq_ai_bot.runtime.subagent_schema import budgets, children
from qq_ai_bot.runtime.work_recovery_schema import deliveries, recovery
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, journal, work
from qq_ai_bot.runtime.work_wait_schema import waits


def _stamp(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def _conditions(text: str) -> list[dict[str, Any]]:
    # The subscription has its own 8 KiB bound; reflect reviewed fields only.
    if len(text.encode("utf-8")) > 8192:
        raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
    try:
        value = json.loads(text)
        if not isinstance(value, list) or len(value) > 8:
            raise ValueError("invalid conditions")
        result = []
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("invalid condition")
            selected = {
                key: item.get(key)
                for key in ("kind", "due", "run_id", "plugin_id", "event_type")
                if key in item
            }
            selected["matched"] = item.get("matched") is not None
            if "due" in selected:
                selected["due"] = _stamp(selected["due"])
            result.append(selected)
        return result
    except (TypeError, ValueError) as exc:
        raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from exc


class ControlWorkQueryAdapter:
    def __init__(self, reader: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> None:
        self._reader = reader

    async def list_work_history(
        self,
        request: PageRequest,
        *,
        work_id: str,
        section: str,
        include_content: bool = False,
    ) -> Page[ActivityView]:
        from qq_ai_bot.runtime.work_management import WorkManagementError, require_work_id

        try:
            require_work_id(work_id)
            if (
                type(request) is not PageRequest
                or type(include_content) is not bool
                or type(section) is not str
            ):
                raise ValueError("invalid history")
            table, key, names = {
                "children": (
                    work,
                    "id",
                    (
                        "id",
                        "state",
                        "reason",
                        "revision",
                        "model_requests",
                        "tool_calls",
                        "created",
                        "updated",
                    ),
                ),
                "inputs": (inputs, "id", ("id", "event_id", "kind", "state", "ready", "created")),
                "effects": (
                    effects,
                    "effect_key",
                    ("effect_key", "kind", "state", "created", "updated"),
                ),
                "deliveries": (
                    deliveries,
                    "id",
                    ("id", "kind", "message_count", "state", "not_before", "created", "updated"),
                ),
                "waits": (
                    waits,
                    "id",
                    ("id", "mode", "status", "deadline", "created", "updated", "delivered"),
                ),
            }[section]
            columns = [table.c[name] for name in names]
            if section == "waits" and include_content:
                columns.append(waits.c.conditions_json)
            stmt = select(*columns)
            if section == "children":
                stmt = stmt.join(children, children.c.work_id == work.c.id).where(
                    children.c.root_id == work_id
                )
            else:
                stmt = stmt.where(table.c.work_id == work_id)
            prefix = f"work-history|{work_id}|{section}|{int(include_content)}|"
            if request.cursor:
                if not request.cursor.value.startswith(prefix):
                    raise ValueError("cursor scope mismatch")
                stamp, separator, last = request.cursor.value[len(prefix) :].partition("|")
                created = float.fromhex(stamp)
                if not separator or not math.isfinite(created) or len(last) > 256 or not last:
                    raise ValueError("invalid cursor")
                if section == "inputs":
                    marker: int | str
                    marker = int(last)
                    if str(marker) != last or not 1 <= marker <= 2**63 - 1:
                        raise ValueError("invalid input cursor")
                else:
                    marker = last
                stmt = stmt.where(
                    or_(
                        table.c.created < created,
                        and_(table.c.created == created, table.c[key] < marker),
                    )
                )
            stmt = stmt.order_by(table.c.created.desc(), table.c[key].desc()).limit(
                request.limit + 1
            )
        except (KeyError, TypeError, ValueError, OverflowError, WorkManagementError) as exc:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
        async with self._reader() as session:
            if await session.scalar(select(work.c.id).where(work.c.id == work_id)) is None:
                raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
            rows = (
                (
                    await session.execute(
                        (
                            sql_window := await numbered_statement(
                                session,
                                stmt,
                                request,
                                order=(table.c.created.desc(), table.c[key].desc()),
                            )
                        ).statement
                    )
                )
                .mappings()
                .all()
            )
        shown = []
        for row in rows[: request.limit]:
            fields = self._record(
                {name: value for name, value in row.items() if name != "conditions_json"}
            )
            if "conditions_json" in row:
                fields["conditions"] = _conditions(row["conditions_json"])
            shown.append(ActivityView(str(row[key]), fields))
        cursor = None
        if len(rows) > request.limit:
            last_row = rows[request.limit - 1]
            cursor = Cursor(prefix + float(last_row["created"]).hex() + "|" + str(last_row[key]))
        return Page(shown, cursor, datetime.now(UTC), total=sql_window.total, number=request.number)

    async def read_work(self, work_id: str, *, include_content: bool = False) -> ActivityView:
        if type(work_id) is not str or type(include_content) is not bool:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        try:
            if str(UUID(work_id)) != work_id or UUID(work_id).version != 4:
                raise ValueError("invalid work id")
        except ValueError as exc:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
        base_names = (
            "id",
            "conversation_id",
            "generation",
            "revision",
            "state",
            "reason",
            "output_kind",
            "deliver_artifacts",
            "model_requests",
            "tool_calls",
            "active_seconds",
            "sent_messages",
            "created",
            "updated",
        )
        columns = [work.c[name] for name in base_names]
        if include_content:
            columns.append(work.c.goal)
        async with self._reader() as session:
            row = (
                (await session.execute(select(*columns).where(work.c.id == work_id)))
                .mappings()
                .first()
            )
            if row is None:
                raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
            result: dict[str, Any] = {name: row[name] for name in base_names if name != "id"}
            for name in ("created", "updated"):
                result[name] = _stamp(row[name])
            if include_content:
                result["goal"] = row["goal"]
            lineage = (
                (
                    await session.execute(
                        select(children.c.root_id, children.c.archived_at).where(
                            children.c.work_id == work_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            root_id = lineage["root_id"] if lineage else work_id
            result["root_id"] = root_id
            result["archived_at"] = _stamp(lineage["archived_at"]) if lineage else None
            budget = (
                (
                    await session.execute(
                        select(
                            budgets.c.models,
                            budgets.c.tools,
                            budgets.c.model_limit,
                            budgets.c.tool_limit,
                        ).where(budgets.c.root_id == root_id)
                    )
                )
                .mappings()
                .first()
            )
            result["shared_budget"] = self._record(budget) if budget else None
            checkpoint = (
                (
                    await session.execute(
                        select(
                            journal.c.chain_id,
                            journal.c.contract,
                            journal.c.source_revision,
                            journal.c.phase,
                            journal.c.updated,
                        ).where(journal.c.work_id == work_id)
                    )
                )
                .mappings()
                .first()
            )
            result["journal"] = self._record(checkpoint) if checkpoint else None
            recovered = (
                (
                    await session.execute(
                        select(
                            recovery.c.activation_id,
                            recovery.c.exit_reason,
                            recovery.c.stage,
                            recovery.c.attempts,
                            recovery.c.not_before,
                            recovery.c.updated,
                        ).where(recovery.c.work_id == work_id)
                    )
                )
                .mappings()
                .first()
            )
            result["recovery"] = self._record(recovered) if recovered else None
            return ActivityView(work_id, result)

    @staticmethod
    def _record(row: Mapping[Any, Any]) -> dict[str, Any]:
        result = {str(key): value for key, value in row.items()}
        for name in ("created", "updated", "not_before", "deadline", "delivered"):
            if name in result:
                result[name] = (
                    None if name == "not_before" and result[name] == 0 else _stamp(result[name])
                )
        return result
