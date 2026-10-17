"""Repositories for the permanent event ledger and event state."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, cast

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.hydrate import conversation_for_scope
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.memory.eligibility import MemoryEventEligibilityPolicy
from yuki.memory.rebuild.models import (
    MemoryRebuildPlanStatistics,
    MemoryRebuildSelection,
)
from yuki.persistence.database import Database
from yuki.persistence.models import (
    ChatEventModel,
    MemoryJobModel,
)
from yuki.persistence.repository_helpers import (
    _event_record,
    keeper_event_clause,
    sql_keeper_event_predicate,
)
from yuki.persistence.repository_records import (
    EventRecord,
)
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


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
    visible_event_ids: tuple[int, ...] = field(default=(), compare=False)
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
        if selection.bot_user_ids:
            conditions.append(ChatEventModel.bot_user_id.in_(selection.bot_user_ids))
        if selection.scope_types:
            conditions.append(
                ChatEventModel.scope_type.in_(tuple(scope.value for scope in selection.scope_types))
            )
        if selection.sender_user_ids:
            conditions.append(ChatEventModel.sender_user_id.in_(selection.sender_user_ids))
        if selection.group_ids:
            conditions.append(ChatEventModel.group_id.in_(selection.group_ids))
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
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(ChatEventModel)
                    .where(*conditions)
                    .order_by(ChatEventModel.occurred_at.asc(), ChatEventModel.id.asc())
                    .limit(bounded_limit)
                )
            ).all()
        return tuple(_event_record(row) for row in rows)

    async def append(
        self,
        *,
        bot_user_id: str,
        platform_message_id: str,
        scope_type: ScopeType,
        sender_user_id: str,
        direction: str,
        content: str,
        segments: tuple[dict[str, Any], ...] = (),
        group_id: str | None = None,
        private_peer_user_id: str | None = None,
        reply_to_message_id: str | None = None,
        reply_to_event_id: int | None = None,
        occurred_at: datetime | None = None,
        sender_nickname: str = "",
        sender_group_card: str = "",
        sender_is_bot: bool = False,
        origin: str = "user_message",
        automation_id: int | None = None,
        automation_run_id: int | None = None,
        caused_by_event_id: int | None = None,
    ) -> tuple[EventRecord, bool]:
        """Append legacy QQ input; canonical ingress writes through its admitted scope."""

        scope = (
            ConversationScope.group(bot_user_id, group_id or "", platform="qq")
            if scope_type is ScopeType.GROUP
            else ConversationScope.private(
                bot_user_id, private_peer_user_id or sender_user_id, platform="qq"
            )
        )
        result = await self._writer.append(
            scope=scope,
            platform_message_id=platform_message_id,
            sender_user_id=sender_user_id,
            direction=direction,
            content=content,
            segments=segments,
            reply_to_message_id=reply_to_message_id,
            reply_to_event_id=reply_to_event_id,
            occurred_at=occurred_at,
            sender_nickname=sender_nickname,
            sender_group_card=sender_group_card,
            sender_is_bot=sender_is_bot,
            origin=origin,
            automation_id=automation_id,
            automation_run_id=automation_run_id,
            caused_by_event_id=caused_by_event_id,
        )
        return result.event, result.created

    async def find_by_platform_message(
        self,
        *,
        bot_user_id: str,
        platform_message_id: str,
    ) -> EventRecord | None:
        """Return one exact locally observed event without widening its conversation scope."""

        async with self._database.sessions() as session:
            row = await session.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.bot_user_id == bot_user_id,
                    ChatEventModel.platform_message_id == platform_message_id,
                )
            )
        return _event_record(row) if row is not None else None

    async def get_event(self, event_id: int) -> EventRecord | None:
        """Return one exact ledger event by its internal immutable identifier."""

        async with self._database.sessions() as session:
            row = await session.get(ChatEventModel, event_id)
        return _event_record(row) if row is not None else None

    async def get_reply_event(
        self, event_id: int, *, conversation_id: str, current_generation_only: bool = False
    ) -> EventRecord | None:
        """Read a resolved reply only from its canonical conversation's live messages."""
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.id == event_id,
                    ChatEventModel.canonical_conversation_id == conversation_id,
                    ChatEventModel.event_kind == "message",
                    keeper_event_clause(),
                )
            )
            if row is not None and current_generation_only:
                from yuki.conversation.canonical_db_models import CanonicalConversationModel

                conversation = await session.get(CanonicalConversationModel, conversation_id)
                if conversation is None or row.id <= conversation.starts_after_event_id:
                    return None
        return _event_record(row) if row is not None else None

    async def read_scope_context(
        self, scope: ConversationScope, *, limit: int, message_only: bool = False
    ) -> tuple[ConversationReadVersion, tuple[EventRecord, ...]]:
        """Read the current generation, retaining its version for dispatch validation."""
        async with self._database.sessions() as session:
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
        async with self._database.sessions() as session:
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
        async with self._database.sessions() as session:
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

        async with self._database.sessions() as session:
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

    async def list_canonical_recent(
        self,
        conversation_id: str,
        *,
        limit: int,
        message_only: bool = False,
    ) -> tuple[EventRecord, ...]:
        """Newest keeper or legacy-null events. Non-live statuses never participate."""

        from yuki.conversation.canonical_db_models import CanonicalConversationModel

        async with self._database.sessions() as session:
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            if conversation is None:
                return ()
            query = select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == conversation_id,
                keeper_event_clause(),
                ChatEventModel.id > conversation.starts_after_event_id,
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
        async with self._database.sessions() as session:
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

        async with self._database.sessions() as session:
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
        async with self._database.sessions() as session:
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
        async with self._database.sessions() as session:
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
        async with self._database.sessions() as session:
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
        async with self._database.sessions() as session:
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

    async def list_scope_around(
        self,
        scope: ConversationScope,
        *,
        event_id: int,
        before: int,
        after: int,
        message_only: bool = False,
    ) -> tuple[EventRecord | None, tuple[EventRecord, ...], tuple[EventRecord, ...]]:
        """Read nearby events strictly inside the current scope generation."""

        async with self._database.sessions() as session:
            await session.execute(text("BEGIN"))
            version = await _read_version(session, scope)
            if version.conversation_id is None:
                return None, (), ()
            query = select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == version.conversation_id,
                ChatEventModel.id > version.starts_after_event_id,
                keeper_event_clause(),
            )
            if message_only:
                query = query.where(ChatEventModel.event_kind == "message")
            center = await session.scalar(query.where(ChatEventModel.id == event_id))
            if center is None:
                return None, (), ()
            earlier = (
                (
                    await session.scalars(
                        query.where(ChatEventModel.id < event_id)
                        .order_by(ChatEventModel.id.desc())
                        .limit(before)
                    )
                ).all()
                if before > 0
                else ()
            )
            later = (
                (
                    await session.scalars(
                        query.where(ChatEventModel.id > event_id)
                        .order_by(ChatEventModel.id)
                        .limit(after)
                    )
                ).all()
                if after > 0
                else ()
            )
            return (
                _event_record(center),
                tuple(_event_record(row) for row in reversed(earlier)),
                tuple(_event_record(row) for row in later),
            )

    async def hydrate_rebuild_subjects(self, event: EventRecord) -> EventRecord:
        """Read legacy selectors in their actual platform; recorded Person IDs win."""
        if event.scope_type is not ScopeType.GROUP or not event.group_id:
            return replace(
                event, mentioned_user_ids=(), mentioned_person_ids=(), reply_sender_user_id=None
            )
        from yuki.identity.db_models import PresenceModel
        from yuki.identity.ownership_interval import historical_person_ids

        async with self._database.sessions() as session:
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
        async with self._database.sessions() as session:
            return cast(
                str | None,
                await session.scalar(
                    select(MemoryJobModel.status).where(MemoryJobModel.event_id == event_id)
                ),
            )

    async def search(
        self,
        *,
        keyword: str,
        conversation_id: str | None = None,
        scope: ConversationScope | None = None,
        limit: int = 20,
        user_id: str | None = None,
        group_id: str | None = None,
        person_id: str | None = None,
        space_id: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        message_only: bool = False,
    ) -> tuple[EventRecord, ...]:
        """Search with trigram FTS, falling back to bounded LIKE for short terms."""

        conditions: list[str] = [sql_keeper_event_predicate("ce")]
        params: dict[str, Any] = {"limit": limit}
        has_search_bound = bool(user_id or group_id or person_id or space_id or after or before)
        if scope is not None:
            conditions.extend(
                (
                    "ce.canonical_conversation_id = :scope_conversation_id",
                    "ce.id > (SELECT c.starts_after_event_id FROM canonical_conversations AS c "
                    "WHERE c.id = :scope_conversation_id)",
                )
            )
            has_search_bound = True
        if conversation_id is not None:
            conditions.append("ce.canonical_conversation_id = :conversation_id")
            conditions.append(
                "ce.id > (SELECT starts_after_event_id FROM canonical_conversations "
                "WHERE id = :conversation_id)"
            )
            params["conversation_id"] = conversation_id
            has_search_bound = True
        if message_only:
            conditions.append("ce.event_kind = 'message'")
        if user_id:
            conditions.append(
                "(ce.sender_user_id = :user_id OR ce.private_peer_user_id = :user_id)"
            )
            params["user_id"] = user_id
        if group_id:
            conditions.append("ce.group_id = :group_id")
            params["group_id"] = group_id
        if person_id is not None:
            conditions.append(
                "(ce.author_person_id = :person_id OR EXISTS (SELECT 1 "
                "FROM canonical_conversations AS source_scene "
                "WHERE source_scene.id=ce.canonical_conversation_id "
                "AND source_scene.kind='private' AND source_scene.person_id=:person_id))"
            )
            params["person_id"] = person_id
        if space_id is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM canonical_conversations AS source_scene "
                "WHERE source_scene.id=ce.canonical_conversation_id "
                "AND source_scene.space_id=:space_id)"
            )
            params["space_id"] = space_id
        if after:
            conditions.append("ce.occurred_at >= :after")
            params["after"] = after
        if before:
            conditions.append("ce.occurred_at <= :before")
            params["before"] = before
        prefix = f" AND {' AND '.join(conditions)}" if conditions else ""
        stripped = keyword.strip()
        if len(stripped) >= 3:
            bounds = ""
            matches = "SELECT rowid FROM chat_events_fts WHERE chat_events_fts MATCH :keyword"
            if has_search_bound:
                bounded_events = "SELECT ce.id FROM chat_events AS ce WHERE " + " AND ".join(
                    conditions
                )
                bounds = (
                    "WITH search_bounds AS MATERIALIZED (SELECT ("
                    + bounded_events
                    + " ORDER BY ce.id ASC LIMIT 1) AS first_id, ("
                    + bounded_events
                    + " ORDER BY ce.id DESC LIMIT 1) AS last_id) "
                )
                matches += (
                    " AND rowid >= (SELECT first_id FROM search_bounds)"
                    " AND rowid <= (SELECT last_id FROM search_bounds)"
                )
            sql = text(
                bounds
                + "SELECT ce.* FROM chat_events AS ce WHERE ce.id IN ("
                + matches
                + ")"
                + prefix
                + " ORDER BY ce.occurred_at DESC, ce.id DESC LIMIT :limit"
            )
            params["keyword"] = '"' + stripped.replace('"', '""') + '"'
        else:
            if not has_search_bound:
                raise ValueError(
                    "short history searches require a conversation, owner, or time bound"
                )
            sql = text(
                "SELECT ce.* FROM chat_events AS ce WHERE "
                "(ce.content LIKE :pattern OR ce.audio_transcript LIKE :pattern)"
                + prefix
                + " ORDER BY ce.occurred_at DESC, ce.id DESC LIMIT :limit"
            )
            params["pattern"] = f"%{stripped}%"
        async with self._database.sessions() as session:
            if scope is not None:
                await session.execute(text("BEGIN"))
                params["scope_conversation_id"] = await _conversation_id_for_scope(session, scope)
            mappings = (await session.execute(sql, params)).mappings().all()
        records: list[EventRecord] = []
        for row in mappings:
            records.append(_event_record(dict(row)))
        return tuple(reversed(records))

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
