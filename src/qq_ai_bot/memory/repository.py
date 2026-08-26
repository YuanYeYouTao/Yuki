"""Persistence-only repositories for Memory V2 facts, evidence, and jobs."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.identity import AuthorKind
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
)
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
)
from qq_ai_bot.memory.partition import (
    MemoryPartitionResolutionError,
    resolve_active_person_id,
    resolve_active_space_id,
    resolve_fact_canonical_owners,
    resolve_memory_partition_for_event,
)
from qq_ai_bot.memory.projections import project_memory_fact_rows
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryActivationStateModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryFactRelationModel,
    MemoryFactStateEventModel,
    MemoryJobModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.persistence.repository_helpers import _event_record, keeper_event_clause

logger = logging.getLogger(__name__)

_EVIDENCE_SCAN_BATCH = 64


def _sql_fact_conversation_aligns() -> Any:
    fact = MemoryFactModel
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


def readable_evidence_count_expression() -> Any:
    """Correlated count of canonical readable evidence. Not a full-table scan."""

    live = and_(
        ChatEventModel.canonical_event_id.is_not(None),
        ChatEventModel.canonical_conversation_id.is_not(None),
        ChatEventModel.author_kind.is_not(None),
        keeper_event_clause(),
        _sql_fact_conversation_aligns(),
    )
    event_count = (
        select(func.count())
        .select_from(MemoryEvidenceModel)
        .join(ChatEventModel, ChatEventModel.id == MemoryEvidenceModel.event_id)
        .join(
            CanonicalConversationModel,
            CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
        )
        .where(MemoryEvidenceModel.fact_id == MemoryFactModel.id, live)
        .correlate(MemoryFactModel)
        .scalar_subquery()
    )
    receipt_count = (
        select(func.count())
        .select_from(MemoryEvidenceModel)
        .join(
            MemoryToolReceiptModel,
            MemoryToolReceiptModel.id == MemoryEvidenceModel.tool_receipt_id,
        )
        .join(ChatEventModel, ChatEventModel.id == MemoryToolReceiptModel.trigger_event_id)
        .join(
            CanonicalConversationModel,
            CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
        )
        .where(
            MemoryEvidenceModel.fact_id == MemoryFactModel.id,
            MemoryEvidenceModel.event_id.is_(None),
            live,
        )
        .correlate(MemoryFactModel)
        .scalar_subquery()
    )
    return event_count + receipt_count


def _initial_activation(fact: MemoryFactCreate) -> float:
    if fact.source_type.value == "explicit" or fact.authority is MemoryAuthority.EXPLICIT:
        return 0.95
    if fact.kind.value == "preference":
        return 0.80
    if fact.kind.value == "episode":
        return 0.75 if fact.importance >= 4 else 0.65
    return 0.70


class MemoryFactRepository:
    """Store and query facts without extraction or prompt logic."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    async def _execute_facts_with_count(
        self,
        session: AsyncSession,
        conditions: list[Any],
        *,
        order_by: tuple[Any, ...],
        limit: int | None = None,
    ) -> list[Any]:
        statement = select(MemoryFactModel, readable_evidence_count_expression()).where(*conditions)
        if order_by:
            statement = statement.order_by(*order_by)
        if limit is not None:
            statement = statement.limit(limit)
        return list((await session.execute(statement)).all())

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self._database.sessions() as session, session.begin():
            yield session

    async def count_active_for_create(self, fact: MemoryFactCreate) -> int:
        """Count current active facts in the exact target without mutating capacity."""

        return await self.count_active(
            MemoryFactQuery(
                scope_type=fact.scope_type,
                subject_user_id=fact.subject_user_id,
                group_id=fact.group_id,
                visibility_type=fact.visibility_type,
                visibility_user_id=fact.visibility_user_id,
                visibility_group_id=fact.visibility_group_id,
            )
        )

    async def list_facts(
        self,
        query: MemoryFactQuery,
        *,
        limit: int = 100,
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
            session, conditions, order_by=order, limit=max(1, limit)
        )
        return await project_memory_fact_rows(session, rows)

    async def list_person_facts_projected_to_group(
        self,
        user_id: str,
        group_id: str,
        *,
        limit: int = 100,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        """Project global person facts supported by that person's evidence in one group.

        This is a read-only visibility query.  It never changes the canonical fact scope,
        and deliberately requires an inbound event and self/explicit evidence from the
        target user in the current group.
        """

        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_person_facts_projected_to_group(
                    user_id,
                    group_id,
                    limit=limit,
                    session=owned,
                )
        try:
            person_id = await resolve_active_person_id(session, user_id)
            space_id = await resolve_active_space_id(session, group_id)
        except MemoryPartitionResolutionError:
            return ()
        qualifying_evidence = (
            select(MemoryEvidenceModel.id)
            .join(ChatEventModel, ChatEventModel.id == MemoryEvidenceModel.event_id)
            .join(
                CanonicalConversationModel,
                CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
            )
            .where(
                MemoryEvidenceModel.fact_id == MemoryFactModel.id,
                MemoryEvidenceModel.authority.in_(
                    (MemoryAuthority.SELF_REPORT.value, MemoryAuthority.EXPLICIT.value)
                ),
                ChatEventModel.direction == "inbound",
                ChatEventModel.author_person_id == person_id,
                ChatEventModel.author_kind.is_not(None),
                ChatEventModel.canonical_event_id.is_not(None),
                CanonicalConversationModel.space_id == space_id,
                keeper_event_clause(),
            )
            .correlate(MemoryFactModel)
            .exists()
        )
        rows = await self._execute_facts_with_count(
            session,
            [
                MemoryFactModel.scope_type == MemoryScopeType.PERSON.value,
                *(await self._person_subject_conditions(session, user_id)),
                MemoryFactModel.canonical_subject_space_id.is_(None),
                MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                MemoryFactModel.review_state != "quarantined",
                or_(
                    MemoryFactModel.valid_until.is_(None),
                    MemoryFactModel.valid_until > datetime.now(UTC),
                ),
                qualifying_evidence,
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
    ) -> tuple[MemoryFact, ...]:
        async with self._database.sessions() as session:
            rows = await self._execute_facts_with_count(
                session,
                [
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
    ) -> tuple[MemoryFact, ...]:
        if limit <= 0:
            return ()
        async with self._database.sessions() as session:
            rows = await self._execute_facts_with_count(
                session,
                [
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

    async def find_active(
        self,
        fact: MemoryFactCreate,
        *,
        session: AsyncSession,
    ) -> MemoryFactModel | None:
        conditions = [
            MemoryFactModel.scope_type == fact.scope_type.value,
            MemoryFactModel.memory_key == fact.memory_key,
            MemoryFactModel.status == MemoryStatus.ACTIVE.value,
            *(await self._query_identity_conditions(session, fact)),
        ]
        if fact.scope_type is not MemoryScopeType.SELF:
            conditions.append(MemoryFactModel.kind == fact.kind.value)
        return cast(
            MemoryFactModel | None,
            await session.scalar(select(MemoryFactModel).where(*conditions)),
        )

    async def create_fact(
        self,
        fact: MemoryFactCreate,
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
        session.add(
            MemoryActivationStateModel(
                fact_id=row.id,
                activation=_initial_activation(fact),
                activation_updated_at=now,
                last_recalled_at=None,
                recall_count=0,
                revision=0,
            )
        )
        await session.flush()
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
        authority: str,
        confidence: float,
        confirmed_at: datetime,
        session: AsyncSession,
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
                authority=authority,
                confidence=confidence,
                last_confirmed_at=max(previous, confirmed_at),
                updated_at=datetime.now(UTC),
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
        result = await session.execute(
            statement.on_conflict_do_nothing(
                index_elements=[
                    MemoryFactRelationModel.source_fact_id,
                    MemoryFactRelationModel.target_fact_id,
                    MemoryFactRelationModel.relation_type,
                ]
            )
        )
        return bool(cast(CursorResult[Any], result).rowcount)

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
            if receipt is None or receipt.trigger_event_id is None:
                return False
            trigger = await session.get(ChatEventModel, receipt.trigger_event_id)
            if trigger is None or not await v2_evidence_event_chain_readable(
                session, fact_row, trigger
            ):
                return False
        statement = insert(MemoryEvidenceModel).values(
            fact_id=fact_id,
            event_id=evidence.event_id,
            tool_receipt_id=evidence.tool_receipt_id,
            source_speaker_user_id=evidence.source_speaker_user_id,
            relation=evidence.relation.value,
            confidence=evidence.confidence,
            authority=evidence.authority.value,
            excerpt=evidence.excerpt[:500],
            created_at=datetime.now(UTC),
        )
        index_elements = (
            [MemoryEvidenceModel.fact_id, MemoryEvidenceModel.event_id]
            if evidence.event_id is not None
            else [MemoryEvidenceModel.fact_id, MemoryEvidenceModel.tool_receipt_id]
        )
        result = await session.execute(
            statement.on_conflict_do_nothing(index_elements=index_elements)
        )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def list_evidence(
        self,
        fact_id: int,
        *,
        limit: int = 100,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryEvidence, ...]:
        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_evidence(fact_id, limit=limit, session=owned)
        bound = max(1, limit)
        from qq_ai_bot.identity.memory_guard import v2_evidence_row_readable

        fact_row = await session.get(MemoryFactModel, fact_id)
        if fact_row is None:
            return ()
        readable: list[MemoryEvidenceModel] = []
        last_created_at: datetime | None = None
        last_id: int | None = None
        while len(readable) < bound:
            conditions = [MemoryEvidenceModel.fact_id == fact_id]
            if last_created_at is not None and last_id is not None:
                conditions.append(
                    or_(
                        MemoryEvidenceModel.created_at < last_created_at,
                        and_(
                            MemoryEvidenceModel.created_at == last_created_at,
                            MemoryEvidenceModel.id < last_id,
                        ),
                    )
                )
            batch = (
                await session.scalars(
                    select(MemoryEvidenceModel)
                    .where(*conditions)
                    .order_by(
                        MemoryEvidenceModel.created_at.desc(),
                        MemoryEvidenceModel.id.desc(),
                    )
                    .limit(_EVIDENCE_SCAN_BATCH)
                )
            ).all()
            if not batch:
                break
            for row in batch:
                last_created_at = row.created_at
                last_id = int(row.id)
                if await v2_evidence_row_readable(session, fact_row, row):
                    readable.append(row)
                    if len(readable) >= bound:
                        break
            if len(batch) < _EVIDENCE_SCAN_BATCH:
                break
        rows = readable
        return tuple(
            MemoryEvidence(
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
            for row in rows
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

    async def list_lifecycle_candidates(
        self,
        *,
        now: datetime,
        automatic_cutoff: datetime,
        third_party_cutoff: datetime,
        contested_cutoff: datetime,
        max_importance: int,
        max_confidence: float,
        limit: int,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        stale_window = or_(
            (
                (MemoryFactModel.authority == "third_party")
                & (MemoryFactModel.last_confirmed_at <= third_party_cutoff)
            ),
            (
                (MemoryFactModel.status == MemoryStatus.CONTESTED.value)
                & (MemoryFactModel.last_confirmed_at <= contested_cutoff)
            ),
            (
                (MemoryFactModel.authority != "third_party")
                & (MemoryFactModel.status != MemoryStatus.CONTESTED.value)
                & (MemoryFactModel.last_confirmed_at <= automatic_cutoff)
            ),
        )
        conditions = [
            MemoryFactModel.status.in_((MemoryStatus.ACTIVE.value, MemoryStatus.CONTESTED.value)),
            MemoryFactModel.review_state != "quarantined",
            or_(
                MemoryFactModel.valid_until <= now,
                and_(
                    MemoryFactModel.source_type != "explicit",
                    MemoryFactModel.authority != "explicit",
                    MemoryFactModel.scope_type != MemoryScopeType.SELF.value,
                    MemoryFactModel.source_type == "automatic",
                    MemoryFactModel.importance <= max_importance,
                    MemoryFactModel.confidence <= max_confidence,
                    stale_window,
                ),
            ),
        ]
        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_lifecycle_candidates(
                    now=now,
                    automatic_cutoff=automatic_cutoff,
                    third_party_cutoff=third_party_cutoff,
                    contested_cutoff=contested_cutoff,
                    max_importance=max_importance,
                    max_confidence=max_confidence,
                    limit=limit,
                    session=owned,
                )
        rows = await self._execute_facts_with_count(
            session,
            conditions,
            order_by=(MemoryFactModel.valid_until.asc(), MemoryFactModel.id),
            limit=max(1, limit),
        )
        return await project_memory_fact_rows(session, rows)

    async def count_active(
        self,
        query: MemoryFactQuery,
        *,
        session: AsyncSession | None = None,
    ) -> int:
        return len(await self.list_facts(query, limit=100_000, session=session))

    async def make_room(
        self,
        query: MemoryFactQuery,
        *,
        limit: int,
        session: AsyncSession,
    ) -> bool:
        """Invalidate the least useful automatic fact when a scope is full."""

        if await self.count_active(query, session=session) < max(1, limit):
            return True
        conditions = [
            MemoryFactModel.scope_type == query.scope_type.value,
            MemoryFactModel.status == MemoryStatus.ACTIVE.value,
            MemoryFactModel.source_type != "explicit",
            *(await self._query_identity_conditions(session, query)),
        ]
        row = await session.scalar(
            select(MemoryFactModel)
            .where(*conditions)
            .order_by(MemoryFactModel.importance.asc(), MemoryFactModel.updated_at.asc())
            .limit(1)
        )
        if row is None:
            return False
        prior_conflict = row.conflict_state
        row.status = MemoryStatus.INVALIDATED.value
        row.conflict_state = MemoryConflictState.CLEAR.value
        row.invalidated_reason = MemoryInvalidationReason.STALE.value
        row.updated_at = datetime.now(UTC)
        session.add(
            MemoryFactStateEventModel(
                fact_id=row.id,
                action=MemoryStateAction.STALE_INVALIDATED.value,
                from_status=MemoryStatus.ACTIVE.value,
                to_status=MemoryStatus.INVALIDATED.value,
                from_conflict_state=prior_conflict,
                to_conflict_state=MemoryConflictState.CLEAR.value,
                reason_code="capacity_retention",
                source_event_id=None,
                actor_user_id=None,
                created_at=datetime.now(UTC),
            )
        )
        await session.flush()
        return True

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

    async def claim(self, *, limit: int = 20) -> tuple[MemoryJob, ...]:
        now = datetime.now(UTC)
        stale = now - timedelta(minutes=5)
        async with self._database.sessions() as session, session.begin():
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
                event = await session.get(ChatEventModel, row.event_id)
                if event is None:
                    await session.delete(row)
                    continue
                if (
                    row.processing_source == MemoryProcessingSource.LIVE.value
                    and await refuse_legacy_live_event(session, event)
                ):
                    row.status = MemoryJobStatus.FAILED.value
                    row.error_category = "legacy_event_replay"
                    row.updated_at = now
                    continue
                if row.processing_source == MemoryProcessingSource.LIVE.value:
                    if bool(row.canonical_person_id) == bool(row.canonical_space_id):
                        row.status = MemoryJobStatus.FAILED.value
                        row.error_category = "missing_canonical_owner"
                        row.updated_at = now
                        continue
                row.status = MemoryJobStatus.PROCESSING.value
                row.updated_at = now
                jobs.append(
                    MemoryJob(
                        id=row.id,
                        event_id=row.event_id,
                        conversation_key=row.conversation_key,
                        status=row.status,
                        attempts=row.attempts,
                        next_attempt_at=row.next_attempt_at,
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                        error_category=row.error_category,
                        processing_source=row.processing_source,
                        rebuild_run_id=row.rebuild_run_id,
                        outcome=row.outcome,
                        completed_at=row.completed_at,
                        event=_event_record(event),
                    )
                )
            return tuple(jobs)

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
        character_count = func.coalesce(func.sum(func.length(ChatEventModel.content)), 0)
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
        async with self._database.sessions() as session, session.begin():
            owner_ready = (
                await session.execute(
                    select(
                        MemoryJobModel.canonical_person_id,
                        MemoryJobModel.canonical_space_id,
                        first_job_id.label("first_job_id"),
                    )
                    .join(ChatEventModel, ChatEventModel.id == MemoryJobModel.event_id)
                    .where(eligible, xor_owner)
                    .group_by(
                        MemoryJobModel.canonical_person_id,
                        MemoryJobModel.canonical_space_id,
                    )
                    .having(
                        or_(
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
                event = await session.get(ChatEventModel, row.event_id)
                if event is None:
                    await session.delete(row)
                    continue
                if (
                    row.processing_source == MemoryProcessingSource.LIVE.value
                    and await refuse_legacy_live_event(session, event)
                ):
                    row.status = MemoryJobStatus.FAILED.value
                    row.error_category = "legacy_event_replay"
                    row.updated_at = claimed_at
                    continue
                if row.processing_source == MemoryProcessingSource.LIVE.value:
                    if bool(row.canonical_person_id) == bool(row.canonical_space_id):
                        row.status = MemoryJobStatus.FAILED.value
                        row.error_category = "missing_canonical_owner"
                        row.updated_at = claimed_at
                        continue
                event_characters = len(event.content)
                if jobs and characters + event_characters > max(1, max_characters):
                    break
                characters += event_characters
                row.status = MemoryJobStatus.PROCESSING.value
                row.updated_at = claimed_at
                jobs.append(
                    MemoryJob(
                        id=row.id,
                        event_id=row.event_id,
                        conversation_key=row.conversation_key,
                        status=row.status,
                        attempts=row.attempts,
                        next_attempt_at=row.next_attempt_at,
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                        error_category=row.error_category,
                        processing_source=row.processing_source,
                        rebuild_run_id=row.rebuild_run_id,
                        outcome=row.outcome,
                        completed_at=row.completed_at,
                        event=_event_record(event),
                    )
                )
            return tuple(jobs)

    async def complete(
        self,
        job_id: int,
        *,
        outcome: MemoryRebuildJobOutcome = MemoryRebuildJobOutcome.CLAIMS_APPLIED,
        result_category: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryJobModel)
                .where(MemoryJobModel.id == job_id)
                .values(
                    status=MemoryJobStatus.DONE.value,
                    updated_at=now,
                    error_category=(result_category[:64] if result_category else None),
                    outcome=outcome.value,
                    completed_at=now,
                )
            )

    async def fail(self, job_id: int, error_category: str) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(MemoryJobModel, job_id)
            if row is None:
                return
            row.attempts += 1
            row.status = (
                MemoryJobStatus.FAILED.value if row.attempts >= 3 else MemoryJobStatus.PENDING.value
            )
            row.next_attempt_at = now + timedelta(seconds=30 * row.attempts)
            row.updated_at = now
            row.error_category = error_category[:64]
