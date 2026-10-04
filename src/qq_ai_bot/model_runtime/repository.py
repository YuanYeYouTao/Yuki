"""Persistence for content-free model invocation telemetry."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import exists, func, insert, literal, select

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.correlation import stamp_conversation_correlation
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.execution_trace.payload import PayloadCapacityError, freeze_payload
from qq_ai_bot.execution_trace.recorder import _require_trace_source, current_trace
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.model_runtime.models import ModelInvocationRecord, ModelStats, ModelTask
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.diagnostic_writer import DiagnosticWriter
from qq_ai_bot.runtime.observability import claim_runtime_turn_id


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
                lambda: self._prepare_and_insert(frozen, privacy_generation),
            )
            return None
        async with self._database.sessions() as session:
            await stamp_conversation_correlation(session, row, canonical_conversation_id)
        async with self._database.sessions() as session, session.begin():
            session.add(row)
            await session.flush()
            return self._record(row)

    async def _prepare_and_insert(
        self, frozen: tuple[tuple[str, Any], ...], privacy_generation: int
    ) -> None:
        async with self._database.sessions() as session:
            await _require_trace_source(session, dict(frozen)["canonical_conversation_id"], None)
        await self._insert(frozen, privacy_generation)

    async def _insert(self, frozen: tuple[tuple[str, Any], ...], privacy_generation: int) -> None:
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
        if values["canonical_conversation_id"] is not None:
            guarded = guarded.where(
                exists(
                    select(CanonicalConversationModel.id).where(
                        CanonicalConversationModel.id == values["canonical_conversation_id"]
                    )
                )
            )
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                insert(ModelInvocationModel).from_select(list(values), guarded)
            )
            if getattr(result, "rowcount", 0) == 0:
                # Visible disposable gap; never retry a guarded/unknown commit.
                raise ValueError("model_invocation_privacy_or_source_changed")

    async def stats(self, *, task: ModelTask | None = None) -> ModelStats:
        statement = self._stats_statement()
        if task is not None:
            statement = statement.where(ModelInvocationModel.task == task.value)
        async with self._database.sessions() as session:
            row = (await session.execute(statement)).one()
        return self._stats(row)

    async def stats_by_task(self) -> dict[str, ModelStats]:
        statement = self._stats_statement(ModelInvocationModel.task).group_by(
            ModelInvocationModel.task
        )
        async with self._database.sessions() as session:
            rows = (await session.execute(statement)).all()
        return {project_model_task(str(row[0])): self._stats(row[1:]) for row in rows}

    async def stats_by_profile(self) -> dict[str, ModelStats]:
        statement = self._stats_statement(ModelInvocationModel.profile_id).group_by(
            ModelInvocationModel.profile_id
        )
        async with self._database.sessions() as session:
            rows = (await session.execute(statement)).all()
        return {str(row[0]): self._stats(row[1:]) for row in rows}

    async def recent_errors(self, *, limit: int) -> tuple[ModelInvocationRecord, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        statement = (
            select(ModelInvocationModel)
            .where(ModelInvocationModel.success.is_(False))
            .order_by(ModelInvocationModel.created_at.desc(), ModelInvocationModel.id.desc())
            .limit(limit)
        )
        async with self._database.sessions() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(self._record(row) for row in rows)

    @staticmethod
    def _stats_statement(group_column: Any | None = None) -> Any:
        columns: list[Any] = []
        if group_column is not None:
            columns.append(group_column)
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
                func.coalesce(func.avg(ModelInvocationModel.latency_seconds), 0.0),
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
            average_latency_seconds=float(values[10] or 0),
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
