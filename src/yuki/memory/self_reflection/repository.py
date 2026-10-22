"""Restart-safe episode cursors and receipts for low-frequency self-reflection."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import TypeAdapter
from sqlalchemy import and_, func, or_, select, text, true, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.sql.elements import ColumnElement

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.conversations import ScopeType
from yuki.memory.extraction import source_event_fingerprint
from yuki.memory.partition import (
    MemoryPartitionResolutionError,
    format_canonical_memory_partition,
    require_xor_memory_owner,
)
from yuki.memory.projections import (
    project_active_person_external_id,
    project_active_space_external_id,
)
from yuki.memory.self_reflection.db_models import InitiativeReflectionWindowModel
from yuki.memory.self_reflection.models import (
    SelfReflectionBatch,
    SelfReflectionState,
    StoredToolReceipt,
)
from yuki.persistence.database import Database
from yuki.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemorySelfReflectionResultModel,
    MemorySelfReflectionRunModel,
    MemorySelfReflectionRuntimeModel,
    MemorySelfReflectionStateModel,
    MemoryToolReceiptModel,
)
from yuki.persistence.repository_helpers import _event_record, keeper_event_clause
from yuki.persistence.repository_records import EventRecord

_CHECKPOINT_EVENTS = TypeAdapter(dict[str, EventRecord])
_CHECKPOINT_TOOLS = TypeAdapter(dict[str, StoredToolReceipt])


@dataclass(frozen=True, slots=True)
class _EventSources:
    data: str
    count: int
    columns: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _CursorAdvance:
    state: MemorySelfReflectionStateModel
    gaps: tuple[tuple[int, int, int, str], ...]
    events: _EventSources
    last_event_id: int
    pending_events: int
    pending_characters: int
    pending_since: datetime | None


_CURSOR_SOURCE_COLUMNS = (
    ChatEventModel.id,
    ChatEventModel.content,
    ChatEventModel.audio_transcript,
    ChatEventModel.occurred_at,
)
_EVENT_SOURCE_COLUMNS = _CURSOR_SOURCE_COLUMNS + tuple(
    getattr(ChatEventModel, name)
    for name in (
        "direction",
        "author_kind",
        "bot_user_id",
        "platform_message_id",
        "scope_type",
        "sender_user_id",
        "group_id",
        "private_peer_user_id",
        "visual_summary",
        "segments_json",
        "reply_to_message_id",
        "reply_to_event_id",
        "origin",
        "canonical_conversation_id",
        "canonical_event_id",
        "author_person_id",
        "author_presence_id",
        "ingress_presence_id",
        "sender_nickname",
        "sender_group_card",
        "external_payload_json",
    )
)


def _prepare_event_sources(
    session: AsyncSession,
    rows: Sequence[ChatEventModel],
    *,
    columns: tuple[Any, ...] = _EVENT_SOURCE_COLUMNS,
) -> _EventSources:
    """Encode the actual source values before admission, using SQLite's bound types."""
    dialect = session.get_bind().dialect
    processors = tuple(c.type.dialect_impl(dialect).bind_processor(dialect) for c in columns)
    data = [
        [
            process(value) if process is not None else value
            for value, process in zip(
                (getattr(row, c.key) for c in columns), processors, strict=True
            )
        ]
        for row in rows
    ]
    return _EventSources(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), len(rows), columns
    )


async def _events_current(session: AsyncSession, query: Any, sources: _EventSources) -> bool:
    """Prove the exact selected set and every raw field in one fixed-shape query."""
    selected = query.with_only_columns(ChatEventModel.id).cte("reflection_selected")
    if not sources.count:
        return bool(await session.scalar(select(~select(selected.c.id).exists())))
    expected = func.json_each(sources.data).table_valued("value")
    # A correlated PK lookup visits each prepared event once. Joining the JSON
    # table from the event side would rescan it for every row in a large tail.
    same = (
        select(ChatEventModel.id)
        .where(
            ChatEventModel.id == func.json_extract(expected.c.value, "$[0]"),
            ChatEventModel.id.in_(select(selected.c.id)),
            *(
                c.is_not_distinct_from(func.json_extract(expected.c.value, f"$[{i}]"))
                for i, c in enumerate(sources.columns[1:], 1)
            ),
        )
        .correlate(expected)
        .exists()
    )
    actual, matched = (
        await session.execute(
            select(
                select(func.count()).select_from(selected).scalar_subquery(),
                select(func.count()).select_from(expected).where(same).scalar_subquery(),
            )
        )
    ).one()
    return int(actual) == sources.count and int(matched) == sources.count


class SelfReflectionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    @staticmethod
    def _xor_owner_state_clause() -> ColumnElement[bool]:
        return or_(
            and_(
                MemorySelfReflectionStateModel.canonical_person_id.is_not(None),
                MemorySelfReflectionStateModel.canonical_space_id.is_(None),
            ),
            and_(
                MemorySelfReflectionStateModel.canonical_person_id.is_(None),
                MemorySelfReflectionStateModel.canonical_space_id.is_not(None),
            ),
        )

    @staticmethod
    def _owner_state_filter(
        person_id: str | None,
        space_id: str | None,
    ) -> ColumnElement[bool]:
        person_id, space_id = require_xor_memory_owner(person_id, space_id)
        if person_id:
            return and_(
                MemorySelfReflectionStateModel.canonical_person_id == person_id,
                MemorySelfReflectionStateModel.canonical_space_id.is_(None),
            )
        return and_(
            MemorySelfReflectionStateModel.canonical_space_id == space_id,
            MemorySelfReflectionStateModel.canonical_person_id.is_(None),
        )

    @staticmethod
    def _owner_receipt_filter(
        person_id: str | None,
        space_id: str | None,
    ) -> ColumnElement[bool]:
        person_id, space_id = require_xor_memory_owner(person_id, space_id)
        if person_id:
            return and_(
                MemoryToolReceiptModel.canonical_person_id == person_id,
                MemoryToolReceiptModel.canonical_space_id.is_(None),
            )
        return and_(
            MemoryToolReceiptModel.canonical_space_id == space_id,
            MemoryToolReceiptModel.canonical_person_id.is_(None),
        )

    @staticmethod
    def _canonical_conversation_event_scope(
        person_id: str | None,
        space_id: str | None,
    ) -> ColumnElement[bool]:
        person_id, space_id = require_xor_memory_owner(person_id, space_id)
        owner = (
            and_(
                CanonicalConversationModel.kind == "space",
                CanonicalConversationModel.space_id == space_id,
                CanonicalConversationModel.person_id.is_(None),
            )
            if space_id
            else and_(
                CanonicalConversationModel.kind == "private",
                CanonicalConversationModel.person_id == person_id,
                CanonicalConversationModel.space_id.is_(None),
            )
        )
        return and_(
            ChatEventModel.canonical_conversation_id == CanonicalConversationModel.id,
            owner,
        )

    @staticmethod
    def _owner_ids_from_live_conversation(
        conversation: CanonicalConversationModel,
        *,
        event_scope_type: str,
    ) -> tuple[str | None, str | None] | None:
        if conversation.kind == "private":
            if event_scope_type != ScopeType.PRIVATE.value:
                return None
            if not conversation.person_id or conversation.space_id:
                return None
            return conversation.person_id, None
        if conversation.kind == "space":
            if event_scope_type != ScopeType.GROUP.value:
                return None
            if not conversation.space_id or conversation.person_id:
                return None
            return None, conversation.space_id
        return None

    @staticmethod
    def _run_conflict_target(
        row: MemorySelfReflectionStateModel,
    ) -> tuple[list[str], ColumnElement[bool] | None]:
        require_xor_memory_owner(row.canonical_person_id, row.canonical_space_id)
        if row.canonical_person_id:
            return (
                ["canonical_person_id", "scheduled_slot"],
                and_(
                    MemorySelfReflectionRunModel.canonical_person_id.is_not(None),
                    MemorySelfReflectionRunModel.canonical_space_id.is_(None),
                ),
            )
        return (
            ["canonical_space_id", "scheduled_slot"],
            and_(
                MemorySelfReflectionRunModel.canonical_space_id.is_not(None),
                MemorySelfReflectionRunModel.canonical_person_id.is_(None),
            ),
        )

    def _apply_event_scope(
        self,
        query: Any,
        row: MemorySelfReflectionStateModel,
    ) -> Any:
        return query.join(
            CanonicalConversationModel,
            ChatEventModel.canonical_conversation_id == CanonicalConversationModel.id,
        ).where(
            self._canonical_conversation_event_scope(
                row.canonical_person_id,
                row.canonical_space_id,
            ),
            ChatEventModel.canonical_event_id.is_not(None),
            ChatEventModel.id > CanonicalConversationModel.starts_after_event_id,
            ChatEventModel.id > CanonicalConversationModel.last_generation_change_event_id,
            keeper_event_clause(),
        )

    def _gap_query(self, state: MemorySelfReflectionStateModel) -> Any:
        return (
            select(
                MemorySelfReflectionRunModel.id,
                MemorySelfReflectionRunModel.first_event_id,
                MemorySelfReflectionRunModel.last_event_id,
                MemorySelfReflectionRunModel.status,
            )
            .where(
                MemorySelfReflectionRunModel.conversation_key_hash == state.conversation_key_hash,
                MemorySelfReflectionRunModel.last_event_id > state.last_event_id,
                self._apply_event_scope(
                    select(ChatEventModel.id).where(
                        ChatEventModel.id >= MemorySelfReflectionRunModel.first_event_id,
                        ChatEventModel.id <= MemorySelfReflectionRunModel.last_event_id,
                        ChatEventModel.event_kind == "message",
                    ),
                    state,
                ).exists(),
            )
            .order_by(MemorySelfReflectionRunModel.id)
        )

    def _cursor_events_query(self, state: MemorySelfReflectionStateModel) -> Any:
        return self._apply_event_scope(
            select(ChatEventModel).where(
                ChatEventModel.id > state.last_event_id,
                ChatEventModel.id <= state.latest_event_id,
                ChatEventModel.event_kind == "message",
            ),
            state,
        ).order_by(ChatEventModel.id)

    async def _prepare_advance(
        self,
        session: AsyncSession,
        state: MemorySelfReflectionStateModel,
        *,
        through_event_id: int,
        completed_run_ids: set[int],
    ) -> _CursorAdvance:
        """Project all completions in this transaction before counting the remaining tail."""
        gaps = tuple(tuple(row) for row in await session.execute(self._gap_query(state)))
        processed = max(state.last_event_id, min(through_event_id, state.latest_event_id))
        unfinished = [
            first
            for ident, first, _, status in gaps
            if status != "completed" and ident not in completed_run_ids
        ]
        if unfinished:
            processed = min(processed, min(unfinished) - 1)
        completed = iter(
            sorted(
                (first, last)
                for ident, first, last, status in gaps
                if status == "completed" or ident in completed_run_ids
            )
        )
        events = tuple(
            await session.scalars(
                self._cursor_events_query(state).options(load_only(*_CURSOR_SOURCE_COLUMNS))
            )
        )
        pending_events = pending_characters = 0
        pending_since = None
        covered = next(completed, None)
        for event in events:
            while covered is not None and covered[1] < event.id:
                covered = next(completed, None)
            if event.id <= processed or (covered is not None and covered[0] <= event.id):
                continue
            content = event.evidence_content
            if content.strip():
                pending_events += 1
                pending_characters += len(content)
                pending_since = pending_since or event.occurred_at
        return _CursorAdvance(
            state,
            gaps,
            _prepare_event_sources(session, events, columns=_CURSOR_SOURCE_COLUMNS),
            processed,
            pending_events,
            pending_characters,
            pending_since,
        )

    async def _cursor_current(
        self, session: AsyncSession, prepared: _CursorAdvance
    ) -> MemorySelfReflectionStateModel | None:
        state = prepared.state
        gaps = self._gap_query(state).subquery()
        rows = tuple(
            await session.execute(
                select(MemorySelfReflectionStateModel, *gaps.c)
                .select_from(MemorySelfReflectionStateModel)
                .outerjoin(gaps, true())
                .where(MemorySelfReflectionStateModel.id == state.id)
                .order_by(gaps.c.id)
            )
        )
        if not rows:
            return None
        current = rows[0][0]
        fields = (
            "conversation_key_hash",
            "canonical_person_id",
            "canonical_space_id",
            "last_event_id",
            "latest_event_id",
        )
        if any(getattr(current, key) != getattr(state, key) for key in fields):
            return None
        actual_gaps = tuple(tuple(row[1:]) for row in rows if row[1] is not None)
        if actual_gaps != prepared.gaps or not await _events_current(
            session, self._cursor_events_query(current), prepared.events
        ):
            return None
        return cast(MemorySelfReflectionStateModel, current)

    @staticmethod
    def _apply_cursor(
        state: MemorySelfReflectionStateModel, prepared: _CursorAdvance, now: datetime
    ) -> None:
        state.last_event_id = prepared.last_event_id
        state.pending_events = prepared.pending_events
        state.pending_characters = prepared.pending_characters
        state.pending_since = prepared.pending_since
        state.updated_at = now

    async def scan_new_events(self, *, limit: int = 5000) -> int:
        """Accumulate only post-deployment events; first startup establishes a baseline."""

        total = 0
        remaining = max(1, limit)
        while remaining:
            page_size = min(100, remaining)
            count = await self._scan_event_batch(limit=page_size)
            total += count
            remaining -= count
            if count < page_size:
                break
            # Release SQLite's writer between bounded, independently committed pages.
            await asyncio.sleep(0)
        return total

    async def _scan_event_batch(self, *, limit: int) -> int:
        while True:
            async with self._database.read_session() as reader:
                runtime = await reader.get(MemorySelfReflectionRuntimeModel, 1)
                if runtime is None:
                    async with self._database.immediate_session() as writer:
                        if await writer.get(MemorySelfReflectionRuntimeModel, 1) is None:
                            maximum = int(
                                await writer.scalar(select(func.max(ChatEventModel.id))) or 0
                            )
                            writer.add(
                                MemorySelfReflectionRuntimeModel(
                                    id=1,
                                    last_scanned_event_id=maximum,
                                    updated_at=datetime.now(UTC),
                                )
                            )
                            return 0
                    continue
                query = (
                    select(ChatEventModel)
                    .where(
                        ChatEventModel.id > runtime.last_scanned_event_id,
                        ChatEventModel.event_kind == "message",
                        ChatEventModel.direction.in_(("inbound", "outbound")),
                    )
                    .order_by(ChatEventModel.id)
                    .limit(max(1, limit))
                )
                rows = tuple(await reader.scalars(query))
                if not rows:
                    return 0
                ids = tuple(row.id for row in rows)
                live_query = (
                    select(ChatEventModel.id)
                    .where(
                        ChatEventModel.id.in_(ids),
                        keeper_event_clause(),
                    )
                    .order_by(ChatEventModel.id)
                )
                live_ids = tuple(await reader.scalars(live_query))
                conversations = {
                    c.id: c
                    for c in await reader.scalars(
                        select(CanonicalConversationModel).where(
                            CanonicalConversationModel.id.in_(
                                tuple({r.canonical_conversation_id for r in rows})
                            )
                        )
                    )
                }
                # Owner hashing, ASR/body interpretation and counting belong to preparation.
                prepared: dict[str, dict[str, Any]] = {}
                for row in rows:
                    conversation = conversations.get(row.canonical_conversation_id or "")
                    if (
                        row.id not in live_ids
                        or not row.canonical_event_id
                        or conversation is None
                        or row.id <= conversation.starts_after_event_id
                        or row.id <= conversation.last_generation_change_event_id
                    ):
                        continue
                    owners = self._owner_ids_from_live_conversation(
                        conversation, event_scope_type=row.scope_type
                    )
                    if owners is None:
                        continue
                    person_id, space_id = owners
                    key_hash = hashlib.sha256(
                        format_canonical_memory_partition(
                            person_id=person_id,
                            space_id=space_id,
                        ).encode()
                    ).hexdigest()
                    item = prepared.setdefault(
                        key_hash,
                        dict(
                            person_id=person_id,
                            space_id=space_id,
                            bot_user_id=row.bot_user_id,
                            events=0,
                            characters=0,
                            since=None,
                            first_occurred_at=row.occurred_at,
                        ),
                    )
                    content = row.evidence_content.strip()
                    if content:
                        item["events"] += 1
                        item["characters"] += len(content)
                        item["since"] = item["since"] or row.occurred_at
                    item["latest"] = row.id
                conversation_fields = (
                    "id",
                    "kind",
                    "person_id",
                    "space_id",
                    "starts_after_event_id",
                    "last_generation_change_event_id",
                )
                expected_conversations = {
                    ident: tuple(getattr(c, k) for k in conversation_fields)
                    for ident, c in conversations.items()
                }
                event_sources = _prepare_event_sources(reader, rows)
            async with self._database.immediate_session() as writer:
                current_runtime = await writer.get(MemorySelfReflectionRuntimeModel, 1)
                if (
                    current_runtime is None
                    or current_runtime.last_scanned_event_id != runtime.last_scanned_event_id
                ):
                    continue
                current_conversations = {
                    c.id: tuple(getattr(c, k) for k in conversation_fields)
                    for c in await writer.scalars(
                        select(CanonicalConversationModel).where(
                            CanonicalConversationModel.id.in_(
                                tuple({r.canonical_conversation_id for r in rows})
                            )
                        )
                    )
                }
                if (
                    current_conversations != expected_conversations
                    or tuple(await writer.scalars(live_query)) != live_ids
                    or not await _events_current(writer, query, event_sources)
                ):
                    continue
                states = (
                    {
                        state.conversation_key_hash: state
                        for state in await writer.scalars(
                            select(MemorySelfReflectionStateModel).where(
                                MemorySelfReflectionStateModel.conversation_key_hash.in_(
                                    tuple(prepared)
                                )
                            )
                        )
                    }
                    if prepared
                    else {}
                )
                now = datetime.now(UTC)
                for key_hash, item in prepared.items():
                    state = states.get(key_hash)
                    if state is None:
                        state = MemorySelfReflectionStateModel(
                            conversation_key_hash=key_hash,
                            bot_user_id=item["bot_user_id"],
                            canonical_person_id=item["person_id"],
                            canonical_space_id=item["space_id"],
                            last_event_id=runtime.last_scanned_event_id,
                            latest_event_id=item["latest"],
                            pending_events=0,
                            pending_characters=0,
                            pending_since=item["first_occurred_at"],
                            updated_at=now,
                        )
                        writer.add(state)
                    current_runtime.ingress_events_total += item["events"]
                    state.pending_events += item["events"]
                    state.pending_characters += item["characters"]
                    state.pending_since = state.pending_since or item["since"]
                    state.latest_event_id = item["latest"]
                    state.updated_at = now
                current_runtime.last_scanned_event_id = rows[-1].id
                current_runtime.updated_at = now
                return len(rows)

    @property
    def database(self) -> Database:
        return self._database

    async def claim_due(
        self,
        *,
        scheduled_slot: str,
        event_threshold: int,
        character_threshold: int,
        max_wait_seconds: float,
        max_events: int,
        max_characters: int,
        context_events: int = 4,
        force: bool = False,
        cycle_id: str | None = None,
        timezone: str = "Asia/Shanghai",
    ) -> tuple[SelfReflectionBatch, ...]:
        from yuki.event_prompt import ChatEventPromptRenderer
        from yuki.memory.self_reflection.initiative import claim_initiative

        initiatives = await claim_initiative(
            self._database, max_characters=max_characters, cycle_id=cycle_id
        )
        if initiatives:
            return initiatives
        isolated_states: set[int] = set()
        # Refresh only changed input facts. SQL/commit failures leave through the common owner.
        while True:
            now = datetime.now(UTC)
            async with self._database.read_session() as reader:
                state_query = select(MemorySelfReflectionStateModel).where(
                    MemorySelfReflectionStateModel.pending_events > 0,
                    self._xor_owner_state_clause(),
                )
                if not force:
                    state_query = state_query.where(
                        or_(
                            MemorySelfReflectionStateModel.pending_events >= event_threshold,
                            MemorySelfReflectionStateModel.pending_characters
                            >= character_threshold,
                            MemorySelfReflectionStateModel.pending_since
                            <= now - timedelta(seconds=max_wait_seconds),
                        )
                    )
                states = tuple(
                    await reader.scalars(
                        state_query.order_by(
                            MemorySelfReflectionStateModel.pending_since,
                            MemorySelfReflectionStateModel.id,
                        )
                    )
                )
                for state in states:
                    if state.id in isolated_states:
                        continue
                    gaps = tuple(tuple(gap) for gap in await reader.execute(self._gap_query(state)))
                    outstanding = tuple(
                        await reader.scalars(
                            select(MemorySelfReflectionRunModel)
                            .where(MemorySelfReflectionRunModel.id.in_(tuple(g[0] for g in gaps)))
                            .order_by(MemorySelfReflectionRunModel.first_event_id)
                        )
                    )
                    if any(r.status == "processing" for r in outstanding):
                        continue
                    retry = next(
                        (
                            r
                            for r in outstanding
                            if r.status == "failed"
                            and r.retry_state != "isolated"
                            and (cycle_id is None or r.cycle_id != cycle_id)
                            and (r.next_attempt_at is None or _utc(r.next_attempt_at) <= now)
                        ),
                        None,
                    )
                    event_query = self._cursor_events_query(state)
                    if retry is not None:
                        event_query = event_query.where(
                            ChatEventModel.id >= retry.first_event_id,
                            ChatEventModel.id <= retry.last_event_id,
                        )
                    else:
                        for owned in outstanding:
                            event_query = event_query.where(
                                or_(
                                    ChatEventModel.id < owned.first_event_id,
                                    ChatEventModel.id > owned.last_event_id,
                                )
                            )
                    event_query = event_query.limit(max_events)
                    candidates = tuple(await reader.scalars(event_query))
                    renderer = ChatEventPromptRenderer(
                        tuple(_event_record(r) for r in candidates), timezone=timezone
                    )
                    selected: list[ChatEventModel] = []
                    characters = 0
                    for item in candidates:
                        size = len(renderer.render_event(_event_record(item)))
                        if characters + size > max_characters:
                            if not selected:
                                selected.append(item)
                            break
                        selected.append(item)
                        characters += size
                    if not selected and outstanding:
                        continue
                    advance = None
                    isolation = None
                    context: tuple[ChatEventModel, ...] = ()
                    context_query = None
                    fingerprint = ""
                    selected_events: tuple[EventRecord, ...] = ()
                    selected_context: tuple[EventRecord, ...] = ()
                    reason = (
                        "manual"
                        if force
                        else self._trigger_reason(
                            state,
                            event_threshold=event_threshold,
                            character_threshold=character_threshold,
                        )
                    )
                    if not selected:
                        advance = await self._prepare_advance(
                            reader,
                            state,
                            through_event_id=state.latest_event_id,
                            completed_run_ids=set(),
                        )
                    elif retry is not None and (
                        selected[0].id != retry.first_event_id
                        or selected[-1].id != retry.last_event_id
                    ):
                        isolation = "source_range_changed"
                    else:
                        fingerprint = hashlib.sha256(
                            repr([(r.id, r.evidence_content) for r in selected]).encode()
                        ).hexdigest()
                        if (
                            retry is not None
                            and retry.input_fingerprint
                            and fingerprint != retry.input_fingerprint
                        ):
                            isolation = "source_changed"
                        else:
                            context_query = (
                                self._apply_event_scope(
                                    select(ChatEventModel).where(
                                        ChatEventModel.id < selected[0].id,
                                        ChatEventModel.event_kind == "message",
                                    ),
                                    state,
                                )
                                .order_by(ChatEventModel.id.desc())
                                .limit(max(0, context_events))
                            )
                            context = tuple(await reader.scalars(context_query))
                            selected_events = tuple(_event_record(r) for r in selected)
                            selected_context = tuple(_event_record(r) for r in reversed(context))
                    break
                else:
                    return ()
                run_fields = (
                    "id",
                    "status",
                    "first_event_id",
                    "last_event_id",
                    "retry_state",
                    "cycle_id",
                    "next_attempt_at",
                    "input_fingerprint",
                )
                expected_runs = tuple(
                    tuple(getattr(r, key) for key in run_fields) for r in outstanding
                )
                input_characters = sum(len(r.evidence_content) for r in selected)
                candidate_sources = _prepare_event_sources(reader, candidates)
                context_sources = _prepare_event_sources(reader, context)
            async with self._database.immediate_session() as writer:
                current = await writer.get(MemorySelfReflectionStateModel, state.id)
                state_fields = (
                    "canonical_person_id",
                    "canonical_space_id",
                    "conversation_key_hash",
                    "last_event_id",
                    "latest_event_id",
                    "pending_events",
                    "pending_characters",
                    "pending_since",
                )
                if current is None or any(
                    getattr(current, key) != getattr(state, key) for key in state_fields
                ):
                    continue
                if (
                    tuple(tuple(gap) for gap in await writer.execute(self._gap_query(current)))
                    != gaps
                ):
                    continue
                actual_runs = tuple(
                    tuple(r)
                    for r in await writer.execute(
                        select(*(getattr(MemorySelfReflectionRunModel, key) for key in run_fields))
                        .where(MemorySelfReflectionRunModel.id.in_(tuple(g[0] for g in gaps)))
                        .order_by(MemorySelfReflectionRunModel.first_event_id)
                    )
                )
                if actual_runs != expected_runs or not await _events_current(
                    writer, event_query, candidate_sources
                ):
                    continue
                if context_query is not None and not await _events_current(
                    writer, context_query, context_sources
                ):
                    continue
                now = datetime.now(UTC)
                if advance is not None:
                    if not await self._cursor_current(writer, advance):
                        continue
                    self._apply_cursor(current, advance, now)
                    continue
                if isolation is not None:
                    assert retry is not None
                    await writer.execute(
                        update(MemorySelfReflectionRunModel)
                        .where(
                            MemorySelfReflectionRunModel.id == retry.id,
                            MemorySelfReflectionRunModel.status == "failed",
                        )
                        .values(retry_state="isolated", error_category=isolation)
                    )
                    isolated_states.add(state.id)
                    continue
                projected_state = await self._state(writer, current)
                if retry is not None:
                    inserted_id = await writer.scalar(
                        update(MemorySelfReflectionRunModel)
                        .where(
                            MemorySelfReflectionRunModel.id == retry.id,
                            MemorySelfReflectionRunModel.status == "failed",
                        )
                        .values(
                            status="processing",
                            started_at=now,
                            completed_at=None,
                            cycle_id=cycle_id,
                            attempt_count=MemorySelfReflectionRunModel.attempt_count + 1,
                            input_fingerprint=fingerprint,
                            processed_events=len(selected),
                            processed_characters=input_characters,
                        )
                        .returning(MemorySelfReflectionRunModel.id)
                    )
                else:
                    conflict, conflict_where = self._run_conflict_target(current)
                    inserted_id = await writer.scalar(
                        insert(MemorySelfReflectionRunModel)
                        .values(
                            conversation_key_hash=current.conversation_key_hash,
                            bot_user_id=current.bot_user_id,
                            canonical_person_id=current.canonical_person_id,
                            canonical_space_id=current.canonical_space_id,
                            scheduled_slot=scheduled_slot,
                            trigger_reason=reason,
                            first_event_id=selected[0].id,
                            last_event_id=selected[-1].id,
                            status="processing",
                            proposal_count=0,
                            committed_count=0,
                            started_at=now,
                            cycle_id=cycle_id,
                            attempt_count=1,
                            input_fingerprint=fingerprint,
                            processed_events=len(selected),
                            processed_characters=input_characters,
                        )
                        .on_conflict_do_nothing(index_elements=conflict, index_where=conflict_where)
                        .returning(MemorySelfReflectionRunModel.id)
                    )
                if inserted_id is None:
                    return ()
                assert projected_state is not None
                return (
                    SelfReflectionBatch(
                        state=projected_state,
                        events=selected_events,
                        context_events=selected_context,
                        trigger_reason=reason,
                        scheduled_slot=scheduled_slot,
                        run_id=inserted_id,
                        max_input_characters=max_characters,
                    ),
                )

    @staticmethod
    def _trigger_reason(
        row: MemorySelfReflectionStateModel,
        *,
        event_threshold: int,
        character_threshold: int,
    ) -> str:
        if row.pending_events >= event_threshold:
            return "event_count"
        if row.pending_characters >= character_threshold:
            return "characters"
        return "max_wait"

    async def health_snapshot(
        self,
        *,
        local_date: str,
    ) -> tuple[int, int, str | None, datetime | None]:
        """Return content-free pending and execution statistics."""

        async with self._database.read_session() as session:
            pending_query = select(func.count(MemorySelfReflectionStateModel.id)).where(
                MemorySelfReflectionStateModel.pending_events > 0,
                self._xor_owner_state_clause(),
            )
            pending = int(await session.scalar(pending_query) or 0)
            daily_calls = int(
                await session.scalar(
                    select(func.count(MemorySelfReflectionRunModel.id)).where(
                        MemorySelfReflectionRunModel.scheduled_slot.like(f"{local_date}:%")
                    )
                )
                or 0
            )
            last_run = await session.scalar(
                select(MemorySelfReflectionRunModel)
                .order_by(MemorySelfReflectionRunModel.started_at.desc())
                .limit(1)
            )
        return (
            pending,
            daily_calls,
            last_run.status if last_run is not None else None,
            last_run.completed_at if last_run is not None else None,
        )

    async def tool_receipts(
        self,
        batch: SelfReflectionBatch,
        *,
        limit: int = 8,
    ) -> tuple[StoredToolReceipt, ...]:
        rows: Sequence[MemoryToolReceiptModel]
        if batch.initiative_run_id is not None:
            async with self._database.read_session() as session:
                rows = list(
                    await session.scalars(
                        select(MemoryToolReceiptModel)
                        .where(
                            MemoryToolReceiptModel.initiative_run_id == batch.initiative_run_id,
                            MemoryToolReceiptModel.canonical_space_id
                            == batch.state.canonical_space_id,
                            MemoryToolReceiptModel.canonical_person_id.is_(None),
                            MemoryToolReceiptModel.id >= batch.first_receipt_id,
                            MemoryToolReceiptModel.id <= batch.last_receipt_id,
                        )
                        .order_by(MemoryToolReceiptModel.id)
                    )
                )
            return tuple(
                StoredToolReceipt(
                    row.id,
                    row.trigger_event_id,
                    row.tool_name,
                    row.success,
                    row.result_excerpt,
                    row.initiative_run_id,
                    row.bot_user_id,
                    _utc(row.created_at),
                )
                for row in rows
            )
        async with self._database.read_session() as session:
            state_row = await session.get(MemorySelfReflectionStateModel, batch.state.id)
            if state_row is None:
                raise MemoryPartitionResolutionError("missing_owner")
            receipt_filter = self._owner_receipt_filter(
                state_row.canonical_person_id,
                state_row.canonical_space_id,
            )
            rows = (
                await session.scalars(
                    select(MemoryToolReceiptModel)
                    .where(
                        receipt_filter,
                        MemoryToolReceiptModel.trigger_event_id >= batch.events[0].id,
                        MemoryToolReceiptModel.trigger_event_id <= batch.events[-1].id,
                        MemoryToolReceiptModel.expires_at > datetime.now(UTC),
                    )
                    .order_by(MemoryToolReceiptModel.created_at.asc())
                    .limit(max(1, limit))
                )
            ).all()
        return tuple(
            StoredToolReceipt(
                id=row.id,
                trigger_event_id=row.trigger_event_id,
                tool_name=row.tool_name,
                success=row.success,
                result_excerpt=row.result_excerpt,
                occurred_at=_utc(row.created_at),
            )
            for row in rows
        )

    async def complete(
        self,
        batch: SelfReflectionBatch,
        *,
        proposals: int,
        committed: int,
    ) -> None:
        await self._finish_runs(
            run_id=batch.run_id,
            completion=(proposals, committed),
            initiative_run_id=batch.initiative_run_id,
        )

    async def recover_interrupted(self, run_id: int, error_category: str) -> str | None:
        """Finalize the original run without replaying paid output or committed effects."""
        statuses = await self._finish_runs(run_id=run_id, error_category=error_category)
        return statuses.get(run_id)

    async def recover_stale_runs(self, *, started_before: datetime) -> int:
        statuses = await self._finish_runs(
            started_before=started_before, error_category="stale_processing"
        )
        return len(statuses)

    async def _finish_runs(
        self,
        *,
        run_id: int | None = None,
        started_before: datetime | None = None,
        completion: tuple[int, int] | None = None,
        initiative_run_id: str | None = None,
        error_category: str = "",
    ) -> dict[int, str]:
        from yuki.memory.self_reflection.initiative import advance_cursor

        query = select(MemorySelfReflectionRunModel).where(
            MemorySelfReflectionRunModel.status == "processing"
        )
        if run_id is not None:
            query = query.where(MemorySelfReflectionRunModel.id == run_id)
        else:
            query = query.where(MemorySelfReflectionRunModel.started_at <= started_before)
        result_count = (
            select(func.count(MemorySelfReflectionResultModel.id))
            .where(MemorySelfReflectionResultModel.run_id == MemorySelfReflectionRunModel.id)
            .correlate(MemorySelfReflectionRunModel)
            .scalar_subquery()
        )
        query = query.outerjoin(
            InitiativeReflectionWindowModel,
            InitiativeReflectionWindowModel.reflection_run_id == MemorySelfReflectionRunModel.id,
        ).order_by(MemorySelfReflectionRunModel.id)
        fields = (
            "id",
            "status",
            "checkpoint_json",
            "proposal_count",
            "committed_count",
            "attempt_count",
            "canonical_person_id",
            "canonical_space_id",
            "first_event_id",
            "last_event_id",
            "first_failed_at",
        )
        window_fields = (
            "reflection_run_id",
            "initiative_run_id",
            "first_receipt_id",
            "last_receipt_id",
        )
        while True:
            async with self._database.read_session() as reader:
                snapshots = tuple(
                    await reader.execute(
                        query.add_columns(InitiativeReflectionWindowModel, result_count)
                    )
                )
                runs = tuple(row[0] for row in snapshots)
                if not runs:
                    return {}
                counts = {run.id: count for run, _, count in snapshots}
                windows = {run.id: window for run, window, _ in snapshots if window is not None}
                expected = tuple(
                    (
                        *(getattr(run, key) for key in fields),
                        count,
                        *(
                            getattr(window, key) if window is not None else None
                            for key in window_fields
                        ),
                    )
                    for run, window, count in snapshots
                )
                if completion is not None and initiative_run_id is not None:
                    window = windows.get(cast(int, run_id))
                    if window is None or window.initiative_run_id != initiative_run_id:
                        raise RuntimeError("initiative reflection window disappeared")
                changes: dict[int, dict[str, Any]] = {}
                states: dict[int, tuple[MemorySelfReflectionStateModel, int]] = {}
                for run in runs:
                    checkpoint = json.loads(run.checkpoint_json) if run.checkpoint_json else {}
                    completed_counts = checkpoint.get("completed_counts")
                    committed = counts.get(run.id, 0)
                    done = (
                        completion is not None
                        or completed_counts is not None
                        or (committed > 0 and run.checkpoint_json is None)
                    )
                    change: dict[str, Any] = {}
                    if done and run.id not in windows:
                        state = await reader.scalar(
                            select(MemorySelfReflectionStateModel).where(
                                self._owner_state_filter(
                                    run.canonical_person_id, run.canonical_space_id
                                )
                            )
                        )
                        if state is None:
                            if completion is not None:
                                raise RuntimeError(
                                    "self-reflection state disappeared during completion"
                                )
                            change.update(status="failed", error_category="recovery_state_missing")
                            done = False
                        else:
                            previous = states.get(state.id)
                            states[state.id] = (
                                state,
                                max(run.last_event_id, previous[1] if previous else 0),
                            )
                    if done:
                        change.update(
                            status="completed",
                            proposal_count=completion[0]
                            if completion
                            else max(
                                run.proposal_count,
                                committed,
                                completed_counts[0] if completed_counts else 0,
                            ),
                            committed_count=completion[1]
                            if completion
                            else max(run.committed_count, committed),
                            error_category=None
                            if completion
                            else f"recovered:{error_category}"[:64],
                            checkpoint_json=None,
                            retry_state=None,
                        )
                    elif not change:
                        output = checkpoint.get("output", {})
                        change.update(
                            status="failed",
                            committed_count=max(run.committed_count, committed),
                            proposal_count=(
                                len(output.get("proposals", [])) + len(output.get("episodes", []))
                            )
                            if run.checkpoint_json
                            else run.proposal_count,
                            error_category=error_category[:64],
                            retry_state="waiting",
                            attempt_count=max(0, run.attempt_count - 1)
                            if error_category in {"daily_limit_reached", "preempted"}
                            else run.attempt_count,
                        )
                    changes[run.id] = change
                completed_ids = {
                    ident for ident, change in changes.items() if change["status"] == "completed"
                }
                advances = tuple(
                    [
                        await self._prepare_advance(
                            reader, state, through_event_id=through, completed_run_ids=completed_ids
                        )
                        for state, through in states.values()
                    ]
                )
            async with self._database.immediate_session() as writer:
                current = tuple(
                    await writer.execute(
                        query.add_columns(
                            result_count,
                            *(
                                getattr(InitiativeReflectionWindowModel, key)
                                for key in window_fields
                            ),
                        )
                    )
                )
                current_runs = tuple(row[0] for row in current)
                actual = tuple(
                    (
                        *(getattr(row[0], key) for key in fields),
                        *row[1:],
                    )
                    for row in current
                )
                if actual != expected:
                    continue
                current_states = [
                    await self._cursor_current(writer, prepared) for prepared in advances
                ]
                if not all(current_states):
                    continue
                now = datetime.now(UTC)
                for run in current_runs:
                    for key, value in changes[run.id].items():
                        setattr(run, key, value)
                    run.completed_at = now
                    if run.retry_state == "waiting":
                        run.first_failed_at = run.first_failed_at or now
                        run.next_attempt_at = now
                for prepared, state in zip(advances, current_states, strict=True):
                    assert state is not None
                    self._apply_cursor(state, prepared, now)
                for ident in completed_ids:
                    if ident in windows:
                        await advance_cursor(writer, windows[ident])
                return {ident: str(change["status"]) for ident, change in changes.items()}

    async def completed_result(self, run_id: int) -> tuple[int, int] | None:
        async with self._database.read_session() as session:
            run = await session.get(MemorySelfReflectionRunModel, run_id)
            if run is None or run.status != "completed":
                return None
            return int(run.proposal_count), int(run.committed_count)

    async def result_counts(self, run_id: int) -> tuple[int, int]:
        """Read durable counts after recovering a partially committed batch."""

        async with self._database.read_session() as session:
            run = await session.get(MemorySelfReflectionRunModel, run_id)
            if run is None:
                return 0, 0
            return int(run.proposal_count), int(run.committed_count)

    async def fail(self, run_id: int, error_category: str) -> None:
        async with self._database.immediate_session() as session:
            await session.execute(
                update(MemorySelfReflectionRunModel)
                .where(
                    MemorySelfReflectionRunModel.id == run_id,
                    MemorySelfReflectionRunModel.status == "processing",
                )
                .values(
                    status="failed",
                    error_category=error_category[:64],
                    completed_at=datetime.now(UTC),
                )
            )

    async def load_checkpoint(self, run_id: int) -> str | None:
        async with self._database.read_session() as session:
            return await session.scalar(
                select(MemorySelfReflectionRunModel.checkpoint_json).where(
                    MemorySelfReflectionRunModel.id == run_id
                )
            )

    async def save_checkpoint(
        self, run_id: int, value: str, *, expected: str | None = None
    ) -> None:
        event_map: dict[str, EventRecord] = {}
        tool_map: dict[str, StoredToolReceipt] = {}
        if expected is None:
            original = json.loads(value)
            event_map = _CHECKPOINT_EVENTS.validate_python(original.get("event_map", {}))
            tool_map = _CHECKPOINT_TOOLS.validate_python(original.get("tool_map", {}))

        while True:
            source_rows: tuple[ChatEventModel, ...] = ()
            receipts: dict[int, MemoryToolReceiptModel] = {}
            if expected is None:
                async with self._database.read_session() as reader:
                    event_query = select(ChatEventModel).where(
                        ChatEventModel.id.in_(tuple(event.id for event in event_map.values())),
                        keeper_event_clause(),
                    )
                    run = await reader.get(MemorySelfReflectionRunModel, run_id)
                    if run is None or run.checkpoint_json is not None:
                        raise MemoryPartitionResolutionError("checkpoint_changed")
                    source_rows = tuple(await reader.scalars(event_query))
                    events = {row.id: _event_record(row) for row in source_rows}
                    for source in event_map.values():
                        current = events.get(source.id)
                        if current is None or _checkpoint_event_source(
                            current
                        ) != _checkpoint_event_source(source):
                            raise MemoryPartitionResolutionError("checkpoint_source_changed")
                    receipts = {
                        row.id: row
                        for row in await reader.scalars(
                            select(MemoryToolReceiptModel).where(
                                MemoryToolReceiptModel.id.in_(
                                    tuple(receipt.id for receipt in tool_map.values())
                                )
                            )
                        )
                    }
                    for tool_source in tool_map.values():
                        receipt = receipts.get(tool_source.id)
                        if (
                            receipt is None
                            or (
                                receipt.trigger_event_id,
                                receipt.tool_name,
                                receipt.success,
                                receipt.result_excerpt,
                                receipt.initiative_run_id,
                                receipt.canonical_person_id,
                                receipt.canonical_space_id,
                            )
                            != (
                                tool_source.trigger_event_id,
                                tool_source.tool_name,
                                tool_source.success,
                                tool_source.result_excerpt,
                                tool_source.initiative_run_id,
                                run.canonical_person_id,
                                run.canonical_space_id,
                            )
                            or (
                                tool_source.bot_user_id
                                and tool_source.bot_user_id != receipt.bot_user_id
                            )
                            or (
                                tool_source.occurred_at is not None
                                and _utc(tool_source.occurred_at) != _utc(receipt.created_at)
                            )
                        ):
                            raise MemoryPartitionResolutionError("checkpoint_source_changed")
                    event_sources = _prepare_event_sources(reader, source_rows)
            async with self._database.immediate_session() as writer:
                if expected is None:
                    assert run is not None
                    if not await _events_current(writer, event_query, event_sources):
                        continue
                    current_receipts = tuple(
                        await writer.execute(
                            select(
                                MemoryToolReceiptModel.id,
                                MemoryToolReceiptModel.trigger_event_id,
                                MemoryToolReceiptModel.tool_name,
                                MemoryToolReceiptModel.success,
                                MemoryToolReceiptModel.result_excerpt,
                                MemoryToolReceiptModel.initiative_run_id,
                                MemoryToolReceiptModel.canonical_person_id,
                                MemoryToolReceiptModel.canonical_space_id,
                                MemoryToolReceiptModel.bot_user_id,
                                MemoryToolReceiptModel.created_at,
                            )
                            .where(MemoryToolReceiptModel.id.in_(tuple(receipts)))
                            .order_by(MemoryToolReceiptModel.id)
                        )
                    )
                    expected_receipts = tuple(
                        (
                            r.id,
                            r.trigger_event_id,
                            r.tool_name,
                            r.success,
                            r.result_excerpt,
                            r.initiative_run_id,
                            r.canonical_person_id,
                            r.canonical_space_id,
                            r.bot_user_id,
                            r.created_at,
                        )
                        for r in sorted(receipts.values(), key=lambda r: r.id)
                    )
                    current_owner = (
                        await writer.execute(
                            select(
                                MemorySelfReflectionRunModel.canonical_person_id,
                                MemorySelfReflectionRunModel.canonical_space_id,
                            ).where(MemorySelfReflectionRunModel.id == run_id)
                        )
                    ).one_or_none()
                    if tuple(
                        tuple(r) for r in current_receipts
                    ) != expected_receipts or current_owner != (
                        run.canonical_person_id,
                        run.canonical_space_id,
                    ):
                        continue
                changed = await writer.execute(
                    update(MemorySelfReflectionRunModel)
                    .where(
                        MemorySelfReflectionRunModel.id == run_id,
                        MemorySelfReflectionRunModel.checkpoint_json.is_(None)
                        if expected is None
                        else MemorySelfReflectionRunModel.checkpoint_json == expected,
                    )
                    .values(checkpoint_json=value)
                )
                if not cast(CursorResult[Any], changed).rowcount:
                    raise MemoryPartitionResolutionError("checkpoint_changed")
                return

    async def committed_results(self, run_id: int) -> set[tuple[str, int]]:
        async with self._database.read_session() as session:
            rows = (
                await session.scalars(
                    select(MemorySelfReflectionResultModel).where(
                        MemorySelfReflectionResultModel.run_id == run_id
                    )
                )
            ).all()
            return {(r.result_kind, r.result_index) for r in rows}

    async def cleanup_receipts(self, *, limit: int = 500) -> int:
        from sqlalchemy import delete

        from yuki.memory.self_reflection.db_models import (
            InitiativeReflectionWindowModel as Window,
        )

        # Discovery never acquires the writer. Reuse the complete eligibility
        # predicate in DELETE so a new evidence/window/expiry change wins.
        conditions = (
            MemoryToolReceiptModel.expires_at <= datetime.now(UTC),
            ~select(MemoryEvidenceModel.id)
            .where(MemoryEvidenceModel.tool_receipt_id == MemoryToolReceiptModel.id)
            .exists(),
            ~select(Window.reflection_run_id)
            .join(
                MemorySelfReflectionRunModel,
                MemorySelfReflectionRunModel.id == Window.reflection_run_id,
            )
            .where(
                Window.initiative_run_id == MemoryToolReceiptModel.initiative_run_id,
                MemoryToolReceiptModel.id >= Window.first_receipt_id,
                MemoryToolReceiptModel.id <= Window.last_receipt_id,
                MemorySelfReflectionRunModel.status != "completed",
            )
            .exists(),
            ~select(MemorySelfReflectionRunModel.id)
            .where(
                MemorySelfReflectionRunModel.checkpoint_json.is_not(None),
                text(
                    "EXISTS (SELECT 1 FROM json_each("
                    "memory_self_reflection_runs.checkpoint_json, '$.tool_map') AS source "
                    "WHERE json_extract(source.value, '$.id') = memory_tool_receipts.id)"
                ),
            )
            .exists(),
        )
        async with self._database.read_session() as session:
            ids = tuple(
                await session.scalars(
                    select(MemoryToolReceiptModel.id)
                    .where(*conditions)
                    .order_by(MemoryToolReceiptModel.expires_at, MemoryToolReceiptModel.id)
                    .limit(max(1, min(limit, 500)))
                )
            )
        if not ids:
            return 0
        async with self._database.immediate_session() as session:
            result = await session.execute(
                delete(MemoryToolReceiptModel).where(
                    MemoryToolReceiptModel.id.in_(ids), *conditions
                )
            )
        return int(cast(CursorResult[object], result).rowcount)

    @staticmethod
    async def _state(
        session: AsyncSession,
        row: MemorySelfReflectionStateModel,
    ) -> SelfReflectionState | None:
        person_id, space_id = require_xor_memory_owner(
            row.canonical_person_id,
            row.canonical_space_id,
        )
        external_person_id = (
            await project_active_person_external_id(session, person_id) if person_id else None
        )
        external_space_id = (
            await project_active_space_external_id(session, space_id) if space_id else None
        )
        return SelfReflectionState(
            id=row.id,
            conversation_key_hash=row.conversation_key_hash,
            bot_user_id=row.bot_user_id,
            canonical_person_id=person_id,
            canonical_space_id=space_id,
            external_person_id=external_person_id,
            external_space_id=external_space_id,
            last_event_id=row.last_event_id,
            latest_event_id=row.latest_event_id,
            pending_events=row.pending_events,
            pending_characters=row.pending_characters,
            pending_since=row.pending_since,
        )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _checkpoint_event_source(event: EventRecord) -> tuple[object, ...]:
    return (
        source_event_fingerprint(replace(event, occurred_at=_utc(event.occurred_at))),
        event.canonical_conversation_id,
        event.canonical_event_id,
        event.author_kind,
        event.author_person_id,
        event.author_presence_id,
        event.ingress_presence_id,
        event.visual_summary,
        event.external_payload,
    )
