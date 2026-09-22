"""Restart-safe episode cursors and receipts for low-frequency self-reflection."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.partition import (
    MemoryPartitionResolutionError,
    format_canonical_memory_partition,
    require_xor_memory_owner,
)
from qq_ai_bot.memory.projections import (
    project_active_person_external_id,
    project_active_space_external_id,
)
from qq_ai_bot.memory.self_reflection.models import (
    SelfReflectionBatch,
    SelfReflectionState,
    StoredToolReceipt,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemorySelfReflectionResultModel,
    MemorySelfReflectionRunModel,
    MemorySelfReflectionRuntimeModel,
    MemorySelfReflectionStateModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.persistence.repository_helpers import _event_record, keeper_event_clause
from qq_ai_bot.persistence.repository_records import event_author_is_yuki


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

    async def _advance_state(
        self,
        session: AsyncSession,
        state: MemorySelfReflectionStateModel,
        *,
        through_event_id: int,
        now: datetime,
    ) -> None:
        """Advance one owner cursor and rebuild pending counters from live events only."""

        processed_last_event_id = max(
            int(state.last_event_id),
            min(int(through_event_id), int(state.latest_event_id)),
        )
        gaps = list(
            (
                await session.scalars(
                    select(MemorySelfReflectionRunModel).where(
                        MemorySelfReflectionRunModel.conversation_key_hash
                        == state.conversation_key_hash,
                        MemorySelfReflectionRunModel.last_event_id > state.last_event_id,
                    )
                )
            ).all()
        )
        unfinished = [r.first_event_id for r in gaps if r.status != "completed"]
        if unfinished:
            processed_last_event_id = min(processed_last_event_id, min(unfinished) - 1)
        remaining_query = self._apply_event_scope(
            select(ChatEventModel).where(
                ChatEventModel.id > processed_last_event_id,
                ChatEventModel.id <= state.latest_event_id,
                ChatEventModel.event_kind == "message",
            ),
            state,
        )
        for completed_run in gaps:
            if completed_run.status == "completed":
                remaining_query = remaining_query.where(
                    or_(
                        ChatEventModel.id < completed_run.first_event_id,
                        ChatEventModel.id > completed_run.last_event_id,
                    )
                )
        remaining = list(
            (await session.scalars(remaining_query.order_by(ChatEventModel.id.asc()))).all()
        )
        nonempty = [item for item in remaining if item.evidence_content.strip()]
        receipt_filter = self._owner_receipt_filter(
            state.canonical_person_id, state.canonical_space_id
        )
        has_tool = bool(
            await session.scalar(
                select(MemoryToolReceiptModel.id).where(
                    receipt_filter,
                    MemoryToolReceiptModel.trigger_event_id > processed_last_event_id,
                    MemoryToolReceiptModel.trigger_event_id <= state.latest_event_id,
                    MemoryToolReceiptModel.expires_at > now,
                )
            )
        )
        state.last_event_id = processed_last_event_id
        state.pending_events = len(nonempty)
        state.pending_characters = sum(len(item.evidence_content) for item in nonempty)
        state.pending_since = nonempty[0].occurred_at if nonempty else None
        state.has_yuki_reply = any(
            item.direction == "outbound" and event_author_is_yuki(author_kind=item.author_kind)
            for item in remaining
        )
        state.has_tool_result = has_tool
        state.high_value_signal = False
        state.updated_at = now

    async def _reconcile_generation_boundary(
        self,
        session: AsyncSession,
        state: MemorySelfReflectionStateModel,
        *,
        now: datetime,
    ) -> None:
        """Discard the no-longer-live prefix left behind by an explicit conversation reset."""

        person_id, space_id = require_xor_memory_owner(
            state.canonical_person_id,
            state.canonical_space_id,
        )
        owner_filter = (
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
        conversation = await session.scalar(
            select(CanonicalConversationModel).where(owner_filter).limit(1)
        )
        if conversation is None:
            return
        live_boundary = max(
            int(conversation.starts_after_event_id),
            int(conversation.last_generation_change_event_id),
        )
        if live_boundary > int(state.last_event_id):
            await self._advance_state(
                session,
                state,
                through_event_id=live_boundary,
                now=now,
            )

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
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            session.autoflush = False
            runtime = await session.get(MemorySelfReflectionRuntimeModel, 1)
            if runtime is None:
                maximum = int(await session.scalar(select(func.max(ChatEventModel.id))) or 0)
                session.add(
                    MemorySelfReflectionRuntimeModel(
                        id=1,
                        last_scanned_event_id=maximum,
                        updated_at=now,
                    )
                )
                return 0
            rows = (
                await session.scalars(
                    select(ChatEventModel)
                    .where(
                        ChatEventModel.id > runtime.last_scanned_event_id,
                        ChatEventModel.event_kind == "message",
                        ChatEventModel.direction.in_(("inbound", "outbound")),
                    )
                    .order_by(ChatEventModel.id.asc())
                    .limit(max(1, limit))
                )
            ).all()
            from qq_ai_bot.identity.memory_guard import refuse_legacy_live_event

            live_ids: set[int] = set()
            if rows:
                live_ids = set(
                    await session.scalars(
                        select(ChatEventModel.id).where(
                            ChatEventModel.id.in_(tuple(row.id for row in rows)),
                            keeper_event_clause(),
                        )
                    )
                )
            staged_states: dict[str, MemorySelfReflectionStateModel] = {}
            for row in rows:
                if row.id not in live_ids:
                    continue
                if await refuse_legacy_live_event(session, row):
                    continue
                conversation = await session.get(
                    CanonicalConversationModel, row.canonical_conversation_id
                )
                if conversation is None:
                    continue
                owners = self._owner_ids_from_live_conversation(
                    conversation, event_scope_type=row.scope_type
                )
                if owners is None:
                    continue
                person_id, space_id = require_xor_memory_owner(*owners)
                key_hash = hashlib.sha256(
                    format_canonical_memory_partition(
                        person_id=person_id, space_id=space_id
                    ).encode("utf-8")
                ).hexdigest()
                state = await session.scalar(
                    select(MemorySelfReflectionStateModel).where(
                        self._owner_state_filter(person_id, space_id)
                    )
                )
                state = staged_states.get(key_hash, state)
                content = row.evidence_content.strip()
                if state is None:
                    state = MemorySelfReflectionStateModel(
                        conversation_key_hash=key_hash,
                        bot_user_id=row.bot_user_id,
                        canonical_person_id=person_id,
                        canonical_space_id=space_id,
                        last_event_id=runtime.last_scanned_event_id,
                        latest_event_id=row.id,
                        pending_events=0,
                        pending_characters=0,
                        pending_since=row.occurred_at,
                        has_yuki_reply=False,
                        has_tool_result=False,
                        high_value_signal=False,
                        updated_at=now,
                    )
                    session.add(state)
                    staged_states[key_hash] = state
                if content:
                    runtime.ingress_events_total += 1
                    state.pending_events += 1
                    state.pending_characters += len(content)
                    state.pending_since = state.pending_since or row.occurred_at
                state.last_policy_reason = None
                state.latest_event_id = row.id
                state.has_yuki_reply = state.has_yuki_reply or (
                    row.direction == "outbound"
                    and event_author_is_yuki(author_kind=row.author_kind)
                )
                state.high_value_signal = False
                state.updated_at = now
            if rows:
                runtime.last_scanned_event_id = rows[-1].id
                runtime.updated_at = now
            return len(rows)

    @property
    def database(self) -> Database:
        return self._database

    async def claim_due(
        self,
        *,
        scheduled_slot: str,
        local_date: str,
        event_threshold: int,
        character_threshold: int,
        max_wait_seconds: float,
        max_sessions: int,
        max_daily_calls: int,
        max_events: int,
        max_characters: int,
        low_event_threshold: int | None = None,
        low_character_threshold: int | None = None,
        natural_gap_seconds: float | None = None,
        context_events: int = 4,
        force: bool = False,
        excluded_conversation_keys: frozenset[str] = frozenset(),
        cycle_id: str | None = None,
        bot_display_name: str = "Yuki",
        timezone: str = "Asia/Shanghai",
    ) -> tuple[SelfReflectionBatch, ...]:
        if max_sessions > 0 and max_daily_calls > 0:
            from qq_ai_bot.memory.self_reflection.initiative import claim_initiative

            initiatives = await claim_initiative(
                self._database,
                max_characters=max_characters,
                excluded_conversation_keys=excluded_conversation_keys,
                cycle_id=cycle_id,
            )
            if initiatives:
                return initiatives
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            session.autoflush = False
            used = 0
            available = min(1, max_sessions, max(0, max_daily_calls - used))
            if available <= 0:
                return ()
            waited_before = now - timedelta(seconds=max_wait_seconds)
            state_query = select(MemorySelfReflectionStateModel).where(
                MemorySelfReflectionStateModel.pending_events > 0,
                self._xor_owner_state_clause(),
            )
            if excluded_conversation_keys:
                state_query = state_query.where(
                    MemorySelfReflectionStateModel.conversation_key_hash.not_in(
                        excluded_conversation_keys
                    )
                )
            if not force:
                state_query = state_query.where(
                    or_(
                        MemorySelfReflectionStateModel.pending_events >= event_threshold,
                        MemorySelfReflectionStateModel.pending_characters >= character_threshold,
                        MemorySelfReflectionStateModel.pending_since <= waited_before,
                    )
                )
            states = (
                await session.scalars(
                    state_query.order_by(
                        MemorySelfReflectionStateModel.pending_since.asc(),
                    )
                )
            ).all()
            claimed: list[SelfReflectionBatch] = []
            for row in states:
                await self._reconcile_generation_boundary(session, row, now=now)
                if row.pending_events <= 0:
                    continue
                receipt_filter = self._owner_receipt_filter(
                    row.canonical_person_id, row.canonical_space_id
                )
                has_tool = bool(
                    await session.scalar(
                        select(MemoryToolReceiptModel.id).where(
                            receipt_filter,
                            MemoryToolReceiptModel.trigger_event_id > row.last_event_id,
                            MemoryToolReceiptModel.expires_at > now,
                        )
                    )
                )
                if not (row.has_yuki_reply or row.has_tool_result or has_tool):
                    if row.pending_since and _utc(row.pending_since) <= waited_before:
                        await self._advance_state(
                            session, row, through_event_id=row.latest_event_id, now=now
                        )
                        row.last_policy_reason = "no_self_evidence"
                        row.last_policy_event_id = row.last_event_id
                    continue
                outstanding = list(
                    (
                        await session.scalars(
                            select(MemorySelfReflectionRunModel)
                            .where(
                                MemorySelfReflectionRunModel.conversation_key_hash
                                == row.conversation_key_hash,
                                MemorySelfReflectionRunModel.last_event_id > row.last_event_id,
                            )
                            .order_by(MemorySelfReflectionRunModel.first_event_id)
                        )
                    ).all()
                )
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
                if any(r.status == "processing" for r in outstanding):
                    continue
                event_query = self._apply_event_scope(
                    select(ChatEventModel).where(
                        ChatEventModel.id > row.last_event_id,
                        ChatEventModel.id <= row.latest_event_id,
                        ChatEventModel.event_kind == "message",
                    ),
                    row,
                )
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
                candidate_rows = list(
                    (
                        await session.scalars(
                            event_query.order_by(ChatEventModel.id.asc()).limit(max_events)
                        )
                    ).all()
                )
                from qq_ai_bot.event_prompt import ChatEventPromptRenderer

                renderer = ChatEventPromptRenderer(
                    tuple(_event_record(r) for r in candidate_rows),
                    bot_display_name=bot_display_name,
                    timezone=timezone,
                )
                event_rows: list[ChatEventModel] = []
                input_characters = 0
                from qq_ai_bot.identity.memory_guard import refuse_legacy_live_event

                for item in candidate_rows:
                    if await refuse_legacy_live_event(session, item):
                        continue
                    item_characters = len(renderer.render_event(_event_record(item)))
                    if input_characters + item_characters > max_characters:
                        if not event_rows:
                            # Persist this source before classifying it; otherwise an oversized
                            # event would crash every cycle without a resumable failure record.
                            event_rows.append(item)
                        break
                    event_rows.append(item)
                    input_characters += item_characters
                if (
                    event_rows
                    and retry is None
                    and natural_gap_seconds is not None
                    and low_event_threshold is not None
                    and low_character_threshold is not None
                    and (
                        row.pending_events >= event_threshold
                        or row.pending_characters >= character_threshold
                    )
                ):
                    event_rows = self._watermark_segment(
                        event_rows,
                        low_event_threshold=low_event_threshold,
                        low_character_threshold=low_character_threshold,
                        natural_gap_seconds=natural_gap_seconds,
                    )
                if not event_rows and outstanding:
                    continue
                if not event_rows:
                    # Suppression or a repaired canonical chain may invalidate every
                    # remaining row after it was counted. Do not spin on that dead tail.
                    await self._advance_state(
                        session,
                        row,
                        through_event_id=row.latest_event_id,
                        now=now,
                    )
                    continue
                if retry is not None and (
                    event_rows[0].id != retry.first_event_id
                    or event_rows[-1].id != retry.last_event_id
                ):
                    retry.retry_state = "isolated"
                    retry.error_category = "source_range_changed"
                    continue
                context_query = self._apply_event_scope(
                    select(ChatEventModel).where(
                        ChatEventModel.id < event_rows[0].id,
                        ChatEventModel.event_kind == "message",
                    ),
                    row,
                )
                context_rows = list(
                    (
                        await session.scalars(
                            context_query.order_by(ChatEventModel.id.desc()).limit(
                                max(0, context_events)
                            )
                        )
                    ).all()
                )
                context_rows.reverse()
                reason = (
                    "manual"
                    if force
                    else self._trigger_reason(
                        row,
                        event_threshold=event_threshold,
                        character_threshold=character_threshold,
                    )
                )
                run_person_id = row.canonical_person_id
                run_space_id = row.canonical_space_id
                projected_state = await self._state(session, row, has_tool=has_tool)
                if projected_state is None:
                    continue
                fingerprint = hashlib.sha256(
                    repr([(r.id, r.evidence_content) for r in event_rows]).encode()
                ).hexdigest()
                if (
                    retry is not None
                    and retry.input_fingerprint
                    and fingerprint != retry.input_fingerprint
                ):
                    retry.retry_state = "isolated"
                    retry.error_category = "source_changed"
                    continue
                input_characters = sum(len(item.evidence_content) for item in event_rows)
                run_values = {
                    "conversation_key_hash": row.conversation_key_hash,
                    "bot_user_id": row.bot_user_id,
                    "canonical_person_id": run_person_id,
                    "canonical_space_id": run_space_id,
                    "scheduled_slot": scheduled_slot,
                    "trigger_reason": reason,
                    "first_event_id": event_rows[0].id,
                    "last_event_id": event_rows[-1].id,
                    "status": "processing",
                    "proposal_count": 0,
                    "committed_count": 0,
                    "started_at": now,
                    "cycle_id": cycle_id,
                    "attempt_count": 1,
                    "input_fingerprint": fingerprint,
                    "processed_events": len(event_rows),
                    "processed_characters": input_characters,
                }
                if retry is not None:
                    retry.status = "processing"
                    retry.started_at = now
                    retry.completed_at = None
                    retry.cycle_id = cycle_id
                    retry.attempt_count += 1
                    retry.input_fingerprint = fingerprint
                    retry.processed_events = len(event_rows)
                    retry.processed_characters = input_characters
                    run_id = retry.id
                else:
                    conflict, conflict_where = self._run_conflict_target(row)
                    insert_stmt = insert(MemorySelfReflectionRunModel).values(**run_values)
                    if conflict_where is not None:
                        insert_stmt = insert_stmt.on_conflict_do_nothing(
                            index_elements=conflict,
                            index_where=conflict_where,
                        )
                    else:
                        insert_stmt = insert_stmt.on_conflict_do_nothing(index_elements=conflict)
                    inserted_id = await session.scalar(
                        insert_stmt.returning(MemorySelfReflectionRunModel.id)
                    )
                    if inserted_id is None:
                        continue
                    run_id = inserted_id
                claimed.append(
                    SelfReflectionBatch(
                        state=projected_state,
                        events=tuple(_event_record(item) for item in event_rows),
                        context_events=tuple(_event_record(item) for item in context_rows),
                        trigger_reason=reason,
                        scheduled_slot=scheduled_slot,
                        run_id=run_id,
                        max_input_characters=max_characters,
                    )
                )
                if len(claimed) >= available:
                    break
            return tuple(claimed)

    @staticmethod
    def _watermark_segment(
        rows: list[ChatEventModel],
        *,
        low_event_threshold: int,
        low_character_threshold: int,
        natural_gap_seconds: float,
    ) -> list[ChatEventModel]:
        """Cut one oldest non-overlapping segment at the latest natural pause."""

        if len(rows) < 2:
            return rows
        characters = 0
        boundary: int | None = None
        for index, item in enumerate(rows[:-1], start=1):
            characters += len(item.evidence_content)
            if index < low_event_threshold and characters < low_character_threshold:
                continue
            gap_seconds = (rows[index].occurred_at - item.occurred_at).total_seconds()
            if gap_seconds >= natural_gap_seconds:
                boundary = index
        return rows[:boundary] if boundary is not None else rows

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

        async with self._database.sessions() as session:
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
            async with self._database.sessions() as session:
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
                        .limit(max(1, limit))
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
        async with self._database.sessions() as session:
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
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            session.autoflush = False
            run = await session.get(MemorySelfReflectionRunModel, batch.run_id)
            if run is None or run.status != "processing":
                return
            run.status = "completed"
            run.proposal_count = proposals
            run.committed_count = committed
            run.error_category = None
            run.retry_state = None
            run.checkpoint_json = None
            run.completed_at = now
            if batch.initiative_run_id is not None:
                from qq_ai_bot.memory.self_reflection.db_models import (
                    InitiativeReflectionWindowModel,
                )
                from qq_ai_bot.memory.self_reflection.initiative import advance_cursor

                window = await session.get(InitiativeReflectionWindowModel, run.id)
                if window is None or window.initiative_run_id != batch.initiative_run_id:
                    raise RuntimeError("initiative reflection window disappeared")
                await advance_cursor(session, window)
                return
            state = await session.get(MemorySelfReflectionStateModel, batch.state.id)
            if state is None:
                raise RuntimeError("self-reflection state disappeared during completion")
            await self._advance_state(
                session,
                state,
                through_event_id=batch.events[-1].id,
                now=now,
            )

    async def recover_interrupted(self, run_id: int, error_category: str) -> str | None:
        """Finalize one interrupted run without replaying already committed effects."""

        async with self._database.sessions() as session, session.begin():
            session.autoflush = False
            run = await session.get(MemorySelfReflectionRunModel, run_id)
            if run is None or run.status != "processing":
                return None
            return await self._recover_processing_run(
                session,
                run,
                error_category=error_category,
                now=datetime.now(UTC),
            )

    async def recover_stale_runs(self, *, started_before: datetime) -> int:
        """Recover abandoned processing rows left by a crash or hard process stop."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            session.autoflush = False
            rows = (
                await session.scalars(
                    select(MemorySelfReflectionRunModel).where(
                        MemorySelfReflectionRunModel.status == "processing",
                        MemorySelfReflectionRunModel.started_at <= started_before,
                    )
                )
            ).all()
            for run in rows:
                await self._recover_processing_run(
                    session,
                    run,
                    error_category="stale_processing",
                    now=now,
                )
            return len(rows)

    async def completed_result(self, run_id: int) -> tuple[int, int] | None:
        async with self._database.sessions() as session:
            run = await session.get(MemorySelfReflectionRunModel, run_id)
            if run is None or run.status != "completed":
                return None
            return int(run.proposal_count), int(run.committed_count)

    async def result_counts(self, run_id: int) -> tuple[int, int]:
        """Read durable counts after recovering a partially committed batch."""

        async with self._database.sessions() as session:
            run = await session.get(MemorySelfReflectionRunModel, run_id)
            if run is None:
                return 0, 0
            return int(run.proposal_count), int(run.committed_count)

    async def _recover_processing_run(
        self,
        session: AsyncSession,
        run: MemorySelfReflectionRunModel,
        *,
        error_category: str,
        now: datetime,
    ) -> str:
        """Use atomic result mappings as the commit checkpoint for recovery."""

        committed = int(
            await session.scalar(
                select(func.count(MemorySelfReflectionResultModel.id)).where(
                    MemorySelfReflectionResultModel.run_id == run.id
                )
            )
            or 0
        )
        completed_counts = (
            json.loads(run.checkpoint_json).get("completed_counts") if run.checkpoint_json else None
        )
        if completed_counts is not None or (committed and run.checkpoint_json is None):
            from qq_ai_bot.memory.self_reflection.db_models import InitiativeReflectionWindowModel
            from qq_ai_bot.memory.self_reflection.initiative import advance_cursor

            window = await session.get(InitiativeReflectionWindowModel, run.id)
            if window is not None:
                await advance_cursor(session, window)
                run.status = "completed"
                run.proposal_count = max(
                    int(run.proposal_count),
                    committed,
                    completed_counts[0] if completed_counts else 0,
                )
                run.committed_count = max(int(run.committed_count), committed)
                run.error_category = f"recovered:{error_category}"[:64]
                run.checkpoint_json = None
                run.retry_state = None
                run.completed_at = now
                return "completed"
            state = await session.scalar(
                select(MemorySelfReflectionStateModel).where(
                    self._owner_state_filter(run.canonical_person_id, run.canonical_space_id)
                )
            )
            if state is None:
                run.status = "failed"
                run.error_category = "recovery_state_missing"
                run.completed_at = now
                return "failed"
            run.status = "completed"
            await self._advance_state(
                session,
                state,
                through_event_id=run.last_event_id,
                now=now,
            )
            run.status = "completed"
            run.proposal_count = max(
                int(run.proposal_count), committed, completed_counts[0] if completed_counts else 0
            )
            run.committed_count = max(int(run.committed_count), committed)
            run.error_category = f"recovered:{error_category}"[:64]
            run.checkpoint_json = None
            run.retry_state = None
            run.completed_at = now
            return "completed"
        run.committed_count = max(int(run.committed_count), committed)
        if run.checkpoint_json:
            output = json.loads(run.checkpoint_json).get("output", {})
            run.proposal_count = len(output.get("proposals", [])) + len(output.get("episodes", []))
        run.status = "failed"
        run.error_category = error_category[:64]
        run.completed_at = now
        run.first_failed_at = run.first_failed_at or now
        if error_category in {"daily_limit_reached", "preempted"}:
            run.attempt_count = max(0, run.attempt_count - 1)
        run.retry_state = "isolated" if run.attempt_count >= 3 else "waiting"
        run.next_attempt_at = now + timedelta(
            minutes=(5, 15, 30)[min(2, max(0, run.attempt_count - 1))]
        )
        return "failed"

    async def fail(self, run_id: int, error_category: str) -> None:
        async with self._database.sessions() as session, session.begin():
            session.autoflush = False
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
        async with self._database.sessions() as session:
            return await session.scalar(
                select(MemorySelfReflectionRunModel.checkpoint_json).where(
                    MemorySelfReflectionRunModel.id == run_id
                )
            )

    async def save_checkpoint(self, run_id: int, value: str) -> None:
        if len(value.encode()) > 4 * 1024 * 1024:
            raise ValueError("reflection_checkpoint_too_large")
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemorySelfReflectionRunModel)
                .where(MemorySelfReflectionRunModel.id == run_id)
                .values(checkpoint_json=value)
            )

    async def committed_results(self, run_id: int) -> set[tuple[str, int]]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(MemorySelfReflectionResultModel).where(
                        MemorySelfReflectionResultModel.run_id == run_id
                    )
                )
            ).all()
            return {(r.result_kind, r.result_index) for r in rows}

    async def cleanup_receipts(self) -> int:
        from sqlalchemy import delete

        from qq_ai_bot.memory.self_reflection.db_models import (
            InitiativeReflectionWindowModel as Window,
        )

        async with self._database.sessions() as session, session.begin():
            session.autoflush = False
            referenced = (
                select(MemoryEvidenceModel.id)
                .where(MemoryEvidenceModel.tool_receipt_id == MemoryToolReceiptModel.id)
                .exists()
            )
            result = await session.execute(
                delete(MemoryToolReceiptModel).where(
                    MemoryToolReceiptModel.expires_at <= datetime.now(UTC),
                    ~referenced,
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
                )
            )
            return int(cast(CursorResult[object], result).rowcount)

    @staticmethod
    async def _state(
        session: AsyncSession,
        row: MemorySelfReflectionStateModel,
        *,
        has_tool: bool,
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
        if person_id and external_person_id is None:
            return None
        if space_id and external_space_id is None:
            return None
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
            has_yuki_reply=row.has_yuki_reply,
            has_tool_result=row.has_tool_result or has_tool,
            high_value_signal=False,
        )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
