"""Persistence for content-free model invocation telemetry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import case, func, insert, literal, or_, select

from yuki.conversation.correlation import stamp_conversation_correlation
from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.execution_trace.payload import PayloadCapacityError, freeze_payload
from yuki.execution_trace.recorder import _trace_source_guards, current_trace
from yuki.model_runtime.db_models import ModelInvocationModel
from yuki.model_runtime.models import ModelInvocationRecord, ModelStats, ModelTask
from yuki.model_runtime.usage import (
    ModelCacheRead,
    ModelStatsView,
    ModelUsageCoverage,
    ModelUsageFilters,
    ModelUsageGroup,
    ModelUsageModel,
    ModelUsageOptions,
    ModelUsageQuery,
    ModelUsageSource,
    ModelUsageStats,
)
from yuki.persistence.database import Database
from yuki.persistence.diagnostic_writer import DiagnosticWrite, DiagnosticWriter
from yuki.runtime.observability import claim_runtime_turn_id

_TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_prompt_tokens",
    "cache_creation_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
)


def project_model_task(value: str) -> str:
    """Project a persisted task string without requiring a live ``ModelTask``.

    Historical ``planner`` / ``tool_selection`` rows stay readable after those
    enum members are deleted. The separate search bridge records web_search.
    """

    return str(value)


class ModelInvocationRepository:
    """Store and aggregate only task/profile/usage/latency metadata."""

    def __init__(self, database: Database, *, writer: DiagnosticWriter | None = None) -> None:
        self._database = database
        self.writer = writer

    async def record(
        self,
        *,
        task: ModelTask | Literal["web_search"],
        profile_id: str,
        provider: str,
        model: str,
        success: bool,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        cached_prompt_tokens: int | None,
        latency_seconds: float,
        error_category: str | None,
        cache_creation_input_tokens: int | None = None,
        cache_creation_5m_input_tokens: int | None = None,
        cache_creation_1h_input_tokens: int | None = None,
        physical_request_count: int | None = None,
        unknown_usage_request_count: int | None = None,
        native_search_requested: bool | None = None,
        canonical_conversation_id: str | None = None,
    ) -> ModelInvocationRecord | None:
        row = ModelInvocationModel(
            runtime_turn_id=claim_runtime_turn_id(),
            task=task.value if isinstance(task, ModelTask) else task,
            profile_id=profile_id,
            provider=provider,
            model=model,
            success=success,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_creation_5m_input_tokens=cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens=cache_creation_1h_input_tokens,
            physical_request_count=physical_request_count,
            unknown_usage_request_count=unknown_usage_request_count,
            native_search_requested=native_search_requested,
            latency_seconds=latency_seconds,
            error_category=error_category,
            created_at=datetime.now(UTC),
        )
        if self.writer is not None:
            scope = current_trace.get()
            # Missing original privacy provenance is a coverage gap, never a
            # reason to recapture today's generation after the model returned.
            capacity = self.writer.capacity()
            if scope is None or scope.coverage.privacy_generation is None or not capacity:
                self.writer.drop("model_invocation")
                return None
            row.canonical_conversation_id = (
                str(canonical_conversation_id).strip() or None
                if canonical_conversation_id is not None
                else None
            )
            values = {
                column.name: getattr(row, column.name)
                for column in row.__table__.columns
                if column.name != "id"
            }
            try:
                snapshot, reserved = freeze_payload(
                    {key: value for key, value in values.items() if key != "created_at"}, capacity
                )
                assert isinstance(snapshot, dict)
                snapshot["created_at"] = values["created_at"]
            except PayloadCapacityError:
                self.writer.drop("model_invocation")
                return None
            frozen = tuple(snapshot.items())
            privacy_generation = scope.coverage.privacy_generation
            self.writer.submit(
                "model_invocation",
                reserved,
                lambda: self._prepare_write(frozen, privacy_generation),
            )
            return None
        async with self._database.read_session() as session:
            await stamp_conversation_correlation(session, row, canonical_conversation_id)
        async with self._database.immediate_session() as session:
            session.add(row)
            await session.flush()
            return self._record(row)

    async def _prepare_write(
        self, frozen: tuple[tuple[str, Any], ...], privacy_generation: int
    ) -> DiagnosticWrite:
        values = dict(frozen)
        table = ModelInvocationModel.__table__
        current_generation = (
            select(ExecutionTraceStateModel.privacy_generation)
            .where(ExecutionTraceStateModel.id == 1)
            .scalar_subquery()
        )
        guarded = select(
            *(literal(value, type_=table.c[key].type) for key, value in values.items())
        ).where(func.coalesce(current_generation, 0) == privacy_generation)
        guarded = guarded.where(*_trace_source_guards(values["canonical_conversation_id"], None))
        return DiagnosticWrite(
            self._database, insert(ModelInvocationModel).from_select(list(values), guarded)
        )

    async def stats(self, *, task: ModelTask | None = None) -> ModelStats:
        statement = self._stats_statement()
        if task is not None:
            statement = statement.where(ModelInvocationModel.task == task.value)
        async with self._database.read_session() as session:
            row = (await session.execute(statement)).one()
        return self._stats(row)

    async def usage(self, query: ModelUsageQuery) -> ModelStatsView:
        window = query.window()
        model = ModelInvocationModel
        dimensions = (model.task, model.provider, model.model, model.profile_id)
        paired = model.prompt_tokens.is_not(None) & model.cached_prompt_tokens.is_not(None)
        statement = (
            select(
                *dimensions,
                func.count(model.id).label("invocations"),
                func.sum(func.cast(model.success, model.id.type)).label("successes"),
                *(func.sum(getattr(model, name)).label(name) for name in _TOKEN_FIELDS),
                func.sum(model.latency_seconds).label("latency_sum"),
                func.sum(
                    func.cast(
                        or_(model.total_tokens.is_(None), model.unknown_usage_request_count > 0),
                        model.id.type,
                    )
                ).label("unknown_usage"),
                *(
                    func.count(getattr(model, name)).label(f"reported_{name}")
                    for name in _TOKEN_FIELDS
                ),
                func.sum(func.cast(paired, model.id.type)).label("paired_calls"),
                func.sum(case((paired, model.prompt_tokens), else_=0)).label("paired_input"),
                func.sum(case((paired, model.cached_prompt_tokens), else_=0)).label("paired_cache"),
            )
            .where(model.created_at < window.end)
            .group_by(*dimensions)
        )
        if window.start is not None:
            statement = statement.where(model.created_at >= window.start)
        async with self._database.read_session() as reader:
            rows = [dict(row) for row in (await reader.execute(statement)).mappings()]
        filters = ModelUsageFilters(
            task=query.task, provider=query.provider, model=query.model, profile_id=query.profile_id
        )
        selected_filters = filters.model_dump(exclude_none=True)
        selected = [
            row for row in rows if all(row[key] == value for key, value in selected_filters.items())
        ]
        grouped: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
        for row in selected:
            key = (row["task"],) if query.group_by == "task" else (row["provider"], row["model"])
            grouped.setdefault(key, []).append(row)
        groups = [
            ModelUsageGroup(
                task=key[0] if query.group_by == "task" else None,
                provider=key[0] if query.group_by == "model" else None,
                model=key[1] if query.group_by == "model" else None,
                stats=self._usage_stats(members),
                sources=[
                    ModelUsageSource(
                        task=row["task"],
                        provider=row["provider"],
                        model=row["model"],
                        profile_id=row["profile_id"],
                        stats=self._usage_stats([row]),
                    )
                    for row in sorted(
                        members, key=lambda row: tuple(row[column.key] for column in dimensions)
                    )
                ],
            )
            for key, members in sorted(grouped.items())
        ]
        groups.sort(key=lambda group: group.stats.total_tokens, reverse=True)
        return ModelStatsView(
            range=window,
            group_by=query.group_by,
            filters=filters,
            options=ModelUsageOptions(
                tasks=sorted({row["task"] for row in rows}),
                models=[
                    ModelUsageModel(provider=provider, model=name)
                    for provider, name in sorted({(row["provider"], row["model"]) for row in rows})
                ],
            ),
            summary=self._usage_stats(selected),
            groups=groups,
        )

    @staticmethod
    def _usage_stats(rows: Sequence[Mapping[str, Any]]) -> ModelUsageStats:
        def total(name: str) -> Any:
            return sum(row[name] or 0 for row in rows)

        calls, successes = total("invocations"), total("successes")
        paired_input, paired_cache = total("paired_input"), total("paired_cache")
        return ModelUsageStats(
            invocations=calls,
            successes=successes,
            failures=calls - successes,
            **{name: total(name) for name in _TOKEN_FIELDS},
            unknown_usage=total("unknown_usage"),
            average_latency_seconds=total("latency_sum") / calls if calls else 0,
            coverage=ModelUsageCoverage(
                **{name: total(f"reported_{name}") for name in _TOKEN_FIELDS}
            ),
            cache_read=ModelCacheRead(
                reported_calls=total("paired_calls"),
                prompt_tokens=paired_input,
                cached_prompt_tokens=paired_cache,
                ratio=paired_cache / paired_input if paired_input else None,
            ),
        )

    async def stats_summary(
        self,
    ) -> tuple[ModelStats, dict[str, ModelStats], dict[str, ModelStats]]:
        """Aggregate one snapshot, then sum its task/profile groups."""
        statement = self._stats_statement(
            ModelInvocationModel.task, ModelInvocationModel.profile_id
        ).group_by(ModelInvocationModel.task, ModelInvocationModel.profile_id)
        async with self._database.read_session() as session:
            rows = (await session.execute(statement)).all()
        by_task: dict[str, list[Sequence[Any]]] = {}
        by_profile: dict[str, list[Sequence[Any]]] = {}
        for row in rows:
            values = row[2:]
            by_task.setdefault(project_model_task(str(row[0])), []).append(values)
            by_profile.setdefault(str(row[1]), []).append(values)

        def combined(groups: Sequence[Sequence[Any]]) -> ModelStats:
            return self._stats([sum(row[index] or 0 for row in groups) for index in range(12)])

        return (
            combined([row[2:] for row in rows]),
            {key: combined(groups) for key, groups in by_task.items()},
            {key: combined(groups) for key, groups in by_profile.items()},
        )

    async def recent_errors(self, *, limit: int) -> tuple[ModelInvocationRecord, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        statement = (
            select(ModelInvocationModel)
            .where(ModelInvocationModel.success.is_(False))
            .order_by(ModelInvocationModel.created_at.desc(), ModelInvocationModel.id.desc())
            .limit(limit)
        )
        async with self._database.read_session() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(self._record(row) for row in rows)

    @staticmethod
    def _stats_statement(*group_columns: Any) -> Any:
        columns: list[Any] = list(group_columns)
        columns.extend(
            (
                func.count(ModelInvocationModel.id),
                func.sum(
                    func.cast(ModelInvocationModel.success, type_=ModelInvocationModel.id.type)
                ),
                func.coalesce(func.sum(ModelInvocationModel.prompt_tokens), 0),
                func.coalesce(func.sum(ModelInvocationModel.completion_tokens), 0),
                func.coalesce(func.sum(ModelInvocationModel.total_tokens), 0),
                func.coalesce(func.sum(ModelInvocationModel.cached_prompt_tokens), 0),
                func.coalesce(func.sum(ModelInvocationModel.cache_creation_input_tokens), 0),
                func.coalesce(func.sum(ModelInvocationModel.cache_creation_5m_input_tokens), 0),
                func.coalesce(func.sum(ModelInvocationModel.cache_creation_1h_input_tokens), 0),
                func.sum(
                    func.cast(
                        ModelInvocationModel.total_tokens.is_(None),
                        type_=ModelInvocationModel.id.type,
                    )
                ),
                func.coalesce(func.sum(ModelInvocationModel.latency_seconds), 0.0),
                func.count(ModelInvocationModel.latency_seconds),
            )
        )
        return select(*columns)

    @staticmethod
    def _stats(row: Sequence[Any]) -> ModelStats:
        values = tuple(row)  # SQLAlchemy Row and sliced Row are both tuple-compatible.
        invocations = int(values[0] or 0)
        successes = int(values[1] or 0)
        return ModelStats(
            invocations=invocations,
            successes=successes,
            failures=invocations - successes,
            prompt_tokens=int(values[2] or 0),
            completion_tokens=int(values[3] or 0),
            total_tokens=int(values[4] or 0),
            cached_prompt_tokens=int(values[5] or 0),
            cache_creation_input_tokens=int(values[6] or 0),
            cache_creation_5m_input_tokens=int(values[7] or 0),
            cache_creation_1h_input_tokens=int(values[8] or 0),
            unknown_usage=int(values[9] or 0),
            average_latency_seconds=float(values[10] or 0) / int(values[11]) if values[11] else 0,
        )

    @staticmethod
    def _record(row: ModelInvocationModel) -> ModelInvocationRecord:
        return ModelInvocationRecord(
            id=row.id,
            task=project_model_task(row.task),
            profile_id=row.profile_id,
            provider=row.provider,
            model=row.model,
            success=row.success,
            prompt_tokens=row.prompt_tokens,
            completion_tokens=row.completion_tokens,
            total_tokens=row.total_tokens,
            cached_prompt_tokens=row.cached_prompt_tokens,
            cache_creation_input_tokens=row.cache_creation_input_tokens,
            cache_creation_5m_input_tokens=row.cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens=row.cache_creation_1h_input_tokens,
            physical_request_count=row.physical_request_count,
            unknown_usage_request_count=row.unknown_usage_request_count,
            native_search_requested=row.native_search_requested,
            latency_seconds=row.latency_seconds,
            error_category=row.error_category,
            created_at=row.created_at,
        )
