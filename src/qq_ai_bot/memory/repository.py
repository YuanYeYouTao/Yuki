"""Persistence-only repositories for Memory V2 facts, evidence, and jobs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar, cast

from sqlalchemy import and_, case, delete, event, func, literal, or_, select, text, true, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.memory.authorized_scope import AuthorizedMemoryScope, authorized_fact_condition
from qq_ai_bot.memory.eligibility import MemoryEventEligibilityPolicy
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryConflictState,
    MemoryFactRelationType,
    MemoryInvalidationReason,
    MemoryJobStatus,
    MemoryProcessingSource,
    MemoryRebuildJobOutcome,
    MemoryReviewState,
    MemoryScopeType,
    MemoryStateAction,
    MemoryStatus,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.job_claims import MemoryJobClaimLost, memory_job_claim_conditions
from qq_ai_bot.memory.models import (
    MemoryEntityTarget,
    MemoryEvidence,
    MemoryEvidenceCreate,
    MemoryFact,
    MemoryFactCreate,
    MemoryFactQuery,
    MemoryFactRelation,
    MemoryFactStateEvent,
    MemoryJob,
    MemoryTemporalIntent,
)
from qq_ai_bot.memory.partition import (
    MemoryPartitionResolutionError,
    resolve_active_person_id,
    resolve_active_space_id,
    resolve_fact_canonical_owners,
    resolve_memory_partition_for_event,
)
from qq_ai_bot.memory.projections import project_memory_fact_rows
from qq_ai_bot.memory.temporal_filter import strict_time_conditions
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.job_claims import PreparedJobClaim, commit_job_claims
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryFactRelationModel,
    MemoryFactStateEventModel,
    MemoryJobModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.persistence.repository_helpers import _event_record, keeper_event_clause

logger = logging.getLogger(__name__)
_Result = TypeVar("_Result")


@dataclass(frozen=True, slots=True)
class PreparedEvidenceCopy:
    identity: tuple[object, ...]
    values: tuple[dict[str, Any], ...]
    by_source: dict[tuple[int | None, int | None], MemoryEvidenceCreate]
    created_at: datetime


def _sql_fact_conversation_aligns(fact: Any = MemoryFactModel) -> Any:
    conv = CanonicalConversationModel
    conv_xor = or_(
        and_(conv.person_id.is_not(None), conv.space_id.is_(None)),
        and_(conv.person_id.is_(None), conv.space_id.is_not(None)),
    )
    person_ok = and_(
        fact.scope_type == MemoryScopeType.PERSON.value,
        fact.canonical_subject_person_id.is_not(None),
        fact.canonical_subject_space_id.is_(None),
        fact.canonical_visibility_person_id.is_(None),
        fact.canonical_visibility_space_id.is_(None),
        or_(
            conv.person_id == fact.canonical_subject_person_id,
            conv.space_id.is_not(None),
        ),
    )
    group_ok = and_(
        fact.scope_type == MemoryScopeType.GROUP.value,
        fact.canonical_subject_space_id.is_not(None),
        fact.canonical_subject_person_id.is_(None),
        fact.canonical_visibility_person_id.is_(None),
        fact.canonical_visibility_space_id.is_(None),
        conv.space_id == fact.canonical_subject_space_id,
    )
    person_group_ok = and_(
        fact.scope_type == MemoryScopeType.PERSON_GROUP.value,
        fact.canonical_subject_person_id.is_not(None),
        fact.canonical_subject_space_id.is_not(None),
        fact.canonical_visibility_person_id.is_(None),
        fact.canonical_visibility_space_id.is_(None),
        conv.space_id == fact.canonical_subject_space_id,
        or_(
            conv.person_id.is_(None),
            conv.person_id == fact.canonical_subject_person_id,
        ),
    )
    self_global = and_(
        fact.scope_type == MemoryScopeType.SELF.value,
        or_(fact.visibility_type.is_(None), fact.visibility_type == "global"),
        fact.canonical_subject_person_id.is_(None),
        fact.canonical_subject_space_id.is_(None),
        fact.canonical_visibility_person_id.is_(None),
        fact.canonical_visibility_space_id.is_(None),
    )
    self_private = and_(
        fact.scope_type == MemoryScopeType.SELF.value,
        fact.visibility_type == "private",
        fact.canonical_visibility_person_id.is_not(None),
        fact.canonical_subject_person_id.is_(None),
        fact.canonical_subject_space_id.is_(None),
        fact.canonical_visibility_space_id.is_(None),
        conv.person_id == fact.canonical_visibility_person_id,
    )
    self_group = and_(
        fact.scope_type == MemoryScopeType.SELF.value,
        fact.visibility_type == "group",
        fact.canonical_visibility_space_id.is_not(None),
        fact.canonical_subject_person_id.is_(None),
        fact.canonical_subject_space_id.is_(None),
        fact.canonical_visibility_person_id.is_(None),
        conv.space_id == fact.canonical_visibility_space_id,
    )
    return and_(
        conv_xor,
        or_(person_ok, group_ok, person_group_ok, self_global, self_private, self_group),
    )


def readable_evidence_predicate(
    *,
    fact: Any = MemoryFactModel,
    fact_table: Any = MemoryFactModel,
    fact_alias: str = "memory_facts",
    evidence: Any = MemoryEvidenceModel,
    evidence_table: Any = MemoryEvidenceModel,
    evidence_alias: str = "memory_evidence",
) -> Any:
    """The three mutually exclusive canonical source chains, evaluated in SQL.

    Correlation is explicit because the same predicate serves both a fact's
    aggregate count and its evidence page. No owner or source is reconstructed
    from an external account, and a run receipt never borrows a chat event.
    """
    live = and_(
        ChatEventModel.canonical_event_id.is_not(None),
        ChatEventModel.canonical_event_id != "",
        ChatEventModel.canonical_conversation_id.is_not(None),
        func.trim(ChatEventModel.author_kind) != "",
        keeper_event_clause(),
        _sql_fact_conversation_aligns(fact),
    )
    event_source = (
        select(literal(1))
        .select_from(ChatEventModel)
        .join(
            CanonicalConversationModel,
            CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
        )
        .where(ChatEventModel.id == evidence.event_id, live)
        .correlate(evidence_table, fact_table)
        .exists()
    )
    tool_source = (
        select(literal(1))
        .select_from(MemoryToolReceiptModel)
        .join(ChatEventModel, ChatEventModel.id == MemoryToolReceiptModel.trigger_event_id)
        .join(
            CanonicalConversationModel,
            CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
        )
        .where(
            MemoryToolReceiptModel.id == evidence.tool_receipt_id,
            MemoryToolReceiptModel.initiative_run_id.is_(None),
            live,
        )
        .correlate(evidence_table, fact_table)
        .exists()
    )
    from sqlalchemy import text

    from qq_ai_bot.memory.self_origin import sql_self_receipt_evidence_predicate

    initiative_source = (
        select(literal(1))
        .select_from(MemoryToolReceiptModel)
        .where(
            MemoryToolReceiptModel.id == evidence.tool_receipt_id,
            text(
                sql_self_receipt_evidence_predicate(
                    fact=fact_alias,
                    evidence=evidence_alias,
                    receipt="memory_tool_receipts",
                )
            ),
        )
        .correlate(evidence_table, fact_table)
        .exists()
    )
    return or_(
        event_source,
        and_(evidence.event_id.is_(None), or_(tool_source, initiative_source)),
    )


def readable_evidence_count_expression() -> Any:
    """Count using exactly the same source/owner gates as the evidence page."""
    return (
        select(func.count())
        .select_from(MemoryEvidenceModel)
        .where(
            MemoryEvidenceModel.fact_id == MemoryFactModel.id,
            readable_evidence_predicate(),
        )
        .correlate(MemoryFactModel)
        .scalar_subquery()
    )


class MemoryFactRepository:
    """Store and query facts without extraction or prompt logic."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    async def get_active_authorized(
        self, scope: AuthorizedMemoryScope, fact_ids: tuple[int, ...]
    ) -> tuple[MemoryFact, ...]:
        """Hydrate globally selected candidates under the same SQL ACL."""
        unique_ids = tuple(dict.fromkeys(fact_ids))
        if not unique_ids:
            return ()
        async with self._database.sessions() as session:
            rows = await self._execute_facts_with_count(
                session,
                [
                    MemoryFactModel.id.in_(unique_ids),
                    authorized_fact_condition(scope),
                    MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                    MemoryFactModel.review_state != "quarantined",
                    or_(
                        MemoryFactModel.valid_until.is_(None),
                        MemoryFactModel.valid_until > datetime.now(UTC),
                    ),
                ],
                order_by=(),
            )
            projected_rows = await project_memory_fact_rows(session, rows)
        projected = {fact.id: fact for fact in projected_rows}
        return tuple(projected[fact_id] for fact_id in unique_ids if fact_id in projected)

    async def _execute_facts_with_count(
        self,
        session: AsyncSession,
        conditions: list[Any],
        *,
        order_by: tuple[Any, ...],
        limit: int | None = None,
    ) -> list[Any]:
        counts = session.info.get("memory_evidence_counts")
        if counts is not None:
            statement = select(MemoryFactModel).where(*conditions)
            if order_by:
                statement = statement.order_by(*order_by)
            if limit is not None:
                statement = statement.limit(limit)
            facts = list((await session.scalars(statement)).all())
            missing = tuple(row.id for row in facts if row.id not in counts)
            if missing:
                if session.info.get("memory_evidence_write_started"):
                    raise RuntimeError("memory evidence count was not prepared before writing")
                await self.prepare_evidence_rows(missing, session=session)
            return [(row, counts[row.id]) for row in facts]
        statement = select(MemoryFactModel, readable_evidence_count_expression()).where(*conditions)
        if order_by:
            statement = statement.order_by(*order_by)
        if limit is not None:
            statement = statement.limit(limit)
        return list((await session.execute(statement)).all())

    @asynccontextmanager
    async def transaction(self, *, read_snapshot: bool = False) -> AsyncIterator[AsyncSession]:
        async with self._database.sessions() as session, session.begin():
            connection = None

            def mark_write(
                _connection: Any,
                _cursor: Any,
                _statement: str,
                _parameters: Any,
                context: Any,
                _executemany: bool,
            ) -> None:
                if (
                    context.isinsert
                    or context.isupdate
                    or context.isdelete
                    or _statement.lstrip()
                    .upper()
                    .startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
                ):
                    session.info["memory_evidence_write_started"] = True

            if read_snapshot:
                if not self._database.url.startswith("sqlite+"):
                    raise RuntimeError(
                        "memory evidence preparation requires SQLite snapshot isolation"
                    )
                # aiosqlite's legacy transaction mode does not BEGIN for SELECT.
                # An explicit deferred BEGIN keeps preparation and the eventual
                # write on one snapshot; WAL rejects stale upgrades atomically.
                await session.execute(text("BEGIN"))
                session.info["memory_evidence_counts"] = {}
                session.info["memory_evidence_rows"] = {}
                session.info["memory_evidence_additions"] = {}
                session.info["memory_added_relation_ids"] = []
                connection = (await session.connection()).sync_connection
                event.listen(connection, "before_cursor_execute", mark_write)
            try:
                yield session
            finally:
                if connection is not None:
                    event.remove(connection, "before_cursor_execute", mark_write)

    async def prepare_evidence_rows(
        self, fact_ids: tuple[int, ...], *, session: AsyncSession
    ) -> None:
        """Load complete readable evidence in bounded SQL pages before first DML."""
        counts = session.info.setdefault("memory_evidence_counts", {})
        rows_by_fact = session.info.setdefault("memory_evidence_rows", {})
        session.info.setdefault("memory_evidence_additions", {})
        missing = tuple(dict.fromkeys(i for i in fact_ids if i not in rows_by_fact))
        if not missing:
            return
        if session.info.get("memory_evidence_write_started"):
            raise RuntimeError("memory evidence was not prepared before writing")
        for offset in range(0, len(missing), 256):
            page = missing[offset : offset + 256]
            rows = (
                await session.scalars(
                    select(MemoryEvidenceModel)
                    .join(MemoryFactModel, MemoryFactModel.id == MemoryEvidenceModel.fact_id)
                    .where(MemoryEvidenceModel.fact_id.in_(page), readable_evidence_predicate())
                    .order_by(MemoryEvidenceModel.created_at.desc(), MemoryEvidenceModel.id.desc())
                )
            ).all()
            grouped: dict[int, list[MemoryEvidence]] = {i: [] for i in page}
            for row in rows:
                grouped[row.fact_id].append(self._evidence_record(row))
            for fact_id, evidence in grouped.items():
                rows_by_fact[fact_id] = tuple(evidence)
                counts[fact_id] = len(evidence)

    async def apply_evidence_write(
        self, operation: Callable[[AsyncSession], Awaitable[_Result]]
    ) -> _Result:
        """Run only a pure database mutation; post-commit work belongs to its caller."""
        while True:
            operation_failure: OperationalError | None = None
            try:
                async with self.transaction(read_snapshot=True) as session:
                    try:
                        result = await operation(session)
                        # ORM callbacks may defer their first DML until commit.
                        # Flush belongs to the pure operation, while physical
                        # commit acknowledgement remains outside this boundary.
                        await session.flush()
                    except OperationalError as exc:
                        operation_failure = exc
                        raise
                return result
            except OperationalError as exc:
                # The identical operation error survives only after the context
                # successfully rolls back. Commit/cleanup errors prove neither
                # a safe retry nor absence of durable effects.
                original = exc.orig
                if (
                    exc is not operation_failure
                    or original is None
                    or ((getattr(original, "sqlite_errorcode", 0) or 0) & 0xFF) != 5
                ):
                    raise
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise asyncio.CancelledError from None

    async def list_facts(
        self,
        query: MemoryFactQuery,
        *,
        limit: int | None = 100,
        after_id: int | None = None,
        include_quarantined: bool = False,
        order_by_id: bool = False,
        order_by_id_desc: bool = False,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        if order_by_id and order_by_id_desc:
            raise ValueError("memory facts cannot use both ascending and descending id order")
        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_facts(
                    query,
                    limit=limit,
                    after_id=after_id,
                    include_quarantined=include_quarantined,
                    order_by_id=order_by_id,
                    order_by_id_desc=order_by_id_desc,
                    session=owned,
                )
        conditions = [
            MemoryFactModel.scope_type == query.scope_type.value,
            MemoryFactModel.status == query.status.value,
        ]
        if not include_quarantined:
            conditions.append(MemoryFactModel.review_state != "quarantined")
        if after_id is not None:
            conditions.append(MemoryFactModel.id > after_id)
        conditions.extend(await self._query_identity_conditions(session, query))
        if query.kind is not None:
            conditions.append(MemoryFactModel.kind == query.kind.value)
        if query.status is MemoryStatus.ACTIVE:
            conditions.append(
                or_(
                    MemoryFactModel.valid_until.is_(None),
                    MemoryFactModel.valid_until > datetime.now(UTC),
                )
            )
        order: tuple[Any, ...]
        if order_by_id:
            order = (MemoryFactModel.id.asc(),)
        elif order_by_id_desc:
            order = (MemoryFactModel.id.desc(),)
        else:
            order = (MemoryFactModel.importance.desc(), MemoryFactModel.updated_at.desc())
        rows = await self._execute_facts_with_count(
            session, conditions, order_by=order, limit=max(1, limit) if limit is not None else None
        )
        return await project_memory_fact_rows(session, rows)

    async def get_fact(
        self,
        fact_id: int,
        *,
        session: AsyncSession | None = None,
    ) -> MemoryFact | None:
        if session is None:
            async with self._database.sessions() as owned:
                return await self.get_fact(fact_id, session=owned)
        rows = await self._execute_facts_with_count(
            session,
            [MemoryFactModel.id == fact_id],
            order_by=(),
            limit=1,
        )
        projected = await project_memory_fact_rows(session, rows)
        return projected[0] if projected else None

    async def get_facts(
        self, fact_ids: tuple[int, ...], *, session: AsyncSession
    ) -> tuple[MemoryFact, ...]:
        if not fact_ids:
            return ()
        rows = await self._execute_facts_with_count(
            session, [MemoryFactModel.id.in_(fact_ids)], order_by=()
        )
        return await project_memory_fact_rows(session, rows)

    async def get_active_for_target(
        self,
        target: MemoryEntityTarget,
        fact_ids: tuple[int, ...],
        *,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        """Load candidate facts only inside the already resolved identity boundary."""

        unique_ids = tuple(dict.fromkeys(fact_ids))
        if not unique_ids:
            return ()
        if session is None:
            async with self._database.sessions() as owned:
                return await self.get_active_for_target(target, unique_ids, session=owned)
        rows = await self._execute_facts_with_count(
            session,
            [
                MemoryFactModel.id.in_(unique_ids),
                *(await self._async_target_conditions(session, target)),
                MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                MemoryFactModel.review_state != "quarantined",
                or_(
                    MemoryFactModel.valid_until.is_(None),
                    MemoryFactModel.valid_until > datetime.now(UTC),
                ),
            ],
            order_by=(),
        )
        projected_rows = await project_memory_fact_rows(session, rows)
        projected = {fact.id: fact for fact in projected_rows}
        return tuple(projected[fact_id] for fact_id in unique_ids if fact_id in projected)

    async def get_active_for_exact_target(
        self,
        target: MemoryEntityTarget,
        fact_ids: tuple[int, ...],
        *,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        """Load one exact mutation target without widening SELF reads to global."""

        unique_ids = tuple(dict.fromkeys(fact_ids))
        if not unique_ids:
            return ()
        if session is None:
            async with self._database.sessions() as owned:
                return await self.get_active_for_exact_target(target, unique_ids, session=owned)
        rows = await self._execute_facts_with_count(
            session,
            [
                MemoryFactModel.id.in_(unique_ids),
                MemoryFactModel.scope_type == target.scope_type.value,
                *(await self._query_identity_conditions(session, target)),
                MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                MemoryFactModel.review_state != "quarantined",
                or_(
                    MemoryFactModel.valid_until.is_(None),
                    MemoryFactModel.valid_until > datetime.now(UTC),
                ),
            ],
            order_by=(),
        )
        projected_rows = await project_memory_fact_rows(session, rows)
        projected = {fact.id: fact for fact in projected_rows}
        return tuple(projected[fact_id] for fact_id in unique_ids if fact_id in projected)

    async def find_global_self_cover(
        self,
        fact: MemoryFactCreate,
        *,
        normalized_content: str,
        session: AsyncSession | None = None,
    ) -> MemoryFact | None:
        """Find an exact active global SELF fact covering a narrower claim."""

        if (
            fact.scope_type is not MemoryScopeType.SELF
            or fact.visibility_type is SelfMemoryVisibility.GLOBAL
        ):
            return None
        if session is None:
            async with self._database.sessions() as owned:
                return await self.find_global_self_cover(
                    fact,
                    normalized_content=normalized_content,
                    session=owned,
                )
        rows = await self._execute_facts_with_count(
            session,
            [
                MemoryFactModel.scope_type == MemoryScopeType.SELF.value,
                MemoryFactModel.visibility_type == SelfMemoryVisibility.GLOBAL.value,
                MemoryFactModel.canonical_subject_person_id.is_(None),
                MemoryFactModel.canonical_subject_space_id.is_(None),
                MemoryFactModel.canonical_visibility_person_id.is_(None),
                MemoryFactModel.canonical_visibility_space_id.is_(None),
                MemoryFactModel.kind == fact.kind.value,
                MemoryFactModel.memory_key == fact.memory_key,
                MemoryFactModel.normalized_content == normalized_content,
                MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                MemoryFactModel.review_state != "quarantined",
                or_(
                    MemoryFactModel.valid_until.is_(None),
                    MemoryFactModel.valid_until > datetime.now(UTC),
                ),
            ],
            order_by=(MemoryFactModel.updated_at.desc(), MemoryFactModel.id.asc()),
            limit=1,
        )
        projected = await project_memory_fact_rows(session, rows)
        return projected[0] if projected else None

    async def list_conflict_candidates(
        self,
        fact: MemoryFactCreate,
        *,
        normalized_content: str,
        limit: int,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        """Return bounded same-target candidates; never widens an identity scope."""

        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_conflict_candidates(
                    fact,
                    normalized_content=normalized_content,
                    limit=limit,
                    session=owned,
                )
        rows = await self._execute_facts_with_count(
            session,
            [
                MemoryFactModel.scope_type == fact.scope_type.value,
                *(await self._query_identity_conditions(session, fact)),
                MemoryFactModel.status.in_(
                    (
                        MemoryStatus.ACTIVE.value,
                        MemoryStatus.CONTESTED.value,
                    )
                ),
                MemoryFactModel.review_state != "quarantined",
                or_(
                    MemoryFactModel.memory_key == fact.memory_key,
                    MemoryFactModel.normalized_content == normalized_content,
                    and_(
                        MemoryFactModel.category == fact.category,
                        MemoryFactModel.kind == fact.kind.value,
                    ),
                ),
            ],
            order_by=(
                (MemoryFactModel.memory_key == fact.memory_key).desc(),
                (MemoryFactModel.normalized_content == normalized_content).desc(),
                MemoryFactModel.updated_at.desc(),
                MemoryFactModel.id.asc(),
            ),
            limit=max(1, limit),
        )
        return await project_memory_fact_rows(session, rows)

    async def list_mutation_locator_candidates(
        self,
        target: MemoryFactQuery,
        *,
        memory_key: str | None,
        normalized_content: str | None,
        category: str | None,
        statuses: tuple[MemoryStatus, ...],
        limit: int = 4,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        """Return exact-first lexical candidates inside one exact mutation target."""

        if not statuses or (memory_key is None and normalized_content is None):
            return ()
        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_mutation_locator_candidates(
                    target,
                    memory_key=memory_key,
                    normalized_content=normalized_content,
                    category=category,
                    statuses=statuses,
                    limit=limit,
                    session=owned,
                )
        exact_parts: list[Any] = []
        lexical_parts: list[Any] = []
        if memory_key is not None:
            exact_parts.append(MemoryFactModel.memory_key == memory_key)
            lexical_parts.append(MemoryFactModel.memory_key.contains(memory_key, autoescape=True))
            # The Agent may only know the user's visible label, not the
            # canonical internal key assigned when the fact was created. Use
            # that label to surface bounded candidates from content as well;
            # _locate_fact still requires an exact selector before execution.
            lexical_parts.append(
                MemoryFactModel.normalized_content.contains(memory_key.casefold(), autoescape=True)
            )
        if normalized_content is not None:
            exact_parts.append(MemoryFactModel.normalized_content == normalized_content)
            lexical_parts.append(
                MemoryFactModel.normalized_content.contains(
                    normalized_content,
                    autoescape=True,
                )
            )
        if category is not None:
            exact_parts.append(MemoryFactModel.category == category)
        exact_match = and_(*exact_parts)
        conditions: list[Any] = [
            MemoryFactModel.scope_type == target.scope_type.value,
            *(await self._query_identity_conditions(session, target)),
            MemoryFactModel.status.in_(tuple(status.value for status in statuses)),
            MemoryFactModel.review_state != MemoryReviewState.QUARANTINED.value,
            or_(
                MemoryFactModel.valid_until.is_(None),
                MemoryFactModel.valid_until > datetime.now(UTC),
            ),
            or_(*lexical_parts),
        ]
        if category is not None:
            conditions.append(MemoryFactModel.category == category)
        rows = await self._execute_facts_with_count(
            session,
            conditions,
            order_by=(
                exact_match.desc(),
                (
                    MemoryFactModel.memory_key == memory_key
                    if memory_key is not None
                    else exact_match
                ).desc(),
                (
                    MemoryFactModel.normalized_content == normalized_content
                    if normalized_content is not None
                    else exact_match
                ).desc(),
                MemoryFactModel.updated_at.desc(),
                MemoryFactModel.id.asc(),
            ),
            limit=max(1, min(limit, 4)),
        )
        return await project_memory_fact_rows(session, rows)

    async def list_overview(
        self,
        target: MemoryEntityTarget,
        *,
        limit: int,
        temporal: MemoryTemporalIntent | None = None,
    ) -> tuple[MemoryFact, ...]:
        async with self._database.sessions() as session:
            rows = await self._execute_facts_with_count(
                session,
                [
                    *strict_time_conditions(temporal),
                    *(await self._async_target_conditions(session, target)),
                    MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                    MemoryFactModel.review_state != "quarantined",
                    or_(
                        MemoryFactModel.valid_until.is_(None),
                        MemoryFactModel.valid_until > datetime.now(UTC),
                    ),
                ],
                order_by=(
                    MemoryFactModel.importance.desc(),
                    MemoryFactModel.confidence.desc(),
                    MemoryFactModel.updated_at.desc(),
                    MemoryFactModel.id.asc(),
                ),
                limit=max(1, limit),
            )
            return await project_memory_fact_rows(session, rows)

    async def list_explicit_preferences(
        self,
        target: MemoryEntityTarget,
        *,
        limit: int,
        temporal: MemoryTemporalIntent | None = None,
    ) -> tuple[MemoryFact, ...]:
        if limit <= 0:
            return ()
        async with self._database.sessions() as session:
            rows = await self._execute_facts_with_count(
                session,
                [
                    *strict_time_conditions(temporal),
                    *(await self._async_target_conditions(session, target)),
                    MemoryFactModel.kind == "preference",
                    MemoryFactModel.source_type == "explicit",
                    MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                    MemoryFactModel.review_state != "quarantined",
                    or_(
                        MemoryFactModel.valid_until.is_(None),
                        MemoryFactModel.valid_until > datetime.now(UTC),
                    ),
                ],
                order_by=(
                    MemoryFactModel.importance.desc(),
                    MemoryFactModel.confidence.desc(),
                    MemoryFactModel.updated_at.desc(),
                    MemoryFactModel.id.asc(),
                ),
                limit=limit,
            )
            return await project_memory_fact_rows(session, rows)

    async def mark_injected(self, fact_ids: tuple[int, ...]) -> int:
        unique_ids = tuple(dict.fromkeys(fact_ids))
        if not unique_ids:
            return 0
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(MemoryFactModel)
                .where(
                    MemoryFactModel.id.in_(unique_ids),
                    MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                    MemoryFactModel.review_state != "quarantined",
                )
                .values(last_injected_at=datetime.now(UTC))
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def create_fact(
        self,
        fact: MemoryFact | MemoryFactCreate,
        *,
        normalized_content: str,
        supersedes_id: int | None,
        recorded_at: datetime | None = None,
        session: AsyncSession,
    ) -> MemoryFactModel:
        now = recorded_at or datetime.now(UTC)
        owners = await resolve_fact_canonical_owners(session, fact)
        row = MemoryFactModel(
            scope_type=fact.scope_type.value,
            visibility_type=(fact.visibility_type.value if fact.visibility_type else None),
            kind=fact.kind.value,
            memory_key=fact.memory_key,
            category=fact.category,
            content=fact.content,
            normalized_content=normalized_content,
            importance=fact.importance,
            confidence=fact.confidence,
            source_type=fact.source_type.value,
            authority=fact.authority.value,
            status=fact.status.value,
            conflict_state=fact.conflict_state.value,
            supersedes_id=supersedes_id,
            valid_from=fact.valid_from,
            valid_until=fact.valid_until,
            created_at=now,
            updated_at=now,
            last_confirmed_at=now,
            invalidated_reason=(
                fact.invalidated_reason.value if fact.invalidated_reason is not None else None
            ),
            last_injected_at=None,
            validation_version=fact.validation_version,
            last_audited_at=fact.last_audited_at,
            review_state=fact.review_state.value,
            canonical_subject_person_id=owners.subject_person_id,
            canonical_subject_space_id=owners.subject_space_id,
            canonical_visibility_person_id=owners.visibility_person_id,
            canonical_visibility_space_id=owners.visibility_space_id,
        )
        session.add(row)
        await session.flush()
        if "memory_evidence_counts" in session.info:
            session.info["memory_evidence_counts"][row.id] = 0
            session.info["memory_evidence_rows"][row.id] = ()
        return row

    async def transition(
        self,
        fact_id: int,
        *,
        status: MemoryStatus,
        conflict_state: MemoryConflictState,
        invalidated_reason: MemoryInvalidationReason | None,
        action: MemoryStateAction,
        reason_code: str,
        source_event_id: int | None,
        actor_user_id: str | None,
        session: AsyncSession,
    ) -> bool:
        row = await session.get(MemoryFactModel, fact_id)
        if row is None:
            return False
        now = datetime.now(UTC)
        before_status = row.status
        before_conflict = row.conflict_state
        row.status = status.value
        row.conflict_state = conflict_state.value
        row.invalidated_reason = invalidated_reason.value if invalidated_reason else None
        row.updated_at = now
        session.add(
            MemoryFactStateEventModel(
                fact_id=fact_id,
                action=action.value,
                from_status=before_status,
                to_status=status.value,
                from_conflict_state=before_conflict,
                to_conflict_state=conflict_state.value,
                reason_code=reason_code[:64],
                source_event_id=source_event_id,
                actor_user_id=actor_user_id,
                created_at=now,
            )
        )
        await session.flush()
        return True

    async def record_created(
        self,
        fact_id: int,
        *,
        status: MemoryStatus,
        conflict_state: MemoryConflictState,
        reason_code: str,
        source_event_id: int | None,
        actor_user_id: str | None,
        session: AsyncSession,
    ) -> None:
        now = datetime.now(UTC)
        session.add(
            MemoryFactStateEventModel(
                fact_id=fact_id,
                action=MemoryStateAction.CREATED.value,
                from_status=None,
                to_status=status.value,
                from_conflict_state=None,
                to_conflict_state=conflict_state.value,
                reason_code=reason_code[:64],
                source_event_id=source_event_id,
                actor_user_id=actor_user_id,
                created_at=now,
            )
        )
        await session.flush()

    async def update_confirmation_metadata(
        self,
        fact_id: int,
        *,
        confirmed_at: datetime,
        session: AsyncSession,
        updated_at: datetime | None = None,
    ) -> None:
        current = await session.get(MemoryFactModel, fact_id)
        if current is None:
            return
        previous = current.last_confirmed_at
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=UTC)
        if confirmed_at.tzinfo is None:
            confirmed_at = confirmed_at.replace(tzinfo=UTC)
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == fact_id)
            .values(
                last_confirmed_at=max(previous, confirmed_at),
                updated_at=updated_at or datetime.now(UTC),
            )
        )

    async def set_review_state(
        self,
        fact_id: int,
        *,
        review_state: MemoryReviewState,
        session: AsyncSession,
    ) -> bool:
        row = await session.get(MemoryFactModel, fact_id)
        if row is None:
            return False
        row.review_state = review_state.value
        row.updated_at = datetime.now(UTC)
        await session.flush()
        return True

    async def restore_confirmation_metadata(
        self,
        fact_id: int,
        *,
        authority: str,
        confidence: float,
        last_confirmed_at: datetime,
        session: AsyncSession,
    ) -> None:
        """Restore exact aggregate fields captured by a reversible internal operation."""

        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == fact_id)
            .values(
                authority=authority,
                confidence=confidence,
                last_confirmed_at=last_confirmed_at,
                updated_at=datetime.now(UTC),
            )
        )

    async def add_relation(
        self,
        *,
        source_fact_id: int,
        target_fact_id: int,
        relation_type: MemoryFactRelationType,
        confidence: float,
        source_event_id: int | None,
        session: AsyncSession,
    ) -> bool:
        statement = insert(MemoryFactRelationModel).values(
            source_fact_id=source_fact_id,
            target_fact_id=target_fact_id,
            relation_type=relation_type.value,
            confidence=confidence,
            source_event_id=source_event_id,
            created_at=datetime.now(UTC),
        )
        relation_id = await session.scalar(
            statement.on_conflict_do_nothing(
                index_elements=[
                    MemoryFactRelationModel.source_fact_id,
                    MemoryFactRelationModel.target_fact_id,
                    MemoryFactRelationModel.relation_type,
                ]
            ).returning(MemoryFactRelationModel.id)
        )
        if relation_id is not None and "memory_added_relation_ids" in session.info:
            session.info["memory_added_relation_ids"].append(relation_id)
        return relation_id is not None

    async def refresh_fact(
        self,
        fact_id: int,
        *,
        importance: int,
        confidence: float,
        session: AsyncSession,
    ) -> None:
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == fact_id)
            .values(
                importance=func.max(MemoryFactModel.importance, importance),
                confidence=func.max(MemoryFactModel.confidence, confidence),
                updated_at=datetime.now(UTC),
            )
        )

    async def prepare_evidence_copy(
        self,
        source_fact_ids: tuple[int, ...],
        *,
        identity: tuple[object, ...],
        authority: MemoryAuthority | None,
        session: AsyncSession,
    ) -> PreparedEvidenceCopy:
        """Apply both source and replacement owner predicates before the writer."""
        if session.info.get("memory_evidence_write_started"):
            raise RuntimeError("memory evidence copy was not prepared before writing")
        target = select(
            *(
                literal(value).label(name)
                for name, value in zip(
                    (
                        "scope_type",
                        "visibility_type",
                        "canonical_subject_person_id",
                        "canonical_subject_space_id",
                        "canonical_visibility_person_id",
                        "canonical_visibility_space_id",
                    ),
                    identity,
                    strict=True,
                )
            )
        ).cte("prepared_memory_fact")
        source_ids = tuple(dict.fromkeys(source_fact_ids))
        copied = (
            select(
                *(
                    (
                        literal(authority.value)
                        if column.name == "authority" and authority is not None
                        else literal("third_party_statement")
                        if column.name == "relation" and authority is MemoryAuthority.THIRD_PARTY
                        else column
                    ).label(column.name)
                    for column in MemoryEvidenceModel.__table__.columns
                )
            )
            .where(MemoryEvidenceModel.fact_id.in_(source_ids))
            .cte("prepared_memory_evidence")
        )
        statement = (
            select(copied)
            .select_from(MemoryEvidenceModel)
            .join(MemoryFactModel, MemoryFactModel.id == MemoryEvidenceModel.fact_id)
            .join(copied, copied.c.id == MemoryEvidenceModel.id)
            .join(target, true())
            .where(
                readable_evidence_predicate(),
                readable_evidence_predicate(
                    fact=target.c,
                    fact_table=target,
                    fact_alias="prepared_memory_fact",
                    evidence=copied.c,
                    evidence_table=copied,
                    evidence_alias="prepared_memory_evidence",
                ),
            )
            .order_by(
                case(
                    {i: position for position, i in enumerate(source_ids)},
                    value=MemoryEvidenceModel.fact_id,
                ),
                MemoryEvidenceModel.created_at.desc(),
                MemoryEvidenceModel.id.desc(),
            )
        )
        rows = (await session.execute(statement)).mappings().all()
        by_source: dict[tuple[int | None, int | None], MemoryEvidenceCreate] = {}
        for row in rows:
            evidence = MemoryEvidenceCreate(
                **{
                    name: row[name]
                    for name in (
                        "event_id",
                        "tool_receipt_id",
                        "source_speaker_user_id",
                        "relation",
                        "confidence",
                        "authority",
                        "excerpt",
                    )
                }
            )
            reflection = evidence.authority is MemoryAuthority.AGENT_REFLECTION
            if reflection != (evidence.relation.value == "agent_reflection"):
                raise ValueError("agent reflection evidence relation and authority must match")
            if reflection and identity[0] != MemoryScopeType.SELF.value:
                raise ValueError("agent reflection evidence is only valid for self memory")
            by_source.setdefault((evidence.event_id, evidence.tool_receipt_id), evidence)
        created_at = datetime.now(UTC)
        values = tuple(
            {
                **item.model_dump(mode="json"),
                "excerpt": item.excerpt,
                "created_at": created_at,
            }
            for item in by_source.values()
        )
        return PreparedEvidenceCopy(identity, values, by_source, created_at)

    async def copy_prepared_evidence(
        self,
        fact_id: int,
        prepared: PreparedEvidenceCopy,
        *,
        session: AsyncSession,
        selected: tuple[MemoryEvidenceCreate, ...] | None = None,
    ) -> int:
        """Insert a snapshot-validated bundle without per-source reads in the writer."""
        fact = await session.get(MemoryFactModel, fact_id)
        if fact is None:
            return 0
        identity = tuple(
            getattr(fact, name)
            for name in (
                "scope_type",
                "visibility_type",
                "canonical_subject_person_id",
                "canonical_subject_space_id",
                "canonical_visibility_person_id",
                "canonical_visibility_space_id",
            )
        )
        if identity != prepared.identity:
            raise RuntimeError("memory evidence copy target changed")
        values = prepared.values
        if selected is not None:
            values = tuple(
                {
                    **prepared.by_source[key].model_dump(mode="json"),
                    "created_at": prepared.created_at,
                }
                for key in dict.fromkeys((item.event_id, item.tool_receipt_id) for item in selected)
                if key in prepared.by_source
            )
        added = 0
        for offset in range(0, len(values), 128):
            rows = (
                await session.execute(
                    insert(MemoryEvidenceModel)
                    .values([dict(item, fact_id=fact_id) for item in values[offset : offset + 128]])
                    .on_conflict_do_nothing()
                    .returning(
                        MemoryEvidenceModel.id,
                        MemoryEvidenceModel.event_id,
                        MemoryEvidenceModel.tool_receipt_id,
                    )
                )
            ).all()
            for evidence_id, event_id, receipt_id in rows:
                item = prepared.by_source[(event_id, receipt_id)]
                session.info["memory_evidence_additions"].setdefault(fact_id, []).append(
                    MemoryEvidence(
                        id=evidence_id,
                        fact_id=fact_id,
                        created_at=prepared.created_at.replace(tzinfo=None),
                        **item.model_dump(),
                    )
                )
            added += len(rows)
        if added:
            session.info["memory_evidence_counts"][fact_id] += added
            previous = fact.updated_at
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=UTC)
            fact.updated_at = max(datetime.now(UTC), previous + timedelta(microseconds=1))
        return added

    async def add_evidence(
        self,
        fact_id: int,
        evidence: MemoryEvidenceCreate,
        *,
        session: AsyncSession,
    ) -> bool:
        reflection_authority = evidence.authority is MemoryAuthority.AGENT_REFLECTION
        reflection_relation = evidence.relation.value == "agent_reflection"
        if reflection_authority != reflection_relation:
            raise ValueError("agent reflection evidence relation and authority must match")
        if reflection_authority:
            scope_type = await session.scalar(
                select(MemoryFactModel.scope_type).where(MemoryFactModel.id == fact_id)
            )
            if scope_type != MemoryScopeType.SELF.value:
                raise ValueError("agent reflection evidence is only valid for self memory")
        from qq_ai_bot.identity.memory_guard import v2_evidence_event_chain_readable

        fact_row = await session.get(MemoryFactModel, fact_id)
        if fact_row is None:
            return False
        if evidence.event_id is not None:
            event = await session.get(ChatEventModel, evidence.event_id)
            if event is None or not await v2_evidence_event_chain_readable(
                session, fact_row, event
            ):
                return False
        else:
            receipt = await session.get(MemoryToolReceiptModel, evidence.tool_receipt_id)
            if receipt is None:
                return False
            if receipt.initiative_run_id is not None:
                from qq_ai_bot.memory.self_origin import receipt_evidence_readable

                if not await receipt_evidence_readable(
                    session,
                    fact=fact_row,
                    evidence=evidence,
                    receipt=receipt,
                ):
                    return False
            else:
                if receipt.trigger_event_id is None:
                    return False
                trigger = await session.get(ChatEventModel, receipt.trigger_event_id)
                if trigger is None or not await v2_evidence_event_chain_readable(
                    session, fact_row, trigger
                ):
                    return False
        created_at = datetime.now(UTC)
        statement = insert(MemoryEvidenceModel).values(
            fact_id=fact_id,
            event_id=evidence.event_id,
            tool_receipt_id=evidence.tool_receipt_id,
            source_speaker_user_id=evidence.source_speaker_user_id,
            relation=evidence.relation.value,
            confidence=evidence.confidence,
            authority=evidence.authority.value,
            excerpt=evidence.excerpt,
            created_at=created_at,
        )
        index_elements = (
            [MemoryEvidenceModel.fact_id, MemoryEvidenceModel.event_id]
            if evidence.event_id is not None
            else [MemoryEvidenceModel.fact_id, MemoryEvidenceModel.tool_receipt_id]
        )
        evidence_id = await session.scalar(
            statement.on_conflict_do_nothing(index_elements=index_elements).returning(
                MemoryEvidenceModel.id
            )
        )
        added = evidence_id is not None
        if added:
            if "memory_evidence_counts" in session.info:
                counts = session.info["memory_evidence_counts"]
                if fact_id not in counts:
                    raise RuntimeError("memory evidence was not prepared before writing")
                counts[fact_id] += 1
                session.info["memory_evidence_additions"].setdefault(fact_id, []).append(
                    MemoryEvidence(
                        id=evidence_id,
                        fact_id=fact_id,
                        created_at=created_at.replace(tzinfo=None),
                        **evidence.model_dump(),
                    )
                )
            # Evidence may make an earlier unreadable fact usable. Advance its
            # change cursor atomically, but do not wake readers for duplicate evidence.
            previous = fact_row.updated_at
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=UTC)
            fact_row.updated_at = max(datetime.now(UTC), previous + timedelta(microseconds=1))
        return added

    async def list_evidence(
        self,
        fact_id: int,
        *,
        limit: int | None = 100,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryEvidence, ...]:
        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_evidence(fact_id, limit=limit, session=owned)
        prepared = session.info.get("memory_evidence_rows")
        if prepared is not None:
            await self.prepare_evidence_rows((fact_id,), session=session)
            additions = session.info["memory_evidence_additions"].get(fact_id, ())
            if not additions:
                cached_rows = cast(tuple[MemoryEvidence, ...], prepared[fact_id])
                return cached_rows if limit is None else cached_rows[: max(1, limit)]
            combined = (*reversed(additions), *prepared[fact_id])
            return combined if limit is None else combined[: max(1, limit)]
        rows = (
            await session.scalars(
                select(MemoryEvidenceModel)
                .join(MemoryFactModel, MemoryFactModel.id == MemoryEvidenceModel.fact_id)
                .where(
                    MemoryEvidenceModel.fact_id == fact_id,
                    readable_evidence_predicate(),
                )
                .order_by(MemoryEvidenceModel.created_at.desc(), MemoryEvidenceModel.id.desc())
                .limit(max(1, limit) if limit is not None else None)
            )
        ).all()
        return tuple(self._evidence_record(row) for row in rows)

    @staticmethod
    def _evidence_record(row: MemoryEvidenceModel) -> MemoryEvidence:
        return MemoryEvidence(
            id=row.id,
            fact_id=row.fact_id,
            event_id=row.event_id,
            tool_receipt_id=row.tool_receipt_id,
            source_speaker_user_id=row.source_speaker_user_id,
            relation=row.relation,
            confidence=row.confidence,
            authority=row.authority,
            excerpt=row.excerpt,
            created_at=row.created_at,
        )

    async def list_relations(
        self,
        fact_id: int,
        *,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFactRelation, ...]:
        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_relations(fact_id, session=owned)
        rows = (
            await session.scalars(
                select(MemoryFactRelationModel)
                .where(
                    or_(
                        MemoryFactRelationModel.source_fact_id == fact_id,
                        MemoryFactRelationModel.target_fact_id == fact_id,
                    )
                )
                .order_by(MemoryFactRelationModel.created_at, MemoryFactRelationModel.id)
            )
        ).all()
        return tuple(
            MemoryFactRelation(
                id=row.id,
                source_fact_id=row.source_fact_id,
                target_fact_id=row.target_fact_id,
                relation_type=row.relation_type,
                confidence=row.confidence,
                source_event_id=row.source_event_id,
                created_at=row.created_at,
            )
            for row in rows
        )

    async def list_state_events(self, fact_id: int) -> tuple[MemoryFactStateEvent, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(MemoryFactStateEventModel)
                    .where(MemoryFactStateEventModel.fact_id == fact_id)
                    .order_by(
                        MemoryFactStateEventModel.created_at,
                        MemoryFactStateEventModel.id,
                    )
                )
            ).all()
        return tuple(
            MemoryFactStateEvent(
                id=row.id,
                fact_id=row.fact_id,
                action=row.action,
                from_status=row.from_status,
                to_status=row.to_status,
                from_conflict_state=row.from_conflict_state,
                to_conflict_state=row.to_conflict_state,
                reason_code=row.reason_code,
                source_event_id=row.source_event_id,
                actor_user_id=row.actor_user_id,
                created_at=row.created_at,
            )
            for row in rows
        )

    async def list_conflicts(
        self,
        *,
        scope_type: str | None = None,
        subject_user_id: str | None = None,
        group_id: str | None = None,
        limit: int = 100,
    ) -> tuple[MemoryFact, ...]:
        conditions = [
            or_(
                MemoryFactModel.status == MemoryStatus.CONTESTED.value,
                MemoryFactModel.conflict_state == MemoryConflictState.CONTESTED.value,
            )
        ]
        if scope_type is not None:
            conditions.append(MemoryFactModel.scope_type == scope_type)
        async with self._database.sessions() as session:
            try:
                if subject_user_id is not None:
                    conditions.append(
                        MemoryFactModel.canonical_subject_person_id
                        == await resolve_active_person_id(session, subject_user_id)
                    )
                if group_id is not None:
                    conditions.append(
                        MemoryFactModel.canonical_subject_space_id
                        == await resolve_active_space_id(session, group_id)
                    )
            except MemoryPartitionResolutionError:
                return ()
            rows = await self._execute_facts_with_count(
                session,
                conditions,
                order_by=(MemoryFactModel.updated_at.desc(), MemoryFactModel.id),
                limit=max(1, limit),
            )
            return await project_memory_fact_rows(session, rows)

    async def list_expired_candidates(
        self,
        *,
        now: datetime,
        limit: int,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_expired_candidates(now=now, limit=limit, session=owned)
        rows = await self._execute_facts_with_count(
            session,
            [
                MemoryFactModel.status.in_(
                    (MemoryStatus.ACTIVE.value, MemoryStatus.CONTESTED.value)
                ),
                MemoryFactModel.valid_until <= now,
            ],
            order_by=(MemoryFactModel.valid_until.asc(), MemoryFactModel.id),
            limit=limit,
        )
        return await project_memory_fact_rows(session, rows)

    async def count_active(
        self,
        query: MemoryFactQuery,
        *,
        session: AsyncSession | None = None,
    ) -> int:
        if session is None:
            async with self._database.sessions() as owned:
                return await self.count_active(query, session=owned)
        conditions = [
            MemoryFactModel.scope_type == query.scope_type.value,
            MemoryFactModel.status == query.status.value,
            MemoryFactModel.review_state != "quarantined",
            *(await self._query_identity_conditions(session, query)),
        ]
        if query.kind is not None:
            conditions.append(MemoryFactModel.kind == query.kind.value)
        if query.status is MemoryStatus.ACTIVE:
            conditions.append(
                or_(
                    MemoryFactModel.valid_until.is_(None),
                    MemoryFactModel.valid_until > datetime.now(UTC),
                )
            )
        return int(
            await session.scalar(
                select(func.count()).select_from(MemoryFactModel).where(*conditions)
            )
            or 0
        )

    async def delete_orphaned_automatic_facts(
        self,
        *,
        event_ids: tuple[int, ...],
        exact_text: str,
        session: AsyncSession,
    ) -> None:
        evidence_fact_ids = select(MemoryEvidenceModel.fact_id).where(
            MemoryEvidenceModel.event_id.in_(event_ids)
        )
        await session.execute(
            delete(MemoryFactModel).where(
                MemoryFactModel.source_type != "explicit",
                or_(
                    MemoryFactModel.content.contains(exact_text),
                    MemoryFactModel.id.in_(evidence_fact_ids),
                ),
            )
        )

    @staticmethod
    def _unmatched_identity() -> tuple[Any, ...]:
        return (MemoryFactModel.id == -1,)

    async def _person_subject_conditions(
        self,
        session: AsyncSession,
        user_id: str,
    ) -> tuple[Any, ...]:
        try:
            person_id = await resolve_active_person_id(session, user_id)
        except MemoryPartitionResolutionError:
            return self._unmatched_identity()
        return (MemoryFactModel.canonical_subject_person_id == person_id,)

    async def _query_identity_conditions(
        self,
        session: AsyncSession,
        query: MemoryFactCreate | MemoryFactQuery | MemoryEntityTarget,
    ) -> tuple[Any, ...]:
        try:
            owners = await resolve_fact_canonical_owners(session, query)
        except MemoryPartitionResolutionError:
            return self._unmatched_identity()
        conditions = [
            (
                MemoryFactModel.canonical_subject_person_id == owners.subject_person_id
                if owners.subject_person_id is not None
                else MemoryFactModel.canonical_subject_person_id.is_(None)
            ),
            (
                MemoryFactModel.canonical_subject_space_id == owners.subject_space_id
                if owners.subject_space_id is not None
                else MemoryFactModel.canonical_subject_space_id.is_(None)
            ),
        ]
        visibility_type = getattr(query, "visibility_type", None)
        visibility_value = visibility_type.value if visibility_type is not None else None
        if str(getattr(query.scope_type, "value", query.scope_type)) == "self":
            conditions.append(
                MemoryFactModel.visibility_type.is_(None)
                if visibility_value is None
                else MemoryFactModel.visibility_type == visibility_value
            )
            conditions.append(
                MemoryFactModel.canonical_visibility_person_id == owners.visibility_person_id
                if owners.visibility_person_id is not None
                else MemoryFactModel.canonical_visibility_person_id.is_(None)
            )
            conditions.append(
                MemoryFactModel.canonical_visibility_space_id == owners.visibility_space_id
                if owners.visibility_space_id is not None
                else MemoryFactModel.canonical_visibility_space_id.is_(None)
            )
        else:
            conditions.extend(self._exact_visibility_conditions())
        return tuple(conditions)

    async def _async_target_conditions(
        self,
        session: AsyncSession,
        target: MemoryEntityTarget,
    ) -> tuple[Any, ...]:
        try:
            owners = await resolve_fact_canonical_owners(session, target)
        except MemoryPartitionResolutionError:
            return self._unmatched_identity()
        conditions: list[Any] = [
            MemoryFactModel.scope_type == target.scope_type.value,
            (
                MemoryFactModel.canonical_subject_person_id == owners.subject_person_id
                if owners.subject_person_id is not None
                else MemoryFactModel.canonical_subject_person_id.is_(None)
            ),
            (
                MemoryFactModel.canonical_subject_space_id == owners.subject_space_id
                if owners.subject_space_id is not None
                else MemoryFactModel.canonical_subject_space_id.is_(None)
            ),
        ]
        if target.scope_type is MemoryScopeType.SELF:
            current_visibility = and_(
                MemoryFactModel.visibility_type
                == (target.visibility_type.value if target.visibility_type else ""),
                (
                    MemoryFactModel.canonical_visibility_person_id == owners.visibility_person_id
                    if owners.visibility_person_id is not None
                    else MemoryFactModel.canonical_visibility_person_id.is_(None)
                ),
                (
                    MemoryFactModel.canonical_visibility_space_id == owners.visibility_space_id
                    if owners.visibility_space_id is not None
                    else MemoryFactModel.canonical_visibility_space_id.is_(None)
                ),
            )
            conditions.append(
                or_(
                    and_(
                        MemoryFactModel.visibility_type == "global",
                        MemoryFactModel.canonical_visibility_person_id.is_(None),
                        MemoryFactModel.canonical_visibility_space_id.is_(None),
                    ),
                    current_visibility,
                )
            )
        else:
            conditions.extend(
                (
                    MemoryFactModel.visibility_type.is_(None),
                    MemoryFactModel.canonical_visibility_person_id.is_(None),
                    MemoryFactModel.canonical_visibility_space_id.is_(None),
                )
            )
        return tuple(conditions)

    @staticmethod
    def _exact_visibility_conditions() -> tuple[Any, ...]:
        return (
            MemoryFactModel.visibility_type.is_(None),
            MemoryFactModel.canonical_visibility_person_id.is_(None),
            MemoryFactModel.canonical_visibility_space_id.is_(None),
        )


class MemoryJobRepository:
    """Durable one-event extraction queue with bounded retries."""

    def __init__(
        self,
        database: Database,
        *,
        eligibility: MemoryEventEligibilityPolicy | None = None,
    ) -> None:
        self._database = database
        self._eligibility = eligibility or MemoryEventEligibilityPolicy()

    async def enqueue(self, event_id: int, conversation_key: str) -> bool:
        created, _partition = await self.enqueue_resolved(event_id, conversation_key)
        return created

    async def enqueue_resolved(
        self,
        event_id: int,
        conversation_key: str,
    ) -> tuple[bool, str]:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            event = await session.get(ChatEventModel, event_id)
            if event is None:
                logger.warning(
                    "memory_job_enqueue_skipped event_id=%d reason=event_not_found",
                    event_id,
                )
                return False, conversation_key[:255]
            from qq_ai_bot.identity.memory_guard import refuse_legacy_live_event

            if await refuse_legacy_live_event(session, event):
                logger.info(
                    "memory_job_enqueue_skipped event_id=%d reason=legacy_event_replay",
                    event_id,
                )
                return False, conversation_key[:255]
            if event.author_kind == AuthorKind.PERSON.value or event.author_person_id:
                if not event.author_person_id:
                    logger.info(
                        "memory_job_enqueue_skipped event_id=%d reason=author_person_missing",
                        event_id,
                    )
                    return False, conversation_key[:255]
                try:
                    owner = await resolve_active_person_id(session, event.sender_user_id)
                except MemoryPartitionResolutionError:
                    owner = None
                if owner != event.author_person_id:
                    logger.info(
                        "memory_job_enqueue_skipped event_id=%d reason=author_owner_mismatch",
                        event_id,
                    )
                    return False, conversation_key[:255]
            rejection_reason = self._eligibility.rejection_reason(_event_record(event))
            if rejection_reason is not None:
                logger.info(
                    "memory_job_enqueue_skipped event_id=%d reason=%s",
                    event_id,
                    rejection_reason,
                )
                return False, conversation_key[:255]
            partition = await resolve_memory_partition_for_event(session, event)
            if bool(partition.person_id) == bool(partition.space_id):
                raise MemoryPartitionResolutionError("owner_shape")
            person_id = partition.person_id
            space_id = partition.space_id
            stored_key = partition.value[:255]
            statement = insert(MemoryJobModel).values(
                event_id=event_id,
                conversation_key=stored_key,
                canonical_person_id=person_id,
                canonical_space_id=space_id,
                status=MemoryJobStatus.PENDING.value,
                attempts=0,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
                error_category=None,
                processing_source=MemoryProcessingSource.LIVE.value,
                outcome=None,
                completed_at=None,
            )
            result = await session.execute(
                statement.on_conflict_do_nothing(index_elements=[MemoryJobModel.event_id])
            )
            created = bool(cast(CursorResult[Any], result).rowcount)
            if not created:
                logger.debug(
                    "memory_job_enqueue_skipped event_id=%d reason=already_enqueued",
                    event_id,
                )
            return created, stored_key

    async def pending_count(self) -> int:
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(func.count())
                .select_from(MemoryJobModel)
                .where(
                    MemoryJobModel.status == MemoryJobStatus.PENDING.value,
                    MemoryJobModel.next_attempt_at <= datetime.now(UTC),
                )
            )
        return int(value or 0)

    async def batch_health(
        self, *, trigger_count: int, max_characters: int, max_wait_seconds: float
    ) -> dict[str, object]:
        """Content-free queue readiness; normal sub-hour accumulation is not blocked."""
        now = datetime.now(UTC)
        stale = now - timedelta(minutes=5)
        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        MemoryJobModel.canonical_person_id,
                        MemoryJobModel.canonical_space_id,
                        MemoryJobModel.status,
                        MemoryJobModel.attempts,
                        MemoryJobModel.next_attempt_at,
                        MemoryJobModel.created_at,
                        MemoryJobModel.updated_at,
                        (
                            func.length(ChatEventModel.content)
                            + func.length(ChatEventModel.audio_transcript)
                        ),
                        MemoryJobModel.error_category,
                    )
                    .join(ChatEventModel, ChatEventModel.id == MemoryJobModel.event_id)
                    .where(
                        MemoryJobModel.status.in_(
                            (
                                MemoryJobStatus.PENDING.value,
                                MemoryJobStatus.PROCESSING.value,
                                MemoryJobStatus.FAILED.value,
                            )
                        )
                    )
                )
            ).all()
        owners: dict[tuple[str | None, str | None], list[Any]] = {}
        failures: dict[str, int] = {}
        pending = processing = failed = stale_processing = invalid_owner = 0
        oldest_age = 0
        for row in rows:
            status, attempts = str(row[2]), int(row[3])
            created = row[5].replace(tzinfo=UTC)
            if status == MemoryJobStatus.PENDING.value:
                pending += 1
                oldest_age = max(oldest_age, max(0, int((now - created).total_seconds())))
            elif status == MemoryJobStatus.PROCESSING.value:
                processing += 1
                oldest_age = max(oldest_age, max(0, int((now - created).total_seconds())))
                stale_processing += int(row[6].replace(tzinfo=UTC) <= stale)
            else:
                failed += 1
                category = str(row[8] or "unknown")[:64]
                failures[category] = failures.get(category, 0) + 1
            if bool(row[0]) == bool(row[1]):
                invalid_owner += 1
                continue
            due = row[4].replace(tzinfo=UTC) <= now
            eligible = (status == MemoryJobStatus.PENDING.value and due) or (
                status == MemoryJobStatus.PROCESSING.value and row[6].replace(tzinfo=UTC) <= stale
            )
            if eligible:
                owners.setdefault((row[0], row[1]), []).append(
                    (attempts, created, int(row[7] or 0), status)
                )
        ready = sum(
            1
            for items in owners.values()
            if (
                any(
                    attempts > 0 or status == MemoryJobStatus.PROCESSING.value
                    for attempts, _created, _chars, status in items
                )
                or len(items) >= max(1, trigger_count)
                or sum(chars for _attempts, _created, chars, _status in items)
                >= max(1, max_characters)
                or any(
                    (now - created).total_seconds() >= max_wait_seconds
                    for _attempts, created, _chars, _status in items
                )
            )
        )
        return {
            "pending_events": pending,
            "processing_events": processing,
            "failed_events": failed,
            "stale_processing_events": stale_processing,
            "oldest_open_age_seconds": oldest_age,
            "ready_owner_count": ready,
            "waiting_owner_count": max(0, len(owners) - ready),
            "normal_waiting_is_blocked": False,
            "invalid_owner_count": invalid_owner,
            "failure_categories": dict(sorted(failures.items())),
        }

    async def claim(self, *, limit: int = 20) -> tuple[MemoryJob, ...]:
        now = datetime.now(UTC)
        stale = now - timedelta(minutes=5)
        prepared: list[PreparedJobClaim] = []
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(MemoryJobModel)
                    .where(
                        or_(
                            MemoryJobModel.status == MemoryJobStatus.PENDING.value,
                            (
                                (MemoryJobModel.status == MemoryJobStatus.PROCESSING.value)
                                & (MemoryJobModel.updated_at <= stale)
                            ),
                        ),
                        MemoryJobModel.next_attempt_at <= now,
                    )
                    .order_by(MemoryJobModel.id)
                    .limit(max(1, limit))
                )
            ).all()
            jobs: list[MemoryJob] = []
            from qq_ai_bot.identity.memory_guard import refuse_legacy_live_event

            for row in rows:
                prior = (row.id, row.status, row.updated_at)
                event = await session.get(ChatEventModel, row.event_id)
                if event is None:
                    prepared.append(PreparedJobClaim(*prior, None))
                    continue
                if (
                    row.processing_source == MemoryProcessingSource.LIVE.value
                    and await refuse_legacy_live_event(session, event)
                ):
                    prepared.append(
                        PreparedJobClaim(
                            *prior,
                            {
                                "status": MemoryJobStatus.FAILED.value,
                                "error_category": "legacy_event_replay",
                                "updated_at": now,
                            },
                        )
                    )
                    continue
                if row.processing_source == MemoryProcessingSource.LIVE.value:
                    if bool(row.canonical_person_id) == bool(row.canonical_space_id):
                        prepared.append(
                            PreparedJobClaim(
                                *prior,
                                {
                                    "status": MemoryJobStatus.FAILED.value,
                                    "error_category": "missing_canonical_owner",
                                    "updated_at": now,
                                },
                            )
                        )
                        continue
                prepared.append(
                    PreparedJobClaim(
                        *prior,
                        {"status": MemoryJobStatus.PROCESSING.value, "updated_at": now},
                        row.event_id
                        if row.processing_source == MemoryProcessingSource.LIVE.value
                        else None,
                    )
                )
                jobs.append(
                    MemoryJob(
                        id=row.id,
                        event_id=row.event_id,
                        conversation_key=row.conversation_key,
                        status=MemoryJobStatus.PROCESSING.value,
                        attempts=row.attempts,
                        next_attempt_at=row.next_attempt_at,
                        created_at=row.created_at,
                        updated_at=now,
                        error_category=row.error_category,
                        processing_source=row.processing_source,
                        rebuild_run_id=row.rebuild_run_id,
                        outcome=row.outcome,
                        completed_at=row.completed_at,
                        event=_event_record(event),
                    )
                )
        accepted = await commit_job_claims(self._database, MemoryJobModel, prepared)
        return tuple(job for job in jobs if job.id in accepted)

    async def claim_ready_batch(
        self,
        *,
        limit: int,
        trigger_count: int,
        max_characters: int,
        max_wait_seconds: float,
        now: datetime | None = None,
    ) -> tuple[MemoryJob, ...]:
        """Claim one ready conversation batch without mixing conversation scopes."""

        claimed_at = now or datetime.now(UTC)
        stale = claimed_at - timedelta(minutes=5)
        oldest_ready = claimed_at - timedelta(seconds=max_wait_seconds)
        eligible = and_(
            or_(
                MemoryJobModel.status == MemoryJobStatus.PENDING.value,
                (
                    (MemoryJobModel.status == MemoryJobStatus.PROCESSING.value)
                    & (MemoryJobModel.updated_at <= stale)
                ),
            ),
            MemoryJobModel.next_attempt_at <= claimed_at,
        )
        job_count = func.count(MemoryJobModel.id)
        recovery_count = func.sum(
            case(
                (
                    or_(
                        MemoryJobModel.attempts > 0,
                        MemoryJobModel.status == MemoryJobStatus.PROCESSING.value,
                    ),
                    1,
                ),
                else_=0,
            )
        )
        character_count = func.coalesce(
            func.sum(
                func.length(ChatEventModel.content) + func.length(ChatEventModel.audio_transcript)
            ),
            0,
        )
        oldest_job = func.min(MemoryJobModel.created_at)
        first_job_id = func.min(MemoryJobModel.id)
        xor_owner = or_(
            and_(
                MemoryJobModel.canonical_person_id.is_not(None),
                MemoryJobModel.canonical_space_id.is_(None),
            ),
            and_(
                MemoryJobModel.canonical_person_id.is_(None),
                MemoryJobModel.canonical_space_id.is_not(None),
            ),
        )
        prepared: list[PreparedJobClaim] = []
        async with self._database.sessions() as session:
            owner_ready = (
                await session.execute(
                    select(
                        MemoryJobModel.canonical_person_id,
                        MemoryJobModel.canonical_space_id,
                        first_job_id.label("first_job_id"),
                        case(
                            (recovery_count > 0, "recovery"),
                            (job_count >= max(1, trigger_count), "event_count"),
                            (character_count >= max(1, max_characters), "characters"),
                            else_="age",
                        ).label("batch_trigger"),
                    )
                    .join(ChatEventModel, ChatEventModel.id == MemoryJobModel.event_id)
                    .where(eligible, xor_owner)
                    .group_by(
                        MemoryJobModel.canonical_person_id,
                        MemoryJobModel.canonical_space_id,
                    )
                    .having(
                        or_(
                            recovery_count > 0,
                            job_count >= max(1, trigger_count),
                            character_count >= max(1, max_characters),
                            oldest_job <= oldest_ready,
                        )
                    )
                    .order_by(first_job_id)
                    .limit(1)
                )
            ).first()
            if owner_ready is None:
                return ()
            person_id = owner_ready[0]
            space_id = owner_ready[1]
            owner_filter = (
                and_(
                    MemoryJobModel.canonical_person_id == person_id,
                    MemoryJobModel.canonical_space_id.is_(None),
                )
                if person_id is not None
                else and_(
                    MemoryJobModel.canonical_space_id == space_id,
                    MemoryJobModel.canonical_person_id.is_(None),
                )
            )
            rows = (
                await session.scalars(
                    select(MemoryJobModel)
                    .where(eligible, owner_filter)
                    .order_by(MemoryJobModel.id)
                    .limit(max(1, limit))
                )
            ).all()
            jobs: list[MemoryJob] = []
            characters = 0
            from qq_ai_bot.identity.memory_guard import refuse_legacy_live_event

            for row in rows:
                prior = (row.id, row.status, row.updated_at)
                event = await session.get(ChatEventModel, row.event_id)
                if event is None:
                    prepared.append(PreparedJobClaim(*prior, None))
                    continue
                if (
                    row.processing_source == MemoryProcessingSource.LIVE.value
                    and await refuse_legacy_live_event(session, event)
                ):
                    prepared.append(
                        PreparedJobClaim(
                            *prior,
                            {
                                "status": MemoryJobStatus.FAILED.value,
                                "error_category": "legacy_event_replay",
                                "updated_at": claimed_at,
                            },
                        )
                    )
                    continue
                if row.processing_source == MemoryProcessingSource.LIVE.value:
                    if bool(row.canonical_person_id) == bool(row.canonical_space_id):
                        prepared.append(
                            PreparedJobClaim(
                                *prior,
                                {
                                    "status": MemoryJobStatus.FAILED.value,
                                    "error_category": "missing_canonical_owner",
                                    "updated_at": claimed_at,
                                },
                            )
                        )
                        continue
                event_characters = len(event.evidence_content)
                if jobs and characters + event_characters > max(1, max_characters):
                    break
                characters += event_characters
                prepared.append(
                    PreparedJobClaim(
                        *prior,
                        {"status": MemoryJobStatus.PROCESSING.value, "updated_at": claimed_at},
                        row.event_id
                        if row.processing_source == MemoryProcessingSource.LIVE.value
                        else None,
                    )
                )
                jobs.append(
                    MemoryJob(
                        id=row.id,
                        event_id=row.event_id,
                        conversation_key=row.conversation_key,
                        status=MemoryJobStatus.PROCESSING.value,
                        attempts=row.attempts,
                        next_attempt_at=row.next_attempt_at,
                        created_at=row.created_at,
                        updated_at=claimed_at,
                        error_category=row.error_category,
                        processing_source=row.processing_source,
                        rebuild_run_id=row.rebuild_run_id,
                        outcome=row.outcome,
                        completed_at=row.completed_at,
                        event=_event_record(event),
                        batch_trigger=str(owner_ready[3]),
                    )
                )
        accepted = await commit_job_claims(self._database, MemoryJobModel, prepared)
        return tuple(job for job in jobs if job.id in accepted)

    async def complete(
        self,
        job: MemoryJob,
        *,
        outcome: MemoryRebuildJobOutcome = MemoryRebuildJobOutcome.CLAIMS_APPLIED,
        result_category: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(MemoryJobModel)
                .where(*memory_job_claim_conditions(job))
                .values(
                    status=MemoryJobStatus.DONE.value,
                    updated_at=now,
                    error_category=(result_category[:64] if result_category else None),
                    outcome=outcome.value,
                    completed_at=now,
                )
            )
            if not getattr(result, "rowcount", 0):
                raise MemoryJobClaimLost(f"memory job {job.id} claim lost")

    async def fail(self, job: MemoryJob, error_category: str) -> None:
        now = datetime.now(UTC)
        attempts = job.attempts + 1
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(MemoryJobModel)
                .where(*memory_job_claim_conditions(job))
                .values(
                    attempts=attempts,
                    status=MemoryJobStatus.PENDING.value,
                    next_attempt_at=now + timedelta(seconds=30 * attempts),
                    updated_at=now,
                    error_category=error_category[:64],
                )
            )
            if not getattr(result, "rowcount", 0):
                raise MemoryJobClaimLost(f"memory job {job.id} claim lost")
