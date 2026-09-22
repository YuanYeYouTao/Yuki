"""Receipt-only windows reuse the ordinary reflection run, budget and checkpoints."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.autonomy_db_models import InitiativeRunModel
from qq_ai_bot.memory.partition import MemoryPartitionResolutionError
from qq_ai_bot.memory.self_origin import resolve_self_origin
from qq_ai_bot.memory.self_reflection.db_models import (
    InitiativeReflectionCursorModel as Cursor,
)
from qq_ai_bot.memory.self_reflection.db_models import (
    InitiativeReflectionWindowModel as Window,
)
from qq_ai_bot.memory.self_reflection.models import SelfReflectionBatch, SelfReflectionState
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemorySelfReflectionRunModel, MemoryToolReceiptModel


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
    async with database.sessions() as session:
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


async def claim_initiative(
    database: Database,
    *,
    max_characters: int,
    excluded_conversation_keys: frozenset[str],
    cycle_id: str | None,
) -> tuple[SelfReflectionBatch, ...]:
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        session.autoflush = False
        # The cursor is per run: a late receipt for an older run cannot be skipped
        # merely because a newer run was already reflected.
        runs = list(
            await session.scalars(
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
                .limit(100)
            )
        )
        for initiative in runs:
            try:
                source = await resolve_self_origin(
                    session, initiative_run_id=initiative.id, require_live=False
                )
            except MemoryPartitionResolutionError:
                continue
            partition_hash = hashlib.sha256(source.partition.encode()).hexdigest()
            if partition_hash in excluded_conversation_keys:
                continue
            cursor = await session.get(Cursor, initiative.id)
            last = cursor.last_receipt_id if cursor else 0
            window = await session.scalar(
                select(Window)
                .where(
                    Window.initiative_run_id == initiative.id,
                    Window.last_receipt_id > last,
                )
                .order_by(Window.first_receipt_id)
                .limit(1)
            )
            retry = (
                await session.get(MemorySelfReflectionRunModel, window.reflection_run_id)
                if window
                else None
            )
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
            query = select(MemoryToolReceiptModel).where(
                MemoryToolReceiptModel.initiative_run_id == initiative.id,
                MemoryToolReceiptModel.canonical_space_id == source.space_id,
                MemoryToolReceiptModel.canonical_person_id.is_(None),
                MemoryToolReceiptModel.id > last,
            )
            if window:
                query = query.where(
                    MemoryToolReceiptModel.id >= window.first_receipt_id,
                    MemoryToolReceiptModel.id <= window.last_receipt_id,
                )
            else:
                query = query.where(MemoryToolReceiptModel.expires_at > now)
            rows = list(await session.scalars(query.order_by(MemoryToolReceiptModel.id).limit(8)))
            selected: list[MemoryToolReceiptModel] = []
            characters = 0
            for receipt in rows:
                size = min(2000, len(receipt.result_excerpt))
                if selected and characters + size > max_characters:
                    break
                selected.append(receipt)
                characters += size
            if not selected:
                continue
            fingerprint = hashlib.sha256(
                repr([(r.id, r.result_excerpt) for r in selected]).encode()
            ).hexdigest()
            if retry and fingerprint != retry.input_fingerprint:
                retry.retry_state = "isolated"
                retry.error_category = "source_changed"
                continue
            first, last = selected[0].id, selected[-1].id
            slot = "self:" + hashlib.sha256(f"{initiative.id}:{first}".encode()).hexdigest()[:27]
            if retry:
                changed = await session.scalar(
                    # Compare status so concurrent workers cannot claim one window twice.
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
                        attempt_count=retry.attempt_count + 1,
                    )
                    .returning(MemorySelfReflectionRunModel.id)
                )
                if changed is None:
                    continue
                run_id = retry.id
            else:
                # No event is manufactured. Zero event cursors mean this run's
                # authoritative input range is the separate receipt window.
                new_run_id = await session.scalar(
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
                        started_at=now,
                        cycle_id=cycle_id,
                        attempt_count=1,
                        input_fingerprint=fingerprint,
                        processed_events=0,
                        processed_characters=characters,
                    )
                    .on_conflict_do_nothing()
                    .returning(MemorySelfReflectionRunModel.id)
                )
                if new_run_id is None:
                    continue
                run_id = new_run_id
                session.add(
                    Window(
                        reflection_run_id=run_id,
                        initiative_run_id=initiative.id,
                        first_receipt_id=first,
                        last_receipt_id=last,
                    )
                )
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
                has_yuki_reply=False,
                has_tool_result=True,
                high_value_signal=False,
            )
            return (
                SelfReflectionBatch(
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
                ),
            )
    return ()
