"""One Work's bounded diagnostics from original durable rows, never recovery payloads."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import ActivityView, ControlQueryError
from qq_ai_bot.runtime.subagent_schema import budgets, children
from qq_ai_bot.runtime.work_recovery_schema import deliveries, recovery
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, journal, work
from qq_ai_bot.runtime.work_wait_schema import waits

LIMIT = 20


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
            queries = {
                "children": select(
                    work.c.id,
                    work.c.state,
                    work.c.reason,
                    work.c.revision,
                    work.c.model_requests,
                    work.c.tool_calls,
                    work.c.updated,
                )
                .join(children, children.c.work_id == work.c.id)
                .where(children.c.root_id == work_id)
                .order_by(work.c.updated.desc(), work.c.id),
                "inputs": select(
                    inputs.c.id,
                    inputs.c.event_id,
                    inputs.c.kind,
                    inputs.c.state,
                    inputs.c.ready,
                    inputs.c.created,
                )
                .where(inputs.c.work_id == work_id)
                .order_by(inputs.c.id.desc()),
                "effects": select(
                    effects.c.effect_key,
                    effects.c.kind,
                    effects.c.state,
                    effects.c.created,
                    effects.c.updated,
                )
                .where(effects.c.work_id == work_id)
                .order_by(effects.c.updated.desc(), effects.c.effect_key),
                "deliveries": select(
                    deliveries.c.id,
                    deliveries.c.kind,
                    deliveries.c.message_count,
                    deliveries.c.state,
                    deliveries.c.not_before,
                    deliveries.c.created,
                    deliveries.c.updated,
                )
                .where(deliveries.c.work_id == work_id)
                .order_by(deliveries.c.updated.desc(), deliveries.c.id),
            }
            for name, stmt in queries.items():
                rows = (await session.execute(stmt.limit(LIMIT + 1))).mappings().all()
                result[name] = [self._record(item) for item in rows[:LIMIT]]
                result[f"{name}_has_more"] = len(rows) > LIMIT
            wait_columns = [
                waits.c[name]
                for name in ("id", "mode", "status", "deadline", "created", "updated", "delivered")
            ]
            if include_content:
                wait_columns.append(waits.c.conditions_json)
            rows = (
                (
                    await session.execute(
                        select(*wait_columns)
                        .where(waits.c.work_id == work_id)
                        .order_by(waits.c.created.desc(), waits.c.id)
                        .limit(LIMIT + 1)
                    )
                )
                .mappings()
                .all()
            )
            result["waits"] = []
            for item in rows[:LIMIT]:
                shown = self._record(
                    {key: value for key, value in item.items() if key != "conditions_json"}
                )
                if include_content:
                    shown["conditions"] = _conditions(item["conditions_json"])
                result["waits"].append(shown)
            result["waits_has_more"] = len(rows) > LIMIT
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
