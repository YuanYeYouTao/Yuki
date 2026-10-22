"""Repositories for the permanent event ledger and event state."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

from aiosqlite import Connection as SQLiteConnection
from sqlalchemy import Integer, and_, column, func, or_, select, table, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.hydrate import conversation_for_scope
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.identity.db_models import CanonicalPersonModel
from yuki.memory.eligibility import MemoryEventEligibilityPolicy
from yuki.memory.rebuild.models import (
    MemoryRebuildPlanStatistics,
    MemoryRebuildSelection,
)
from yuki.persistence.database import Database
from yuki.persistence.history_search import (
    MatchMode,
    compile_match,
    match_fields,
    prepare_query,
)
from yuki.persistence.models import (
    ChatEventModel,
    ConversationMediaItemModel,
    MemoryJobModel,
)
from yuki.persistence.repository_helpers import (
    _event_record,
    keeper_event_clause,
)
from yuki.persistence.repository_records import (
    EventRecord,
    HistoryPage,
)
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


async def _history_read(session: AsyncSession, statement: Any, *, single: bool = False) -> Any:
    """Interrupt this read before its driver/transaction is returned on cancellation."""
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    driver = cast(SQLiteConnection, raw.driver_connection)
    operation: asyncio.Task[Any] = asyncio.create_task(
        session.scalar(statement) if single else session.scalars(statement)
    )
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await driver.interrupt()
        try:
            await operation
        except OperationalError:
            pass  # SQLITE_INTERRUPT belongs to the cancelled read, not a retry.
        raise


async def _conversation_id_for_scope(session: AsyncSession, scope: ConversationScope) -> str | None:
    conversation = await conversation_for_scope(session, scope)
    return conversation.id if conversation else None


@dataclass(frozen=True, slots=True)
class ConversationReadVersion:
    scope: ConversationScope
    conversation_id: str | None
    generation: int
    starts_after_event_id: int
    prompt_source_revision: int = 0
    visible_event_ids: Collection[int] = field(default=(), compare=False)
    selected_summary_text: str | None = field(default=None, compare=False)
    observation_sources: tuple[tuple[str, int], ...] = field(default=(), compare=False)
    observation_actor_id: str = field(default="", compare=False)
    observation_read_scope: str = field(default="", compare=False)


async def _read_version(
    session: AsyncSession, scope: ConversationScope, *, conversation_id: str | None = None
) -> ConversationReadVersion:
    row = (
        await session.get(CanonicalConversationModel, conversation_id)
        if conversation_id
        else await conversation_for_scope(session, scope)
    )
    if row is None:
        return ConversationReadVersion(scope, None, 0, 0)
    return ConversationReadVersion(
        scope, row.id, row.generation, row.starts_after_event_id, row.prompt_source_revision
    )


class EventLedgerRepository:
    """Append, query, search, and forget permanent raw chat events."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._memory_eligibility = MemoryEventEligibilityPolicy()
        self._writer = ScopedEventLedgerUnitOfWork(database)

    def set_scoped_writer(self, writer: ScopedEventLedgerUnitOfWork) -> None:
        self._writer = writer

    async def maximum_event_id(self, *, session: AsyncSession | None = None) -> int:
        from yuki.persistence.unit_of_work import optional_session

        async with optional_session(self._database, session, write=False) as active:
            return int(await active.scalar(select(func.max(ChatEventModel.id))) or 0)

    def _rebuild_conditions(
        self,
        selection: MemoryRebuildSelection,
        *,
        snapshot_max_event_id: int,
        eligible_only: bool,
    ) -> tuple[Any, ...]:
        conditions: list[Any] = [ChatEventModel.id <= snapshot_max_event_id]
        if selection.event_ids is not None:
            selected_events = func.json_each(json.dumps(selection.event_ids)).table_valued("value")
            conditions.append(ChatEventModel.id.in_(select(selected_events.c.value)))
        if selection.presence_ids:
            conditions.append(ChatEventModel.ingress_presence_id.in_(selection.presence_ids))
        if selection.scope_types:
            conditions.append(
                ChatEventModel.scope_type.in_(tuple(scope.value for scope in selection.scope_types))
            )
        if selection.person_ids:
            selected = aliased(CanonicalPersonModel)
            family = aliased(CanonicalPersonModel)
            conditions.append(
                ChatEventModel.author_person_id.in_(
                    select(family.id).where(
                        func.coalesce(family.merged_into_person_id, family.id).in_(
                            select(
                                func.coalesce(selected.merged_into_person_id, selected.id)
                            ).where(selected.id.in_(selection.person_ids))
                        )
                    )
                )
            )
        if selection.space_ids:
            conditions.append(
                ChatEventModel.canonical_conversation_id.in_(
                    select(CanonicalConversationModel.id).where(
                        CanonicalConversationModel.space_id.in_(selection.space_ids)
                    )
                )
            )
        if selection.conversation_ids:
            conditions.append(
                ChatEventModel.canonical_conversation_id.in_(selection.conversation_ids)
            )
        if selection.after is not None:
            conditions.append(ChatEventModel.occurred_at >= selection.after)
        if selection.before is not None:
            conditions.append(ChatEventModel.occurred_at <= selection.before)
        if selection.minimum_event_id is not None:
            conditions.append(ChatEventModel.id >= selection.minimum_event_id)
        if selection.maximum_event_id is not None:
            conditions.append(ChatEventModel.id <= selection.maximum_event_id)
        if eligible_only:
            conditions.extend(
                self._memory_eligibility.sql_conditions(
                    include_failed_live_jobs=selection.include_failed_live_jobs
                )
            )
        return tuple(conditions)

    async def count_rebuild_candidates(
        self,
        selection: MemoryRebuildSelection,
        *,
        snapshot_max_event_id: int,
        session: AsyncSession | None = None,
    ) -> MemoryRebuildPlanStatistics:
        base = self._rebuild_conditions(
            selection,
            snapshot_max_event_id=snapshot_max_event_id,
            eligible_only=False,
        )
        eligible = self._rebuild_conditions(
            selection,
            snapshot_max_event_id=snapshot_max_event_id,
            eligible_only=True,
        )
        candidate = (
            select(
                ChatEventModel.id,
                ChatEventModel.scope_type,
                ChatEventModel.content,
                ChatEventModel.occurred_at,
            )
            .where(*eligible)
            .order_by(ChatEventModel.occurred_at.asc(), ChatEventModel.id.asc())
        )
        if selection.maximum_events is not None:
            candidate = candidate.limit(selection.maximum_events)
        candidate_rows = candidate.subquery()
        from yuki.persistence.unit_of_work import optional_session

        async with optional_session(self._database, session, write=False) as active:
            matched = int(
                await active.scalar(select(func.count()).select_from(ChatEventModel).where(*base))
                or 0
            )
            summary = (
                await active.execute(
                    select(
                        func.count(candidate_rows.c.id),
                        func.sum(func.length(candidate_rows.c.content)),
                        func.sum(func.iif(candidate_rows.c.scope_type == "private", 1, 0)),
                        func.sum(func.iif(candidate_rows.c.scope_type == "group", 1, 0)),
                        func.min(candidate_rows.c.occurred_at),
                        func.max(candidate_rows.c.occurred_at),
                    )
                )
            ).one()
            status_result = (
                await active.execute(
                    select(MemoryJobModel.status, func.count())
                    .join(ChatEventModel, ChatEventModel.id == MemoryJobModel.event_id)
                    .where(*base)
                    .group_by(MemoryJobModel.status)
                )
            ).all()
            status_rows: dict[str, int] = {
                str(status): int(count) for status, count in status_result
            }
        eligible_count = int(summary[0] or 0)
        return MemoryRebuildPlanStatistics(
            matched_events=matched,
            eligible_events=eligible_count,
            already_processed=int(status_rows.get("done", 0)),
            live_pending_processing=int(status_rows.get("pending", 0))
            + int(status_rows.get("processing", 0)),
            failed_live_jobs=int(status_rows.get("failed", 0)),
            private_events=int(summary[2] or 0),
            group_events=int(summary[3] or 0),
            input_characters=int(summary[1] or 0),
            earliest_event=summary[4],
            latest_event=summary[5],
            estimated_extraction_requests=eligible_count,
        )

    async def list_rebuild_candidates(
        self,
        selection: MemoryRebuildSelection,
        *,
        snapshot_max_event_id: int,
        after_occurred_at: datetime | None,
        after_event_id: int | None,
        limit: int,
    ) -> tuple[EventRecord, ...]:
        conditions = list(
            self._rebuild_conditions(
                selection,
                snapshot_max_event_id=snapshot_max_event_id,
                eligible_only=True,
            )
        )
        if after_occurred_at is not None and after_event_id is not None:
            conditions.append(
                or_(
                    ChatEventModel.occurred_at > after_occurred_at,
                    and_(
                        ChatEventModel.occurred_at == after_occurred_at,
                        ChatEventModel.id > after_event_id,
                    ),
                )
            )
        bounded_limit = limit
        if selection.maximum_events is not None:
            bounded_limit = min(limit, selection.maximum_events)
        async with self._database.read_session() as session:
            rows = (
                await session.scalars(
                    select(ChatEventModel)
                    .where(*conditions)
                    .order_by(ChatEventModel.occurred_at.asc(), ChatEventModel.id.asc())
                    .limit(bounded_limit)
                )
            ).all()
        return tuple(_event_record(row) for row in rows)

    async def find_by_platform_message(
        self,
        *,
        bot_user_id: str,
        platform_message_id: str,
    ) -> EventRecord | None:
        """Return one exact locally observed event without widening its conversation scope."""

        async with self._database.read_session() as session:
            row = await session.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.bot_user_id == bot_user_id,
                    ChatEventModel.platform_message_id == platform_message_id,
                )
            )
        return _event_record(row) if row is not None else None

    async def get_event(self, event_id: int) -> EventRecord | None:
        """Return one exact ledger event by its internal immutable identifier."""

        async with self._database.read_session() as session:
            row = await session.get(ChatEventModel, event_id)
        return _event_record(row) if row is not None else None

    async def get_reply_event(self, event_id: int, *, conversation_id: str) -> EventRecord | None:
        """Read a resolved reply only from its canonical conversation's live messages."""
        async with self._database.read_session() as session:
            row = await session.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.id == event_id,
                    ChatEventModel.canonical_conversation_id == conversation_id,
                    ChatEventModel.event_kind == "message",
                    keeper_event_clause(),
                )
            )
        return _event_record(row) if row is not None else None

    async def read_scope_context(
        self, scope: ConversationScope, *, limit: int, message_only: bool = False
    ) -> tuple[ConversationReadVersion, tuple[EventRecord, ...]]:
        """Read the current generation, retaining its version for dispatch validation."""
        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            version = await _read_version(session, scope)
            if version.conversation_id is None:
                return version, ()
            query = select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == version.conversation_id,
                keeper_event_clause(),
                ChatEventModel.id > version.starts_after_event_id,
            )
            if message_only:
                query = query.where(ChatEventModel.event_kind == "message")
            rows = list(
                (await session.scalars(query.order_by(ChatEventModel.id.desc()).limit(limit))).all()
            )
        return version, tuple(_event_record(row) for row in reversed(rows))

    async def read_version_matches(self, version: ConversationReadVersion) -> bool:
        async with self._database.read_session() as session:
            if (
                await _read_version(session, version.scope, conversation_id=version.conversation_id)
                != version
            ):
                return False
            if version.observation_sources:
                from yuki.conversation.observations import validate_observations

                return await validate_observations(
                    session,
                    version.conversation_id or "",
                    version.generation,
                    version.observation_actor_id,
                    version.observation_read_scope,
                    version.observation_sources,
                )
            return True

    async def read_scope_delta(
        self, scope: ConversationScope, *, after_event_id: int, limit: int = 128
    ) -> tuple[ConversationReadVersion, tuple[EventRecord, ...]]:
        """Earliest new chat events and their generation from one read snapshot."""
        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            version = await _read_version(session, scope)
            if version.conversation_id is None:
                return version, ()
            rows = (
                await session.scalars(
                    select(ChatEventModel)
                    .where(
                        ChatEventModel.canonical_conversation_id == version.conversation_id,
                        keeper_event_clause(),
                        ChatEventModel.event_kind == "message",
                        ChatEventModel.id > max(after_event_id, version.starts_after_event_id),
                    )
                    .order_by(ChatEventModel.id)
                    .limit(max(1, limit))
                )
            ).all()
            return version, tuple(_event_record(row) for row in rows)

    async def list_scope_recent(
        self,
        scope: ConversationScope,
        *,
        after_event_id: int = 0,
        limit: int,
        message_only: bool = False,
    ) -> tuple[EventRecord, ...]:
        """Read the newest events from the canonical Conversation behind an alias."""

        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            version = await _read_version(session, scope)
            if version.conversation_id is None:
                return ()
            query = select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == version.conversation_id,
                keeper_event_clause(),
                ChatEventModel.id > max(0, after_event_id, version.starts_after_event_id),
            )
            if message_only:
                query = query.where(ChatEventModel.event_kind == "message")
            rows = list(
                (await session.scalars(query.order_by(ChatEventModel.id.desc()).limit(limit))).all()
            )
        rows.reverse()
        return tuple(_event_record(row) for row in rows)

    async def list_recent_activity(
        self,
        scope: ConversationScope,
        *,
        conversation_id: str | None = None,
        limit: int,
    ) -> tuple[tuple[str, str | None, datetime], ...]:
        """Read the same recent keeper window without bodies or attachment JSON."""

        query = (
            select(
                ChatEventModel.event_kind, ChatEventModel.author_kind, ChatEventModel.occurred_at
            )
            .join(
                CanonicalConversationModel,
                CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
            )
            .where(
                keeper_event_clause(),
                ChatEventModel.id > CanonicalConversationModel.starts_after_event_id,
            )
        )
        async with self._database.read_session() as session:
            conversation_id = conversation_id or await _conversation_id_for_scope(session, scope)
            query = query.where(CanonicalConversationModel.id == conversation_id)
            rows = (
                (await session.execute(query.order_by(ChatEventModel.id.desc()).limit(limit)))
                .tuples()
                .all()
            )
        return tuple(reversed(rows))

    async def list_scope_before(
        self,
        scope: ConversationScope,
        *,
        before_event_id: int,
        limit: int,
        message_only: bool = False,
    ) -> tuple[EventRecord, ...]:
        """Return the bounded scope prefix preceding one ledger event id."""

        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            version = await _read_version(session, scope)
            if version.conversation_id is None:
                return ()
            query = select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == version.conversation_id,
                keeper_event_clause(),
                ChatEventModel.id < before_event_id,
                ChatEventModel.id > version.starts_after_event_id,
            )
            if message_only:
                query = query.where(ChatEventModel.event_kind == "message")
            rows = list(
                (
                    await session.scalars(
                        query.order_by(ChatEventModel.id.desc()).limit(max(1, limit))
                    )
                ).all()
            )
        rows.reverse()
        return tuple(_event_record(row) for row in rows)

    async def list_scope_after(
        self,
        scope: ConversationScope,
        *,
        after_event_id: int,
        through_event_id: int | None = None,
        limit: int,
        message_only: bool = False,
    ) -> tuple[EventRecord, ...]:
        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            version = await _read_version(session, scope)
            if version.conversation_id is None:
                return ()
            query = select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == version.conversation_id,
                keeper_event_clause(),
                ChatEventModel.id > max(after_event_id, version.starts_after_event_id),
            )
            if message_only:
                query = query.where(ChatEventModel.event_kind == "message")
            if through_event_id is not None:
                query = query.where(ChatEventModel.id <= through_event_id)
            rows = list(
                (
                    await session.scalars(
                        query.order_by(ChatEventModel.id.asc()).limit(max(1, limit))
                    )
                ).all()
            )
        return tuple(_event_record(row) for row in rows)

    async def read_scope_missing_history(
        self,
        expected: ConversationReadVersion,
        *,
        after_event_id: int,
        through_event_id: int,
        frozen_event_ids: frozenset[int],
        current_event_id: int | None,
    ) -> tuple[ConversationReadVersion, tuple[EventRecord, ...]]:
        """Discover every eligible ID, loading only missing bodies in one snapshot.

        The exclusion set only avoids repeated body work. It never proves that
        the range contains no other events or replaces publication source CAS.
        A changed version returns no bodies so the caller can reject its plan.
        """
        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            version = await _read_version(
                session, expected.scope, conversation_id=expected.conversation_id
            )
            if version != expected or version.conversation_id is None:
                return version, ()
            lower = max(after_event_id, version.starts_after_event_id)
            conditions = (
                ChatEventModel.canonical_conversation_id == version.conversation_id,
                keeper_event_clause(),
                ChatEventModel.event_kind == "message",
                ChatEventModel.id > lower,
                ChatEventModel.id <= through_event_id,
            )
            after = lower
            records: list[EventRecord] = []
            while after < through_event_id:
                identities = (
                    await session.scalars(
                        select(ChatEventModel.id)
                        .where(*conditions, ChatEventModel.id > after)
                        .order_by(ChatEventModel.id)
                        .limit(256)
                    )
                ).all()
                if not identities:
                    break
                missing = [
                    identity
                    for identity in identities
                    if identity not in frozen_event_ids and identity != current_event_id
                ]
                if missing:
                    rows = (
                        await session.scalars(
                            select(ChatEventModel)
                            .where(*conditions, ChatEventModel.id.in_(missing))
                            .order_by(ChatEventModel.id)
                            .limit(256)
                        )
                    ).all()
                    records.extend(_event_record(item) for item in rows)
                # An excluded-only page must still advance to the next unseen ID.
                after = identities[-1]
            return version, tuple(records)

    async def count_scope_range(
        self,
        scope: ConversationScope,
        *,
        after_event_id: int,
        through_event_id: int,
    ) -> int:
        async with self._database.read_session() as session:
            conversation_id = await _conversation_id_for_scope(session, scope)
            if conversation_id is None:
                return 0
            return int(
                await session.scalar(
                    select(func.count(ChatEventModel.id)).where(
                        ChatEventModel.canonical_conversation_id == conversation_id,
                        keeper_event_clause(),
                        ChatEventModel.id > after_event_id,
                        ChatEventModel.id <= through_event_id,
                    )
                )
                or 0
            )

    async def maximum_scope_event_id(self, scope: ConversationScope) -> int:
        async with self._database.read_session() as session:
            conversation_id = await _conversation_id_for_scope(session, scope)
            if conversation_id is None:
                return 0
            return int(
                await session.scalar(
                    select(func.max(ChatEventModel.id)).where(
                        ChatEventModel.canonical_conversation_id == conversation_id,
                        keeper_event_clause(),
                    )
                )
                or 0
            )

    @staticmethod
    def _history_conditions(
        conversation_id: str | tuple[str, ...], event_kind: str, predicates: Collection[Any]
    ) -> list[Any]:
        conditions = [
            ChatEventModel.canonical_conversation_id.in_(conversation_id)
            if isinstance(conversation_id, tuple)
            else ChatEventModel.canonical_conversation_id == conversation_id,
            keeper_event_clause(),
            *predicates,
        ]
        if event_kind not in ("message", "external_event", "all"):
            raise ValueError("event_kind must be message, external_event, or all")
        if event_kind != "all":
            conditions.append(ChatEventModel.event_kind == event_kind)
        return conditions

    async def query_history(
        self,
        *,
        conversation_id: str | tuple[str, ...],
        session: AsyncSession | None = None,
        order: str = "recent",
        limit: int = 20,
        cursor: int | None = None,
        before_event_id: int | None = None,
        after_event_id: int | None = None,
        event_kind: str = "message",
        person_id: str | None = None,
        author_kind: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        has: Collection[str] = (),
        keyword: str = "",
        match: MatchMode = "all",
        exclude: tuple[str, ...] = (),
        context_before: int = 0,
        context_after: int = 0,
        predicates: Collection[Any] = (),
    ) -> HistoryPage:
        """Read exact matches and neighbors in one snapshot; never acquire a writer."""
        from yuki.identity.person_identity import person_family_ids
        from yuki.persistence.unit_of_work import optional_session

        if order not in ("recent", "oldest"):
            raise ValueError("order must be recent or oldest")
        if limit <= 0 or context_before < 0 or context_after < 0:
            raise ValueError("limit must be positive and context counts nonnegative")
        query = (
            await asyncio.to_thread(prepare_query, keyword, match, exclude)
            if keyword or exclude
            else prepare_query(keyword, match, exclude)
        )
        metadata_candidates = bool(start_at or end_at or has)
        lower = after_event_id or 0
        upper = before_event_id
        if cursor is not None:
            if order == "recent":
                upper = cursor if upper is None else min(upper, cursor)
            else:
                lower = max(lower, cursor)
        conditions = self._history_conditions(conversation_id, event_kind, predicates)
        if author_kind is not None:
            conditions.append(ChatEventModel.author_kind == author_kind)
        for stamp, comparison in ((start_at, "start"), (end_at, "end")):
            if stamp is not None:
                aware = (
                    stamp
                    if stamp.tzinfo is not None
                    else stamp.replace(tzinfo=timezone(timedelta(hours=8)))
                )
                utc = aware.astimezone(UTC).replace(tzinfo=None)
                conditions.append(
                    ChatEventModel.occurred_at >= utc
                    if comparison == "start"
                    else ChatEventModel.occurred_at < utc
                )
        if has:
            kinds = tuple(
                dict.fromkeys(
                    item
                    for kind in has
                    for item in (("audio", "record") if kind == "audio" else (kind,))
                )
            )
            # The index retains platform domain classification; record is audio.
            conditions.append(
                ChatEventModel.id.in_(
                    select(ConversationMediaItemModel.source_event_id).where(
                        ConversationMediaItemModel.conversation_id.in_(conversation_id)
                        if isinstance(conversation_id, tuple)
                        else ConversationMediaItemModel.conversation_id == conversation_id,
                        ConversationMediaItemModel.source_snapshot_json.is_(None),
                        ConversationMediaItemModel.kind.in_(kinds),
                    )
                )
            )
        async with optional_session(self._database, session, write=False) as active:
            with active.no_autoflush:
                if session is None:
                    await active.execute(text("BEGIN"))
                family: tuple[str, ...] = ()
                if person_id is not None:
                    family = await person_family_ids(active, person_id)
                    conditions.append(ChatEventModel.author_person_id.in_(family))

                def scoped_match() -> str:
                    conversations = (
                        conversation_id
                        if isinstance(conversation_id, tuple)
                        else (conversation_id,)
                    )
                    return " OR ".join(
                        "("
                        + cast(
                            str,
                            compile_match(
                                identity, query, person_ids=family, author_kind=author_kind
                            ),
                        )
                        + ")"
                        for identity in dict.fromkeys(conversations)
                    )

                fts_expression = (
                    await asyncio.to_thread(scoped_match)
                    if query.terms and not metadata_candidates
                    else None
                )
                fts = table("chat_history_fts", column("rowid", Integer))
                candidate_id = fts.c.rowid if fts_expression else ChatEventModel.id
                selected: list[EventRecord] = []
                fields: dict[int, tuple[str, ...]] = {}
                boundary = upper if order == "recent" else lower

                cancelled = threading.Event()

                def exact_fields(
                    records: tuple[EventRecord, ...],
                ) -> tuple[tuple[str, ...] | None, ...]:
                    matched: list[tuple[str, ...] | None] = []
                    for row in records:
                        if cancelled.is_set():
                            break
                        matched.append(match_fields(query, row.content, row.audio_transcript))
                    return tuple(matched)

                while len(selected) <= limit:
                    batch_conditions = [*conditions]
                    identifier = candidate_id
                    if order == "recent":
                        batch_conditions.append(identifier > lower)
                        if boundary is not None:
                            batch_conditions.append(identifier < boundary)
                    else:
                        batch_conditions.append(identifier > boundary)
                        if upper is not None:
                            batch_conditions.append(identifier < upper)
                    statement = select(ChatEventModel)
                    if fts_expression:
                        statement = statement.select_from(fts).join(
                            ChatEventModel, ChatEventModel.id == fts.c.rowid
                        )
                        statement = statement.where(*batch_conditions)
                        statement = statement.where(text("chat_history_fts MATCH :history_match"))
                    else:
                        statement = statement.where(*batch_conditions)
                    statement = statement.order_by(
                        candidate_id.desc() if order == "recent" else candidate_id.asc()
                    ).limit(limit + 1)
                    if fts_expression:
                        statement = statement.params(history_match=fts_expression)
                    rows = (await _history_read(active, statement)).all()
                    if not rows:
                        break
                    records = tuple(_event_record(row) for row in rows)
                    if query.terms or query.exclude:
                        try:
                            matched = await asyncio.to_thread(exact_fields, records)
                        except asyncio.CancelledError:
                            cancelled.set()
                            raise
                    else:
                        matched = ((),) * len(records)
                    for record, labels in zip(records, matched, strict=True):
                        if labels is not None:
                            selected.append(record)
                            fields[record.id] = labels
                            if len(selected) > limit:
                                break
                    boundary = records[-1].id
                    if len(rows) < limit + 1:
                        break
                has_more = len(selected) > limit
                events = tuple(selected[:limit])
                contexts: dict[int, EventRecord] = {}
                if context_before or context_after:
                    for record in events:
                        _, earlier, later = await self.history_around(
                            session=active,
                            conversation_id=cast(str, record.canonical_conversation_id),
                            event_id=record.id,
                            before=context_before,
                            after=context_after,
                            event_kind=event_kind,
                            predicates=predicates,
                        )
                        contexts.update((row.id, row) for row in (*earlier, *later))
                    for record in events:
                        contexts.pop(record.id, None)
                return HistoryPage(
                    events,
                    has_more,
                    events[-1].id if has_more else None,
                    {row.id: fields[row.id] for row in events},
                    tuple(contexts[key] for key in sorted(contexts)),
                )

    async def history_around(
        self,
        *,
        conversation_id: str,
        event_id: int,
        before: int,
        after: int,
        session: AsyncSession | None = None,
        event_kind: str = "message",
        predicates: Collection[Any] = (),
    ) -> tuple[EventRecord | None, tuple[EventRecord, ...], tuple[EventRecord, ...]]:
        """Read one permanent anchor and its ID-adjacent events in the same conversation."""
        from yuki.persistence.unit_of_work import optional_session

        if before < 0 or after < 0:
            raise ValueError("context counts must be nonnegative")
        async with optional_session(self._database, session, write=False) as active:
            with active.no_autoflush:
                if session is None:
                    await active.execute(text("BEGIN"))
                conditions = self._history_conditions(conversation_id, event_kind, predicates)
                center = await _history_read(
                    active,
                    select(ChatEventModel).where(*conditions, ChatEventModel.id == event_id),
                    single=True,
                )
                if center is None:
                    return None, (), ()
                earlier = (
                    await self.query_history(
                        session=active,
                        conversation_id=conversation_id,
                        before_event_id=event_id,
                        event_kind=event_kind,
                        predicates=predicates,
                        limit=before,
                    )
                    if before
                    else None
                )
                later = (
                    await self.query_history(
                        session=active,
                        conversation_id=conversation_id,
                        after_event_id=event_id,
                        event_kind=event_kind,
                        predicates=predicates,
                        limit=after,
                        order="oldest",
                    )
                    if after
                    else None
                )
                return (
                    _event_record(center),
                    tuple(reversed(earlier.events)) if earlier else (),
                    later.events if later else (),
                )

    async def hydrate_rebuild_subjects(self, event: EventRecord) -> EventRecord:
        """Read legacy selectors in their actual platform; recorded Person IDs win."""
        if event.scope_type is not ScopeType.GROUP or not event.group_id:
            return replace(
                event, mentioned_user_ids=(), mentioned_person_ids=(), reply_sender_user_id=None
            )
        from yuki.identity.db_models import PresenceModel
        from yuki.identity.ownership_interval import historical_person_ids

        async with self._database.read_session() as session:
            presence = (
                await session.get(PresenceModel, event.ingress_presence_id)
                if event.ingress_presence_id
                else None
            )
            mentions = list(event.mentioned_user_ids)
            if not mentions and presence is not None and presence.platform == "qq":
                for segment in event.segments:
                    data = segment.get("data")
                    if segment.get("type") == "at" and isinstance(data, dict):
                        raw = str(data.get("qq", "")).strip()
                        if raw.isdigit():
                            mentions.append(raw)
            accounts = tuple(
                dict.fromkeys(item for item in mentions if item != event.sender_user_id)
            )
            recorded_people = bool(event.mentioned_person_ids) or any(
                part.get("type") == "yuki_context"
                and isinstance(data := part.get("data"), dict)
                and "mentioned_person_ids" in data
                for part in event.segments
            )
            observed_at = await session.scalar(
                select(ChatEventModel.observed_at).where(ChatEventModel.id == event.id)
            )
            people = (
                await historical_person_ids(session, presence.platform, accounts, observed_at)
                if presence is not None and observed_at is not None and not recorded_people
                else {}
            )
        mention_accounts = tuple(
            account for account in accounts if recorded_people or account in people
        )
        mention_people = (
            event.mentioned_person_ids
            if recorded_people
            else tuple(dict.fromkeys(people[account] for account in mention_accounts))
        )
        reply_sender = None
        if event.reply_to_event_id is not None and event.canonical_conversation_id is not None:
            reply = await self.get_reply_event(
                event.reply_to_event_id, conversation_id=event.canonical_conversation_id
            )
            if reply is not None and reply.author_person_id is not None:
                reply_sender = reply.sender_user_id
        return replace(
            event,
            mentioned_user_ids=mention_accounts,
            mentioned_person_ids=mention_people,
            reply_sender_user_id=reply_sender,
        )

    async def memory_job_status(self, event_id: int) -> str | None:
        async with self._database.read_session() as session:
            return cast(
                str | None,
                await session.scalar(
                    select(MemoryJobModel.status).where(MemoryJobModel.event_id == event_id)
                ),
            )

    async def set_visual_summary(self, event_id: int, summary: str) -> bool:
        """Attach one compact derived observation to its immutable source event."""

        return await self._writer.set_visual_summary(event_id, summary)

    async def set_audio_transcript(
        self,
        event_id: int,
        transcript: str,
        *,
        generation: int,
        expected_source: EventRecord | None = None,
    ) -> bool:
        return await self._writer.set_audio_transcript(
            event_id, transcript, generation=generation, expected_source=expected_source
        )
