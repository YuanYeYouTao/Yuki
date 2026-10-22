"""Receipt-only windows reuse the ordinary reflection run, budget and checkpoints."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, String, and_, cast, func, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.autonomy_db_models import InitiativeRunModel
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.db_models import (
    AccountModel,
    CanonicalSpaceModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.memory.partition import MemoryPartitionResolutionError
from yuki.memory.self_origin import resolve_self_origin
from yuki.memory.self_reflection.db_models import (
    InitiativeReflectionCursorModel as Cursor,
)
from yuki.memory.self_reflection.db_models import (
    InitiativeReflectionWindowModel as Window,
)
from yuki.memory.self_reflection.models import SelfReflectionBatch, SelfReflectionState
from yuki.persistence.database import Database
from yuki.persistence.models import MemorySelfReflectionRunModel, MemoryToolReceiptModel


async def advance_cursor(session: AsyncSession, window: Window) -> None:
    statement = insert(Cursor).values(
        initiative_run_id=window.initiative_run_id,
        last_receipt_id=window.last_receipt_id,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["initiative_run_id"],
            set_={"last_receipt_id": func.max(Cursor.last_receipt_id, window.last_receipt_id)},
        )
    )


async def initiative_backlog(database: Database) -> dict[str, int]:
    """Report receipt workload independently; never call receipts chat events."""
    async with database.read_session() as session:
        row = (
            await session.execute(
                select(
                    func.count(MemoryToolReceiptModel.id),
                    func.count(func.distinct(MemoryToolReceiptModel.initiative_run_id)),
                )
                .outerjoin(
                    Cursor, Cursor.initiative_run_id == MemoryToolReceiptModel.initiative_run_id
                )
                .where(
                    MemoryToolReceiptModel.initiative_run_id.is_not(None),
                    MemoryToolReceiptModel.id > func.coalesce(Cursor.last_receipt_id, 0),
                )
            )
        ).one()
        return {"pending_receipts": int(row[0]), "pending_runs": int(row[1])}


def _origin_source_query(initiative_id: str) -> Select[Any]:
    initiative, conversation = InitiativeRunModel, CanonicalConversationModel
    presence, space, binding = PresenceModel, CanonicalSpaceModel, SpaceBindingModel
    return (
        select(
            initiative.id,
            initiative.conversation_id,
            initiative.space_id,
            initiative.presence_id,
            initiative.generation,
            initiative.created_at,
            conversation.kind,
            conversation.space_id,
            conversation.person_id,
            conversation.space_binding_id,
            presence.account_id,
            AccountModel.platform,
            AccountModel.external_account_id,
            space.id,
            binding.id,
            binding.external_space_id,
        )
        .select_from(initiative)
        .join(conversation, conversation.id == initiative.conversation_id)
        .join(presence, presence.id == initiative.presence_id)
        .join(AccountModel, AccountModel.id == presence.account_id)
        .join(space, space.id == initiative.space_id)
        .join(
            binding,
            and_(
                binding.space_id == initiative.space_id,
                binding.platform == AccountModel.platform,
                binding.status == "active",
                or_(
                    conversation.space_binding_id.is_(None),
                    binding.id == conversation.space_binding_id,
                ),
            ),
        )
        .where(initiative.id == initiative_id, initiative.state.not_in(("accepted", "running")))
    )


async def claim_initiative(
    database: Database,
    *,
    max_characters: int,
    cycle_id: str | None,
) -> tuple[SelfReflectionBatch, ...]:
    while True:
        now = datetime.now(UTC)
        async with database.read_session() as reader:
            await reader.execute(text("BEGIN"))
            runs = tuple(
                await reader.scalars(
                    select(InitiativeRunModel)
                    .join(
                        MemoryToolReceiptModel,
                        MemoryToolReceiptModel.initiative_run_id == InitiativeRunModel.id,
                    )
                    .outerjoin(Cursor, Cursor.initiative_run_id == InitiativeRunModel.id)
                    .where(
                        InitiativeRunModel.state.not_in(("accepted", "running")),
                        MemoryToolReceiptModel.id > func.coalesce(Cursor.last_receipt_id, 0),
                        or_(
                            MemoryToolReceiptModel.expires_at > now,
                            select(Window.reflection_run_id)
                            .where(
                                Window.initiative_run_id == InitiativeRunModel.id,
                                Window.first_receipt_id <= MemoryToolReceiptModel.id,
                                Window.last_receipt_id >= MemoryToolReceiptModel.id,
                            )
                            .exists(),
                        ),
                    )
                    .group_by(InitiativeRunModel.id)
                    .order_by(func.min(MemoryToolReceiptModel.id))
                )
            )
            run_ids = tuple(run.id for run in runs)
            cursors = (
                dict(
                    (
                        await reader.execute(
                            select(Cursor.initiative_run_id, Cursor.last_receipt_id).where(
                                Cursor.initiative_run_id.in_(run_ids)
                            )
                        )
                    )
                    .tuples()
                    .all()
                )
                if run_ids
                else {}
            )
            windows: dict[str, Any] = {}
            if run_ids:
                for pending in await reader.execute(
                    select(
                        Window.reflection_run_id,
                        Window.initiative_run_id,
                        Window.first_receipt_id,
                        Window.last_receipt_id,
                    )
                    .outerjoin(Cursor, Cursor.initiative_run_id == Window.initiative_run_id)
                    .where(
                        Window.initiative_run_id.in_(run_ids),
                        Window.last_receipt_id > func.coalesce(Cursor.last_receipt_id, 0),
                    )
                    .order_by(Window.first_receipt_id)
                ):
                    windows.setdefault(pending.initiative_run_id, pending)
            retries = (
                {
                    row.id: row
                    for row in await reader.scalars(
                        select(MemorySelfReflectionRunModel).where(
                            MemorySelfReflectionRunModel.id.in_(
                                tuple(window.reflection_run_id for window in windows.values())
                            )
                        )
                    )
                }
                if windows
                else {}
            )
            receipt_columns = tuple(MemoryToolReceiptModel.__table__.columns)
            excerpt_index = tuple(column.key for column in receipt_columns).index("result_excerpt")
            for initiative in runs:
                try:
                    source = await resolve_self_origin(
                        reader, initiative_run_id=initiative.id, require_live=False
                    )
                except MemoryPartitionResolutionError:
                    continue
                origin_query = _origin_source_query(initiative.id)
                origin_rows = tuple(tuple(row) for row in await reader.execute(origin_query))
                cursor_at = cursors.get(initiative.id, 0)
                window_query = (
                    select(
                        Window.reflection_run_id,
                        Window.initiative_run_id,
                        Window.first_receipt_id,
                        Window.last_receipt_id,
                    )
                    .where(
                        Window.initiative_run_id == initiative.id,
                        Window.last_receipt_id > cursor_at,
                    )
                    .order_by(Window.first_receipt_id)
                    .limit(1)
                )
                window = windows.get(initiative.id)
                retry = retries.get(window.reflection_run_id) if window else None
                if retry is not None:
                    next_at = retry.next_attempt_at
                    if next_at and next_at.tzinfo is None:
                        next_at = next_at.replace(tzinfo=UTC)
                    if (
                        retry.status != "failed"
                        or retry.retry_state == "isolated"
                        or (cycle_id is not None and retry.cycle_id == cycle_id)
                        or (next_at is not None and next_at > now)
                    ):
                        continue
                scope = (
                    MemoryToolReceiptModel.initiative_run_id == initiative.id,
                    MemoryToolReceiptModel.canonical_space_id == source.space_id,
                    MemoryToolReceiptModel.canonical_person_id.is_(None),
                    MemoryToolReceiptModel.id > cursor_at,
                )
                query = select(MemoryToolReceiptModel).where(*scope)
                if window:
                    query = query.where(
                        MemoryToolReceiptModel.id >= window.first_receipt_id,
                        MemoryToolReceiptModel.id <= window.last_receipt_id,
                    )
                else:
                    query = query.where(MemoryToolReceiptModel.expires_at > now)
                receipt_query = query.with_only_columns(
                    *(
                        cast(column, String).label(f"value_{index}")
                        for index, column in enumerate(receipt_columns)
                    ),
                    maintain_column_froms=True,
                ).order_by(MemoryToolReceiptModel.id)
                rows = await reader.stream(receipt_query)
                selected: list[tuple[Any, ...]] = []
                characters = 0
                try:
                    async for receipt in rows:
                        size = len(receipt[excerpt_index])
                        if window is None and selected and characters + size > max_characters:
                            break
                        selected.append(tuple(receipt))
                        characters += size
                finally:
                    await rows.close()
                if not selected:
                    continue
                first, last = int(selected[0][0]), int(selected[-1][0])
                partition_hash = hashlib.sha256(source.partition.encode()).hexdigest()
                fingerprint = hashlib.sha256(
                    repr([(int(row[0]), row[excerpt_index]) for row in selected]).encode()
                ).hexdigest()
                slot = (
                    "self:" + hashlib.sha256(f"{initiative.id}:{first}".encode()).hexdigest()[:27]
                )
                isolation = retry is not None and fingerprint != retry.input_fingerprint
                retry_fields = (
                    "id",
                    "status",
                    "retry_state",
                    "cycle_id",
                    "next_attempt_at",
                    "attempt_count",
                    "input_fingerprint",
                )
                retry_source = (
                    tuple(getattr(retry, field) for field in retry_fields) if retry else None
                )
                # Compare the actual original input, not a max-ID/count/hash surrogate.
                receipt_query = receipt_query.where(MemoryToolReceiptModel.id <= last)
                original_json = json.dumps(selected, ensure_ascii=False)
                expected_ids = tuple(row[0] for row in selected)
                state = SelfReflectionState(
                    id=0,
                    conversation_key_hash=partition_hash,
                    bot_user_id=source.bot_user_id,
                    canonical_person_id=None,
                    canonical_space_id=source.space_id,
                    external_person_id=None,
                    external_space_id=source.group_id,
                    last_event_id=0,
                    latest_event_id=0,
                    pending_events=0,
                    pending_characters=characters,
                    pending_since=source.occurred_at,
                )
                break
            else:
                return ()
        async with database.immediate_session() as writer:
            claimed_at = datetime.now(UTC)
            if tuple(tuple(row) for row in await writer.execute(origin_query)) != origin_rows:
                continue
            current_cursor = await writer.get(Cursor, initiative.id)
            if (current_cursor.last_receipt_id if current_cursor else 0) != cursor_at:
                continue
            current_window = (await writer.execute(window_query)).one_or_none()
            if current_window != window:
                continue
            if retry is not None:
                actual_retry = (
                    await writer.execute(
                        select(
                            *(
                                getattr(MemorySelfReflectionRunModel, field)
                                for field in retry_fields
                            )
                        ).where(MemorySelfReflectionRunModel.id == retry.id)
                    )
                ).one_or_none()
                if actual_retry != retry_source:
                    continue
                if retry.next_attempt_at is not None:
                    next_at = (
                        retry.next_attempt_at.replace(tzinfo=UTC)
                        if (retry.next_attempt_at.tzinfo is None)
                        else retry.next_attempt_at
                    )
                    if next_at > claimed_at:
                        continue
            current_query = receipt_query
            if window is None:
                current_query = current_query.where(MemoryToolReceiptModel.expires_at > claimed_at)
            actual = current_query.subquery()
            wanted = func.json_each(original_json).table_valued("value")
            columns = tuple(actual.c)
            checked = tuple(
                await writer.execute(
                    select(
                        columns[0],
                        and_(
                            *(
                                column.is_(func.json_extract(wanted.c.value, f"$[{index}]"))
                                for index, column in enumerate(columns)
                            )
                        ),
                    )
                    .select_from(
                        actual.outerjoin(
                            wanted, columns[0] == func.json_extract(wanted.c.value, "$[0]")
                        )
                    )
                    .order_by(columns[0])
                )
            )
            # IDs are compared numerically in source order (10 follows 9).
            if tuple(sorted((row[0] for row in checked), key=int)) != expected_ids or not all(
                row[1] for row in checked
            ):
                continue
            if retry is not None and isolation:
                await writer.execute(
                    update(MemorySelfReflectionRunModel)
                    .where(
                        MemorySelfReflectionRunModel.id == retry.id,
                        MemorySelfReflectionRunModel.status == "failed",
                    )
                    .values(retry_state="isolated", error_category="source_changed")
                )
                continue
            run_id: int | None
            if retry is not None:
                changed = await writer.scalar(
                    update(MemorySelfReflectionRunModel)
                    .where(
                        MemorySelfReflectionRunModel.id == retry.id,
                        MemorySelfReflectionRunModel.status == "failed",
                    )
                    .values(
                        status="processing",
                        started_at=claimed_at,
                        completed_at=None,
                        cycle_id=cycle_id,
                        attempt_count=retry.attempt_count + 1,
                    )
                    .returning(MemorySelfReflectionRunModel.id)
                )
                if changed is None:
                    continue
                run_id = retry.id
            else:
                run_id = await writer.scalar(
                    insert(MemorySelfReflectionRunModel)
                    .values(
                        conversation_key_hash=partition_hash,
                        bot_user_id=source.bot_user_id,
                        canonical_person_id=None,
                        canonical_space_id=source.space_id,
                        scheduled_slot=slot,
                        trigger_reason="self_tool_receipts",
                        first_event_id=0,
                        last_event_id=0,
                        status="processing",
                        proposal_count=0,
                        committed_count=0,
                        started_at=claimed_at,
                        cycle_id=cycle_id,
                        attempt_count=1,
                        input_fingerprint=fingerprint,
                        processed_events=0,
                        processed_characters=characters,
                    )
                    .on_conflict_do_nothing()
                    .returning(MemorySelfReflectionRunModel.id)
                )
                if run_id is None:
                    continue
                writer.add(
                    Window(
                        reflection_run_id=run_id,
                        initiative_run_id=initiative.id,
                        first_receipt_id=first,
                        last_receipt_id=last,
                    )
                )
            batch = SelfReflectionBatch(
                state=state,
                events=(),
                context_events=(),
                trigger_reason="self_tool_receipts",
                scheduled_slot=slot,
                run_id=run_id,
                max_input_characters=max_characters,
                initiative_run_id=initiative.id,
                first_receipt_id=first,
                last_receipt_id=last,
                occurred_at=source.occurred_at,
            )
        return (batch,)
