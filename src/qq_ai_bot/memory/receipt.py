"""Content-free recall receipts for adaptive memory retrieval."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text, update

from qq_ai_bot.memory.models import MemoryQueryIntent, MemoryRetrievalHit, MemoryRetrievalResult
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryRecallItemModel, MemoryRecallReceiptModel
from qq_ai_bot.runtime.observability import claim_runtime_turn_id


def hashed_identifier(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


@dataclass(frozen=True, slots=True)
class MemoryRecallTurn:
    turn_id: str
    injected_fact_ids: tuple[int, ...]


class MemoryRecallRepository:
    """Persist bounded recall stages without query, message, or memory text."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def record_initial(
        self,
        *,
        conversation_key: str,
        trigger_message_id: str,
        origin: str,
        intent: MemoryQueryIntent,
        result: MemoryRetrievalResult,
        injected_fact_ids: tuple[int, ...],
        retention_days: int,
        consumer: str = "automatic_context",
    ) -> MemoryRecallTurn:
        turn_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        selected_ids = {hit.fact.id for hit in result.hits}
        injected_ids = set(injected_fact_ids)
        by_fact: dict[int, MemoryRetrievalHit] = {}
        for hit in result.trace_hits:
            by_fact.setdefault(hit.fact.id, hit)
        async with self._database.sessions() as session, session.begin():
            receipt = MemoryRecallReceiptModel(
                turn_id=turn_id,
                runtime_turn_id=claim_runtime_turn_id(),
                conversation_hash=hashed_identifier(conversation_key),
                trigger_hash=hashed_identifier(trigger_message_id),
                origin=origin[:32],
                mode=intent.mode.value,
                purpose=intent.purpose.value,
                candidate_count=result.candidate_count,
                selected_count=len(selected_ids),
                injected_count=len(injected_ids),
                used_count=0,
                reinforced_count=0,
                attribution_status="skipped",
                consumer=consumer,
                attribution_reason="not_scheduled" if injected_ids else "no_memory",
                attribution_completed_at=now,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(days=retention_days),
            )
            session.add(receipt)
            await session.flush()
            for hit in by_fact.values():
                session.add(
                    MemoryRecallItemModel(
                        receipt_id=receipt.id,
                        fact_id=hit.fact.id,
                        target_role=hit.target.role.value,
                        candidate=True,
                        selected=hit.fact.id in selected_ids,
                        injected=hit.fact.id in injected_ids,
                        used=False,
                        reinforced=False,
                        base_rank_score=hit.base_rank_score,
                        subject_score=hit.subject_score,
                        entity_score=hit.entity_score,
                        temporal_score=hit.temporal_score,
                        kind_score=hit.kind_score,
                        activation_score=hit.activation_score,
                        rerank_score=hit.rerank_score,
                        selection_reason=hit.selection_reason[:64],
                        injected_at=now if hit.fact.id in injected_ids else None,
                        used_at=None,
                        reinforced_at=None,
                    )
                )
        return MemoryRecallTurn(turn_id=turn_id, injected_fact_ids=injected_fact_ids)

    async def mark_attributed_used(
        self,
        turn_id: str,
        fact_ids: tuple[int, ...],
        *,
        evaluated_fact_ids: tuple[int, ...] | None = None,
    ) -> tuple[int, ...] | None:
        unique_ids = tuple(dict.fromkeys(fact_ids))
        if not turn_id or (not unique_ids and evaluated_fact_ids is None):
            return ()
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            receipt_id = await session.scalar(
                select(MemoryRecallReceiptModel.id).where(
                    MemoryRecallReceiptModel.turn_id == turn_id
                )
            )
            if receipt_id is None:
                return None
            allowed = tuple(
                await session.scalars(
                    select(MemoryRecallItemModel.fact_id).where(
                        MemoryRecallItemModel.receipt_id == receipt_id,
                        MemoryRecallItemModel.fact_id.in_(unique_ids),
                        MemoryRecallItemModel.injected.is_(True),
                    )
                )
            )
            await session.execute(
                update(MemoryRecallItemModel)
                .where(
                    MemoryRecallItemModel.receipt_id == receipt_id,
                    MemoryRecallItemModel.fact_id.in_(allowed),
                )
                .values(used=True, used_at=now)
            )
            if evaluated_fact_ids is not None:
                await session.execute(
                    update(MemoryRecallItemModel)
                    .where(
                        MemoryRecallItemModel.receipt_id == receipt_id,
                        MemoryRecallItemModel.fact_id.in_(evaluated_fact_ids),
                        MemoryRecallItemModel.injected.is_(True),
                    )
                    .values(attribution_evaluated=True)
                )
                await session.execute(
                    update(MemoryRecallReceiptModel)
                    .where(MemoryRecallReceiptModel.id == receipt_id)
                    .values(
                        attribution_status="succeeded",
                        attribution_reason="used" if allowed else "no_used",
                        attribution_completed_at=now,
                    )
                )
            used_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryRecallItemModel)
                    .where(
                        MemoryRecallItemModel.receipt_id == receipt_id,
                        MemoryRecallItemModel.used.is_(True),
                    )
                )
                or 0
            )
            await session.execute(
                update(MemoryRecallReceiptModel)
                .where(MemoryRecallReceiptModel.id == receipt_id)
                .values(used_count=used_count, updated_at=now)
            )
        return allowed

    async def set_attribution_outcome(self, turn_id: str, status: str, reason: str) -> None:
        """Only closed, content-free categories are persisted; never exception strings."""
        if status not in {"pending", "failed", "skipped"} or reason not in {
            "queued",
            "queue_full",
            "expired",
            "disabled",
            "invalid",
            "preempted",
            "timeout",
            "model_error",
            "interrupted",
            "delivery_failed",
            "not_scheduled",
        }:
            raise ValueError("invalid attribution outcome")
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryRecallReceiptModel)
                .where(
                    MemoryRecallReceiptModel.turn_id == turn_id,
                    MemoryRecallReceiptModel.attribution_status != "succeeded",
                )
                .values(
                    attribution_status=status,
                    attribution_reason=reason,
                    attribution_completed_at=None if status == "pending" else now,
                    updated_at=now,
                )
            )

    async def recover_pending_attribution(self) -> None:
        """The queue is process-local: pending receipts from a prior process cannot resume."""
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryRecallReceiptModel)
                .where(MemoryRecallReceiptModel.attribution_status == "pending")
                .values(
                    attribution_status="failed",
                    attribution_reason="interrupted",
                    attribution_completed_at=now,
                    updated_at=now,
                )
            )

    async def record_tool_read_outcome(self, turn_id: str, outcome: str) -> None:
        allowed = {
            "success",
            "empty",
            "ambiguous",
            "permission_denied",
            "duplicate",
            "infrastructure_failure",
        }
        if outcome not in allowed:
            raise ValueError("invalid tool read outcome")
        if not turn_id:
            return
        column = getattr(MemoryRecallReceiptModel, f"tool_read_{outcome}_count")
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryRecallReceiptModel)
                .where(MemoryRecallReceiptModel.turn_id == turn_id)
                .values(
                    {column: column + 1, MemoryRecallReceiptModel.updated_at: datetime.now(UTC)}
                )
            )

    async def summarize(self, *, since: datetime) -> dict[str, object]:
        """Aggregate diagnostic denominators without returning identities or content."""
        parameters = {"since": since.replace(tzinfo=None)}
        async with self._database.sessions() as session:
            counts = (
                (
                    await session.execute(
                        text(
                            "SELECT count(*) AS recall_turns, "
                            "coalesce(sum(consumer='automatic_context'),0) AS automatic_turns, "
                            "coalesce(sum(consumer='automatic_context' AND injected_count=0),0) "
                            "AS zero_turns, "
                            "coalesce(sum(candidate_count),0) AS candidates, "
                            "coalesce(sum(injected_count),0) AS injected "
                            "FROM memory_recall_receipts WHERE created_at >= :since"
                        ),
                        parameters,
                    )
                )
                .mappings()
                .one()
            )
            assessed = (
                (
                    await session.execute(
                        text(
                            "SELECT coalesce(sum(i.attribution_evaluated AND i.injected),0) "
                            "AS evaluated, coalesce(sum(i.attribution_evaluated AND i.injected "
                            "AND i.used),0) AS used FROM memory_recall_items i "
                            "JOIN memory_recall_receipts r ON r.id=i.receipt_id "
                            "WHERE r.created_at >= :since"
                        ),
                        parameters,
                    )
                )
                .mappings()
                .one()
            )
            outcomes = (
                (
                    await session.execute(
                        text(
                            "SELECT attribution_status, attribution_reason, count(*) AS count "
                            "FROM memory_recall_receipts WHERE created_at >= :since "
                            "GROUP BY attribution_status, attribution_reason"
                        ),
                        parameters,
                    )
                )
                .mappings()
                .all()
            )
            repetitions = (
                await session.execute(
                    text(
                        "SELECT exposures, count(*) AS pairs FROM ("
                        "SELECT count(*) AS exposures FROM memory_recall_items i "
                        "JOIN memory_recall_receipts r ON r.id=i.receipt_id "
                        "WHERE r.created_at >= :since AND i.injected=1 "
                        "GROUP BY r.conversation_hash, i.fact_id) GROUP BY exposures"
                    ),
                    parameters,
                )
            ).all()
            tool_reads = (
                await session.execute(
                    text(
                        "SELECT coalesce(sum(tool_read_success_count),0), "
                        "coalesce(sum(tool_read_empty_count),0), "
                        "coalesce(sum(tool_read_ambiguous_count),0), "
                        "coalesce(sum(tool_read_permission_denied_count),0), "
                        "coalesce(sum(tool_read_duplicate_count),0), "
                        "coalesce(sum(tool_read_infrastructure_failure_count),0) "
                        "FROM memory_recall_receipts WHERE created_at >= :since"
                    ),
                    parameters,
                )
            ).one()
        automatic, evaluated = int(counts["automatic_turns"]), int(assessed["evaluated"])
        injected = int(counts["injected"])
        return {
            **dict(counts),
            **dict(assessed),
            "zero_injection_rate": int(counts["zero_turns"]) / automatic if automatic else None,
            "evaluation_coverage": evaluated / injected if injected else None,
            "evaluated_use_rate": int(assessed["used"]) / evaluated if evaluated else None,
            "attribution_outcomes": [dict(row) for row in outcomes],
            "repeated_exposure_histogram": {str(count): pairs for count, pairs in repetitions},
            "tool_reads": dict(
                zip(
                    (
                        "success",
                        "empty",
                        "ambiguous",
                        "permission_denied",
                        "duplicate",
                        "infrastructure_failure",
                    ),
                    (int(value) for value in tool_reads),
                    strict=True,
                )
            ),
        }

    async def record_tool_injected(
        self,
        turn_id: str,
        fact_ids: tuple[int, ...],
    ) -> None:
        unique_ids = tuple(dict.fromkeys(fact_ids))
        if not turn_id or not unique_ids:
            return
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            receipt_id = await session.scalar(
                select(MemoryRecallReceiptModel.id).where(
                    MemoryRecallReceiptModel.turn_id == turn_id
                )
            )
            if receipt_id is None:
                return
            existing = {
                row.fact_id: row
                for row in (
                    await session.scalars(
                        select(MemoryRecallItemModel).where(
                            MemoryRecallItemModel.receipt_id == receipt_id,
                            MemoryRecallItemModel.fact_id.in_(unique_ids),
                        )
                    )
                ).all()
            }
            for fact_id in unique_ids:
                row = existing.get(fact_id)
                if row is not None:
                    row.selected = True
                    row.injected = True
                    row.injected_at = now
                    continue
                session.add(
                    MemoryRecallItemModel(
                        receipt_id=receipt_id,
                        fact_id=fact_id,
                        target_role="agent_tool",
                        candidate=True,
                        selected=True,
                        injected=True,
                        used=False,
                        reinforced=False,
                        base_rank_score=0,
                        subject_score=0.5,
                        entity_score=0.5,
                        temporal_score=0.5,
                        kind_score=0.5,
                        activation_score=0.5,
                        rerank_score=0,
                        selection_reason="agent_tool_result",
                        injected_at=now,
                    )
                )
            await session.flush()
            injected_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryRecallItemModel)
                    .where(
                        MemoryRecallItemModel.receipt_id == receipt_id,
                        MemoryRecallItemModel.injected.is_(True),
                    )
                )
                or 0
            )
            await session.execute(
                update(MemoryRecallReceiptModel)
                .where(MemoryRecallReceiptModel.id == receipt_id)
                .values(injected_count=injected_count, updated_at=now)
            )

    async def pending_reinforcement(
        self,
        turn_id: str,
        fact_ids: tuple[int, ...],
    ) -> tuple[int, ...] | None:
        if not turn_id or not fact_ids:
            return ()
        async with self._database.sessions() as session:
            receipt_id = await session.scalar(
                select(MemoryRecallReceiptModel.id).where(
                    MemoryRecallReceiptModel.turn_id == turn_id
                )
            )
            if receipt_id is None:
                return None
            rows = await session.scalars(
                select(MemoryRecallItemModel.fact_id).where(
                    MemoryRecallItemModel.receipt_id == receipt_id,
                    MemoryRecallItemModel.fact_id.in_(tuple(dict.fromkeys(fact_ids))),
                    MemoryRecallItemModel.used.is_(True),
                    MemoryRecallItemModel.reinforced.is_(False),
                )
            )
            return tuple(rows)

    async def mark_reinforced(self, turn_id: str, fact_ids: tuple[int, ...]) -> int:
        if not turn_id or not fact_ids:
            return 0
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            receipt_id = await session.scalar(
                select(MemoryRecallReceiptModel.id).where(
                    MemoryRecallReceiptModel.turn_id == turn_id
                )
            )
            if receipt_id is None:
                return 0
            await session.execute(
                update(MemoryRecallItemModel)
                .where(
                    MemoryRecallItemModel.receipt_id == receipt_id,
                    MemoryRecallItemModel.fact_id.in_(tuple(dict.fromkeys(fact_ids))),
                    MemoryRecallItemModel.used.is_(True),
                    MemoryRecallItemModel.reinforced.is_(False),
                )
                .values(reinforced=True, reinforced_at=now)
            )
            reinforced_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryRecallItemModel)
                    .where(
                        MemoryRecallItemModel.receipt_id == receipt_id,
                        MemoryRecallItemModel.reinforced.is_(True),
                    )
                )
                or 0
            )
            await session.execute(
                update(MemoryRecallReceiptModel)
                .where(MemoryRecallReceiptModel.id == receipt_id)
                .values(reinforced_count=reinforced_count, updated_at=now)
            )
        return reinforced_count

    async def cleanup_expired(self, *, now: datetime, limit: int) -> int:
        async with self._database.sessions() as session, session.begin():
            ids = tuple(
                await session.scalars(
                    select(MemoryRecallReceiptModel.id)
                    .where(MemoryRecallReceiptModel.expires_at < now)
                    .order_by(MemoryRecallReceiptModel.id)
                    .limit(limit)
                )
            )
            if not ids:
                return 0
            await session.execute(
                delete(MemoryRecallReceiptModel).where(MemoryRecallReceiptModel.id.in_(ids))
            )
            return len(ids)

    async def recent_for_fact(
        self,
        fact_id: int,
        *,
        limit: int = 5,
    ) -> tuple[dict[str, object], ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(MemoryRecallReceiptModel, MemoryRecallItemModel)
                    .join(
                        MemoryRecallItemModel,
                        MemoryRecallItemModel.receipt_id == MemoryRecallReceiptModel.id,
                    )
                    .where(MemoryRecallItemModel.fact_id == fact_id)
                    .order_by(MemoryRecallReceiptModel.created_at.desc())
                    .limit(max(1, limit))
                )
            ).all()
        return tuple(
            {
                "turn_id": receipt.turn_id,
                "mode": receipt.mode,
                "purpose": receipt.purpose,
                "selected": item.selected,
                "injected": item.injected,
                "used": item.used,
                "attribution_evaluated": item.attribution_evaluated,
                "attribution_status": receipt.attribution_status,
                "attribution_reason": receipt.attribution_reason,
                "reinforced": item.reinforced,
                "rerank_score": item.rerank_score,
                "created_at": receipt.created_at.isoformat(),
            }
            for receipt, item in rows
        )
