"""Restart-safe episode cursors and receipts for low-frequency self-reflection."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.sql.elements import ColumnElement

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.identity.runtime import identity_runtime_is_complete_v2
from qq_ai_bot.memory.partition import (
    MemoryPartitionResolutionError,
    format_canonical_memory_partition,
    require_xor_memory_owner,
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
    MemorySelfReflectionRunModel,
    MemorySelfReflectionRuntimeModel,
    MemorySelfReflectionStateModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.persistence.repository_helpers import _event_record, keeper_event_clause
from qq_ai_bot.persistence.repository_records import event_author_is_yuki


def conversation_key_hash(
    scope_type: ScopeType,
    *,
    group_id: str | None,
    private_peer_user_id: str | None,
) -> str:
    key = (
        f"group:{group_id}" if scope_type is ScopeType.GROUP else f"private:{private_peer_user_id}"
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


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
        row: MemorySelfReflectionStateModel, *, complete_v2: bool
    ) -> tuple[list[str], ColumnElement[bool] | None]:
        if not complete_v2:
            return (["conversation_key_hash", "bot_user_id", "scheduled_slot"], None)
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
        *,
        complete_v2: bool,
    ) -> Any:
        if not complete_v2:
            if row.scope_type == ScopeType.GROUP.value:
                return query.where(ChatEventModel.group_id == row.group_id)
            return query.where(ChatEventModel.private_peer_user_id == row.private_peer_user_id)
        return query.join(
            CanonicalConversationModel,
            ChatEventModel.canonical_conversation_id == CanonicalConversationModel.id,
        ).where(
            self._canonical_conversation_event_scope(
                row.canonical_person_id,
                row.canonical_space_id,
            ),
            keeper_event_clause(),
        )

    async def scan_new_events(self, *, limit: int = 5000) -> int:
        """Accumulate only post-deployment events; first startup establishes a baseline."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            # Older workers accidentally copied a group sender into this
            # private-only field. Existing deployments heal on the next scan.
            await session.execute(
                update(MemorySelfReflectionStateModel)
                .where(
                    MemorySelfReflectionStateModel.scope_type == ScopeType.GROUP.value,
                    MemorySelfReflectionStateModel.private_peer_user_id.is_not(None),
                )
                .values(private_peer_user_id=None, updated_at=now)
            )
            await session.execute(
                update(MemorySelfReflectionStateModel)
                .where(MemorySelfReflectionStateModel.high_value_signal.is_(True))
                .values(high_value_signal=False, updated_at=now)
            )
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

            complete_v2 = await identity_runtime_is_complete_v2(session)
            live_ids: set[int] | None = None
            if complete_v2 and rows:
                live_ids = set(
                    await session.scalars(
                        select(ChatEventModel.id).where(
                            ChatEventModel.id.in_(tuple(row.id for row in rows)),
                            keeper_event_clause(),
                        )
                    )
                )
            for row in rows:
                if live_ids is not None and row.id not in live_ids:
                    continue
                if await refuse_legacy_live_event(session, row):
                    continue
                scope_type = ScopeType(row.scope_type)
                peer: str | None = None
                if scope_type is ScopeType.PRIVATE:
                    peer = row.private_peer_user_id or (
                        row.sender_user_id if row.direction == "inbound" else None
                    )
                    if not peer:
                        continue
                person_id: str | None = None
                space_id: str | None = None
                if complete_v2:
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
                else:
                    key_hash = conversation_key_hash(
                        scope_type,
                        group_id=row.group_id,
                        private_peer_user_id=peer,
                    )
                    state = await session.scalar(
                        select(MemorySelfReflectionStateModel).where(
                            MemorySelfReflectionStateModel.conversation_key_hash == key_hash,
                            MemorySelfReflectionStateModel.bot_user_id == row.bot_user_id,
                        )
                    )
                    from qq_ai_bot.identity.owner_dual_write import (
                        optional_reflection_state_owners,
                    )

                    if state is None or (
                        state.canonical_person_id is None and state.canonical_space_id is None
                    ):
                        person_id, space_id = await optional_reflection_state_owners(
                            session,
                            scope_type=row.scope_type,
                            group_id=row.group_id,
                            private_peer_user_id=peer,
                            exclude_state_id=None if state is None else state.id,
                        )
                content = row.content.strip()
                if state is None:
                    state = MemorySelfReflectionStateModel(
                        conversation_key_hash=key_hash,
                        bot_user_id=row.bot_user_id,
                        canonical_person_id=person_id,
                        canonical_space_id=space_id,
                        scope_type=row.scope_type,
                        group_id=row.group_id,
                        private_peer_user_id=peer,
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
                elif not complete_v2 and (
                    state.canonical_person_id is None and state.canonical_space_id is None
                ):
                    state.canonical_person_id = person_id
                    state.canonical_space_id = space_id
                if content:
                    state.pending_events += 1
                    state.pending_characters += len(content)
                    state.pending_since = state.pending_since or row.occurred_at
                state.latest_event_id = row.id
                state.has_yuki_reply = state.has_yuki_reply or (
                    row.direction == "outbound"
                    and event_author_is_yuki(
                        author_kind=row.author_kind,
                        sender_user_id=row.sender_user_id,
                        bot_user_id=row.bot_user_id,
                    )
                )
                state.high_value_signal = False
                state.updated_at = now
            if rows:
                runtime.last_scanned_event_id = rows[-1].id
                runtime.updated_at = now
            return len(rows)

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
    ) -> tuple[SelfReflectionBatch, ...]:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            used = int(
                await session.scalar(
                    select(func.count(MemorySelfReflectionRunModel.id)).where(
                        MemorySelfReflectionRunModel.scheduled_slot.like(f"{local_date}:%")
                    )
                )
                or 0
            )
            available = min(max_sessions, max(0, max_daily_calls - used))
            if available <= 0:
                return ()
            waited_before = now - timedelta(seconds=max_wait_seconds)
            complete_v2 = await identity_runtime_is_complete_v2(session)
            state_query = select(MemorySelfReflectionStateModel).where(
                MemorySelfReflectionStateModel.pending_events > 0
            )
            if complete_v2:
                state_query = state_query.where(self._xor_owner_state_clause())
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
                    ).limit(available * 3)
                )
            ).all()
            claimed: list[SelfReflectionBatch] = []
            for row in states:
                receipt_filter = (
                    self._owner_receipt_filter(row.canonical_person_id, row.canonical_space_id)
                    if complete_v2
                    else and_(
                        MemoryToolReceiptModel.conversation_key_hash == row.conversation_key_hash,
                        MemoryToolReceiptModel.bot_user_id == row.bot_user_id,
                    )
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
                    continue
                event_query = self._apply_event_scope(
                    select(ChatEventModel).where(
                        ChatEventModel.id > row.last_event_id,
                        ChatEventModel.id <= row.latest_event_id,
                        ChatEventModel.event_kind == "message",
                    ),
                    row,
                    complete_v2=complete_v2,
                )
                if not complete_v2:
                    event_query = event_query.where(ChatEventModel.bot_user_id == row.bot_user_id)
                candidate_rows = list(
                    (
                        await session.scalars(
                            event_query.order_by(ChatEventModel.id.asc()).limit(max_events)
                        )
                    ).all()
                )
                event_rows: list[ChatEventModel] = []
                input_characters = 0
                from qq_ai_bot.identity.memory_guard import refuse_legacy_live_event

                for item in candidate_rows:
                    if await refuse_legacy_live_event(session, item):
                        continue
                    item_characters = len(item.content)
                    if event_rows and input_characters + item_characters > max_characters:
                        break
                    event_rows.append(item)
                    input_characters += item_characters
                if (
                    event_rows
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
                if not event_rows:
                    continue
                context_query = self._apply_event_scope(
                    select(ChatEventModel).where(
                        ChatEventModel.id < event_rows[0].id,
                        ChatEventModel.event_kind == "message",
                    ),
                    row,
                    complete_v2=complete_v2,
                )
                if not complete_v2:
                    context_query = context_query.where(
                        ChatEventModel.bot_user_id == row.bot_user_id
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
                if not complete_v2:
                    from qq_ai_bot.identity.owner_dual_write import (
                        optional_reflection_run_owners,
                    )

                    run_person_id, run_space_id = await optional_reflection_run_owners(
                        session,
                        row.canonical_person_id,
                        row.canonical_space_id,
                        scheduled_slot,
                    )
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
                }
                conflict, conflict_where = self._run_conflict_target(row, complete_v2=complete_v2)
                insert_stmt = insert(MemorySelfReflectionRunModel).values(**run_values)
                if conflict_where is not None:
                    insert_stmt = insert_stmt.on_conflict_do_nothing(
                        index_elements=conflict,
                        index_where=conflict_where,
                    )
                else:
                    insert_stmt = insert_stmt.on_conflict_do_nothing(index_elements=conflict)
                run_id = await session.scalar(
                    insert_stmt.returning(MemorySelfReflectionRunModel.id)
                )
                if run_id is None:
                    continue
                claimed.append(
                    SelfReflectionBatch(
                        state=self._state(row, has_tool=has_tool),
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
            characters += len(item.content)
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
            complete_v2 = await identity_runtime_is_complete_v2(session)
            pending_query = select(func.count(MemorySelfReflectionStateModel.id)).where(
                MemorySelfReflectionStateModel.pending_events > 0
            )
            if complete_v2:
                pending_query = pending_query.where(self._xor_owner_state_clause())
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
        async with self._database.sessions() as session:
            complete_v2 = await identity_runtime_is_complete_v2(session)
            state_row = await session.get(MemorySelfReflectionStateModel, batch.state.id)
            if complete_v2:
                if state_row is None:
                    raise MemoryPartitionResolutionError("missing_owner")
                receipt_filter = self._owner_receipt_filter(
                    state_row.canonical_person_id,
                    state_row.canonical_space_id,
                )
            else:
                receipt_filter = and_(
                    MemoryToolReceiptModel.conversation_key_hash
                    == batch.state.conversation_key_hash,
                    MemoryToolReceiptModel.bot_user_id == batch.state.bot_user_id,
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
            await session.execute(
                update(MemorySelfReflectionRunModel)
                .where(MemorySelfReflectionRunModel.id == batch.run_id)
                .values(
                    status="completed",
                    proposal_count=proposals,
                    committed_count=committed,
                    completed_at=now,
                )
            )
            state = await session.get(MemorySelfReflectionStateModel, batch.state.id)
            if state is None:
                raise RuntimeError("self-reflection state disappeared during completion")
            processed_last_event_id = batch.events[-1].id
            complete_v2 = await identity_runtime_is_complete_v2(session)
            remaining_query = self._apply_event_scope(
                select(ChatEventModel).where(
                    ChatEventModel.id > processed_last_event_id,
                    ChatEventModel.id <= state.latest_event_id,
                    ChatEventModel.event_kind == "message",
                ),
                state,
                complete_v2=complete_v2,
            )
            if not complete_v2:
                remaining_query = remaining_query.where(
                    ChatEventModel.bot_user_id == state.bot_user_id
                )
            remaining = list(
                (await session.scalars(remaining_query.order_by(ChatEventModel.id.asc()))).all()
            )
            nonempty = [item for item in remaining if item.content.strip()]
            receipt_filter = (
                self._owner_receipt_filter(state.canonical_person_id, state.canonical_space_id)
                if complete_v2
                else and_(
                    MemoryToolReceiptModel.conversation_key_hash == state.conversation_key_hash,
                    MemoryToolReceiptModel.bot_user_id == state.bot_user_id,
                )
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
            state.pending_characters = sum(len(item.content) for item in nonempty)
            state.pending_since = nonempty[0].occurred_at if nonempty else None
            state.has_yuki_reply = any(
                item.direction == "outbound"
                and event_author_is_yuki(
                    author_kind=item.author_kind,
                    sender_user_id=item.sender_user_id,
                    bot_user_id=item.bot_user_id,
                )
                for item in remaining
            )
            state.has_tool_result = has_tool
            state.high_value_signal = False
            state.updated_at = now

    async def fail(self, run_id: int, error_category: str) -> None:
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemorySelfReflectionRunModel)
                .where(MemorySelfReflectionRunModel.id == run_id)
                .values(
                    status="failed",
                    error_category=error_category[:64],
                    completed_at=datetime.now(UTC),
                )
            )

    async def cleanup_receipts(self) -> int:
        from sqlalchemy import delete

        async with self._database.sessions() as session, session.begin():
            referenced = (
                select(MemoryEvidenceModel.id)
                .where(MemoryEvidenceModel.tool_receipt_id == MemoryToolReceiptModel.id)
                .exists()
            )
            result = await session.execute(
                delete(MemoryToolReceiptModel).where(
                    MemoryToolReceiptModel.expires_at <= datetime.now(UTC),
                    ~referenced,
                )
            )
            return int(cast(CursorResult[object], result).rowcount)

    @staticmethod
    def _state(row: MemorySelfReflectionStateModel, *, has_tool: bool) -> SelfReflectionState:
        scope_type = ScopeType(row.scope_type)
        return SelfReflectionState(
            id=row.id,
            conversation_key_hash=row.conversation_key_hash,
            bot_user_id=row.bot_user_id,
            scope_type=scope_type,
            group_id=row.group_id,
            private_peer_user_id=(
                None if scope_type is ScopeType.GROUP else row.private_peer_user_id
            ),
            last_event_id=row.last_event_id,
            latest_event_id=row.latest_event_id,
            pending_events=row.pending_events,
            pending_characters=row.pending_characters,
            pending_since=row.pending_since,
            has_yuki_reply=row.has_yuki_reply,
            has_tool_result=row.has_tool_result or has_tool,
            high_value_signal=False,
        )
