"""Reviewed read-only Memory projections; queries never reinforce or mutate facts."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.control_plane.paging import Page, PageRequest
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import (
    ActivityView,
    ControlQueryError,
    MemoryEvidenceView,
    MemoryFactView,
    MemoryQueryFilter,
    QueryResourceKind,
)
from qq_ai_bot.domain.identity import PersonId, RequestId
from qq_ai_bot.domain.relationships import stage_for_score
from qq_ai_bot.memory.dream.db_models import MemoryDreamRunModel
from qq_ai_bot.persistence.control_activity_query import _stamp
from qq_ai_bot.persistence.control_execution_query import _key, _page
from qq_ai_bot.persistence.models import (
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryRebuildProposalModel,
    MemoryRebuildRunModel,
    MemoryToolReceiptModel,
    PersonRelationshipModel,
    RelationshipEventModel,
    RelationshipJobModel,
)
from qq_ai_bot.persistence.unit_of_work import state_revision

_FACT_NAMES = (
    "id",
    "scope_type",
    "kind",
    "category",
    "status",
    "updated_at",
    "canonical_subject_person_id",
    "canonical_subject_space_id",
    "visibility_type",
    "canonical_visibility_person_id",
    "canonical_visibility_space_id",
    "review_state",
    "importance",
    "confidence",
)
_DETAIL_NAMES = (
    "source_type",
    "authority",
    "conflict_state",
    "supersedes_id",
    "valid_from",
    "valid_until",
    "created_at",
    "last_confirmed_at",
    "last_injected_at",
    "validation_version",
    "last_audited_at",
    "invalidated_reason",
)


def _id(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    return value


def _partition(scope: MemoryQueryFilter, content: bool) -> str:
    if type(scope) is not MemoryQueryFilter:
        raise TypeError("scope must be MemoryQueryFilter")
    return json.dumps(
        [
            scope.scope_type,
            scope.person_id.text if scope.person_id else None,
            scope.space_id.text if scope.space_id else None,
            scope.visibility_type,
            scope.visibility_person_id.text if scope.visibility_person_id else None,
            scope.visibility_space_id.text if scope.visibility_space_id else None,
            scope.kind,
            scope.status,
            scope.review_state,
            scope.fact_id,
            scope.event_id,
            scope.tool_receipt_id,
            content,
        ]
    )


def _filter[T: tuple[object, ...]](stmt: Select[T], scope: MemoryQueryFilter) -> Select[T]:
    model = MemoryFactModel
    for column, value in (
        (model.scope_type, scope.scope_type),
        (model.canonical_subject_person_id, scope.person_id.text if scope.person_id else None),
        (model.canonical_subject_space_id, scope.space_id.text if scope.space_id else None),
        (model.visibility_type, scope.visibility_type),
        (
            model.canonical_visibility_person_id,
            scope.visibility_person_id.text if scope.visibility_person_id else None,
        ),
        (
            model.canonical_visibility_space_id,
            scope.visibility_space_id.text if scope.visibility_space_id else None,
        ),
        (model.kind, scope.kind),
        (model.status, scope.status),
        (model.review_state, scope.review_state),
        (model.id, scope.fact_id),
    ):
        if value is not None:
            stmt = stmt.where(column == value)
    return stmt


class ControlMemoryQueryAdapter:
    async def read_memory_maintenance_run(self, operation_id: str) -> ActivityView:
        if type(operation_id) is not str:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        kind, _, raw = operation_id.partition(":")
        try:
            public_id = RequestId.parse(raw).text
        except (TypeError, ValueError) as exc:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
        if kind not in {"rebuild", "dream"}:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        model = MemoryRebuildRunModel if kind == "rebuild" else MemoryDreamRunModel
        statistics_name = "plan_statistics_json" if kind == "rebuild" else "statistics_json"
        names = [
            "id",
            "public_id",
            "status",
            "created_at",
            "updated_at",
            "started_at",
            "completed_at",
            "cancelled_at",
            "error_category",
            statistics_name,
        ]
        names.extend(
            [
                "snapshot_max_event_id",
                "scan_checkpoint_event_id",
                "commit_checkpoint_event_id",
                "extraction_requests",
                "consolidation_requests",
                "input_tokens",
                "output_tokens",
            ]
            if kind == "rebuild"
            else ["snapshot_max_fact_id", "model_calls", "completed_clusters", "failed_clusters"]
        )
        async with self._reader() as session:
            row = (
                (
                    await session.execute(
                        select(*(getattr(model, name) for name in names)).where(
                            model.public_id == public_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
            fields = {
                key: _stamp(value) if isinstance(value, datetime) else value
                for key, value in row.items()
                if key not in {"id", statistics_name}
            }
            from pydantic import ValidationError

            from qq_ai_bot.memory.dream.models import DreamPlanStatistics
            from qq_ai_bot.memory.rebuild.models import MemoryRebuildPlanStatistics

            raw_statistics = row[statistics_name]
            if isinstance(raw_statistics, str) and len(raw_statistics.encode("utf-8")) <= 65536:
                try:
                    schema = (
                        MemoryRebuildPlanStatistics if kind == "rebuild" else DreamPlanStatistics
                    )
                    fields["plan_statistics"] = schema.model_validate_json(
                        raw_statistics
                    ).model_dump(mode="json")
                except ValidationError:
                    fields["plan_statistics_error"] = "invalid_original_statistics"
            else:
                fields["plan_statistics_error"] = "original_statistics_unavailable"
            fields["revision"] = state_revision(row["updated_at"])
            fields["kind"] = kind
            if kind == "rebuild":
                counts = (
                    await session.execute(
                        select(MemoryRebuildProposalModel.review_status, func.count())
                        .where(MemoryRebuildProposalModel.run_id == row["id"])
                        .group_by(MemoryRebuildProposalModel.review_status)
                    )
                ).all()
                fields["review_counts"] = {status: count for status, count in counts}
        return ActivityView(operation_id, fields)

    async def list_rebuild_proposals(
        self, request: PageRequest, *, run_id: str, include_content: bool = False
    ) -> Page[ActivityView]:
        from qq_ai_bot.memory.extraction import MemoryClaim

        public = RequestId.parse(run_id).text
        partition = f"{public}:{int(include_content)}"
        kind = QueryResourceKind.MEMORY_REBUILD_PROPOSAL
        key = _key(request, kind, partition)
        model = MemoryRebuildProposalModel
        names = (
            "id",
            "event_id",
            "claim_index",
            "scope_type",
            "operation",
            "kind",
            "authority",
            "confidence",
            "review_status",
            "commit_status",
            "actual_fact_id",
            "actual_action",
            "actual_reason_code",
            "attempts",
            "next_attempt_at",
            "error_category",
            "updated_at",
            "reviewed_at",
            "committed_at",
        )
        stmt = (
            select(
                *(getattr(model, name) for name in names),
                *([model.claim_json] if include_content else []),
            )
            .join(MemoryRebuildRunModel, MemoryRebuildRunModel.id == model.run_id)
            .where(MemoryRebuildRunModel.public_id == public)
        )
        if key:
            from qq_ai_bot.persistence.control_activity_query import _integer_key

            stmt = stmt.where(model.id > _integer_key(key))
        async with self._reader() as session:
            if (
                await session.scalar(
                    select(MemoryRebuildRunModel.id).where(
                        MemoryRebuildRunModel.public_id == public
                    )
                )
                is None
            ):
                raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
            rows = (
                (await session.execute(stmt.order_by(model.id).limit(request.limit + 1)))
                .mappings()
                .all()
            )
        result = []
        for row in rows[: request.limit]:
            fields = {
                name: _stamp(row[name]) if isinstance(row[name], datetime) else row[name]
                for name in names
            }
            fields["revision"] = state_revision(row["updated_at"])
            fields["content_visible"] = include_content
            if include_content:
                if len(row["claim_json"].encode()) > 65536:
                    raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
                claim = MemoryClaim.model_validate_json(row["claim_json"])
                fields.update(
                    memory_key=claim.memory_key,
                    content=claim.content,
                    evidence_quote=claim.evidence_quote,
                    valid_from=claim.valid_from,
                    valid_until=claim.valid_until,
                )
            result.append(ActivityView(str(row["id"]), fields))
        return _page(
            result, rows, request, kind, partition, result[-1].resource_id if result else None
        )

    def __init__(self, reader: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> None:
        self._reader = reader

    async def list_relationships(self, request: PageRequest) -> Page[ActivityView]:
        kind = QueryResourceKind.RELATIONSHIP
        key = _key(request, kind, "canonical_relationships")
        model = PersonRelationshipModel
        stmt = select(
            model.canonical_person_id,
            model.affection_score,
            model.trust_score,
            model.updated_at,
            model.last_automatic_change_at,
        )
        if key is not None:
            try:
                after = PersonId.parse(key).text
            except (ValueError, TypeError) as exc:
                raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
            stmt = stmt.where(model.canonical_person_id > after)
        async with self._reader() as session:
            rows = (
                (
                    await session.execute(
                        stmt.order_by(model.canonical_person_id).limit(request.limit + 1)
                    )
                )
                .mappings()
                .all()
            )
        items = [self._relationship(row) for row in rows[: request.limit]]
        return _page(
            items,
            rows,
            request,
            kind,
            "canonical_relationships",
            rows[request.limit - 1]["canonical_person_id"] if len(rows) >= request.limit else None,
        )

    @staticmethod
    def _relationship(row: RowMapping) -> ActivityView:
        # SQL RowMapping stays local to this adapter; never return the ORM row.
        fields = dict(row)
        fields["person_id"] = fields.pop("canonical_person_id")
        fields["stage"] = stage_for_score(fields["affection_score"]).value
        fields["revision"] = state_revision(fields["updated_at"])
        for name in ("updated_at", "last_automatic_change_at"):
            fields[name] = _stamp(fields[name])
        return ActivityView(fields["person_id"], fields)

    async def read_relationship(self, person_id: PersonId) -> ActivityView:
        if type(person_id) is not PersonId:
            raise TypeError("person_id must be PersonId")
        model = PersonRelationshipModel
        async with self._reader() as session:
            row = (
                (
                    await session.execute(
                        select(
                            model.canonical_person_id,
                            model.affection_score,
                            model.trust_score,
                            model.updated_at,
                            model.last_automatic_change_at,
                        ).where(model.canonical_person_id == person_id.text)
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
        return self._relationship(row)

    async def list_relationship_history(
        self, request: PageRequest, *, person_id: PersonId, section: str
    ) -> Page[ActivityView]:
        if type(person_id) is not PersonId or section not in {"events", "jobs"}:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        kind = (
            QueryResourceKind.RELATIONSHIP_EVENT
            if section == "events"
            else QueryResourceKind.RELATIONSHIP_JOB
        )
        scope = person_id.text
        key = _key(request, kind, scope)
        model = RelationshipEventModel if section == "events" else RelationshipJobModel
        names = (
            (
                "id",
                "source_event_id",
                "change_type",
                "affection_before",
                "affection_delta",
                "affection_after",
                "trust_before",
                "trust_delta",
                "trust_after",
                "reason_code",
                "confidence",
                "created_at",
            )
            if section == "events"
            else (
                "id",
                "trigger_event_id",
                "status",
                "attempts",
                "next_attempt_at",
                "error_category",
                "created_at",
                "updated_at",
            )
        )
        stmt = select(*(getattr(model, name) for name in names)).where(
            model.canonical_person_id == person_id.text
        )
        if key is not None:
            stmt = stmt.where(model.id < self._marker(key))
        async with self._reader() as session:
            rows = (
                (await session.execute(stmt.order_by(model.id.desc()).limit(request.limit + 1)))
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
        )

    async def list_memory_facts(
        self,
        request: PageRequest,
        *,
        include_content: bool,
        scope: MemoryQueryFilter | None = None,
    ) -> Page[MemoryFactView]:
        scope = scope or MemoryQueryFilter()
        partition = _partition(scope, include_content)
        key = _key(request, QueryResourceKind.MEMORY_FACT, partition)
        names = _FACT_NAMES + (("content",) if include_content else ())
        model, evidence = MemoryFactModel, MemoryEvidenceModel
        stmt = _filter(select(*(getattr(model, name) for name in names)), scope)
        if scope.event_id is not None or scope.tool_receipt_id is not None:
            source = select(evidence.id).where(evidence.fact_id == model.id)
            if scope.event_id is not None:
                source = source.where(evidence.event_id == scope.event_id)
            else:
                source = source.where(evidence.tool_receipt_id == scope.tool_receipt_id)
            stmt = stmt.where(source.exists())
        if key is not None:
            stmt = stmt.where(model.id > self._marker(key))
        async with self._reader() as session:
            rows = (
                (await session.execute(stmt.order_by(model.id).limit(request.limit + 1)))
                .mappings()
                .all()
            )
        items = [
            MemoryFactView(
                fact_id=row["id"],
                revision=state_revision(row["updated_at"]),
                scope_type=row["scope_type"],
                kind=row["kind"],
                category=row["category"] or "uncategorized",
                status=row["status"],
                content=row.get("content"),
                excerpt=row["content"][:120] if include_content else None,
                person_id=row["canonical_subject_person_id"],
                space_id=row["canonical_subject_space_id"],
                visibility_type=row["visibility_type"],
                visibility_person_id=row["canonical_visibility_person_id"],
                visibility_space_id=row["canonical_visibility_space_id"],
                review_state=row["review_state"],
                importance=row["importance"],
                confidence=row["confidence"],
                updated_at=_stamp(row["updated_at"]),
            )
            for row in rows[: request.limit]
        ]
        return _page(
            items,
            rows,
            request,
            QueryResourceKind.MEMORY_FACT,
            partition,
            str(rows[request.limit - 1]["id"]) if len(rows) >= request.limit else None,
        )

    async def list_memory_evidence(
        self,
        request: PageRequest,
        *,
        include_content: bool,
        scope: MemoryQueryFilter | None = None,
    ) -> Page[MemoryEvidenceView]:
        scope = scope or MemoryQueryFilter()
        partition = _partition(scope, include_content)
        key = _key(request, QueryResourceKind.MEMORY_EVIDENCE, partition)
        model = MemoryEvidenceModel
        names = (
            "id",
            "fact_id",
            "relation",
            "event_id",
            "tool_receipt_id",
            "authority",
            "confidence",
            "created_at",
        ) + (("excerpt",) if include_content else ())
        stmt = _filter(
            select(*(getattr(model, name) for name in names), MemoryToolReceiptModel.execution_id)
            .join(MemoryFactModel, MemoryFactModel.id == model.fact_id)
            .outerjoin(MemoryToolReceiptModel, MemoryToolReceiptModel.id == model.tool_receipt_id),
            scope,
        )
        if scope.event_id is not None:
            stmt = stmt.where(model.event_id == scope.event_id)
        if scope.tool_receipt_id is not None:
            stmt = stmt.where(model.tool_receipt_id == scope.tool_receipt_id)
        if key is not None:
            stmt = stmt.where(model.id > self._marker(key))
        async with self._reader() as session:
            rows = (
                (await session.execute(stmt.order_by(model.id).limit(request.limit + 1)))
                .mappings()
                .all()
            )
        items = [
            MemoryEvidenceView(
                evidence_id=row["id"],
                fact_id=row["fact_id"],
                relation=row["relation"],
                excerpt=row.get("excerpt"),
                event_id=row["event_id"],
                tool_receipt_id=row["tool_receipt_id"],
                authority=row["authority"],
                confidence=row["confidence"],
                created_at=_stamp(row["created_at"]),
                execution_id=row["execution_id"],
            )
            for row in rows[: request.limit]
        ]
        return _page(
            items,
            rows,
            request,
            QueryResourceKind.MEMORY_EVIDENCE,
            partition,
            str(rows[request.limit - 1]["id"]) if len(rows) >= request.limit else None,
        )

    @staticmethod
    def _marker(key: str) -> int:
        if not key.isascii() or not key.isdecimal() or len(key) > 19:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        result = _id(int(key))
        if str(result) != key:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        return result

    async def read_memory_fact(self, fact_id: int, *, include_content: bool) -> ActivityView:
        _id(fact_id)
        model = MemoryFactModel
        names = _FACT_NAMES + _DETAIL_NAMES + (("content", "memory_key") if include_content else ())
        async with self._reader() as session:
            row = (
                (
                    await session.execute(
                        select(*(getattr(model, name) for name in names)).where(model.id == fact_id)
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
        fields = {
            name: _stamp(value) if isinstance(value, datetime) else value
            for name, value in row.items()
        }
        fields["fact_id"] = fields.pop("id")
        fields["revision"] = state_revision(row["updated_at"])
        return ActivityView(str(fact_id), fields)
