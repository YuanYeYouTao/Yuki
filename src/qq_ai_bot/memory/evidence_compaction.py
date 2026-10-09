"""Restart-safe evidence compaction with conservative provenance checks."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.config import Settings
from qq_ai_bot.memory.dream.db_models import (
    MemoryDreamFactCheckpointModel,
    MemoryDreamOperationModel,
    MemoryDreamOperationResultModel,
    MemoryDreamOperationSourceModel,
    MemoryEvidenceCompactionItemModel,
    MemoryEvidenceCompactionRunModel,
)
from qq_ai_bot.memory.dream.repository import fact_signature
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryFactRelationModel,
    MemoryMutationReceiptModel,
    MemorySelfReflectionResultModel,
    MemorySelfReflectionRunModel,
)

logger = logging.getLogger(__name__)
_BATCH_TIMEOUT_SECONDS = 120.0


class EvidenceCompactionService:
    """Compact only evidence whose complete source lineage is independently recoverable."""

    def __init__(self, *, settings: Settings, database: Database, facts: MemoryFactService) -> None:
        self._settings = settings
        self._database = database
        self._facts = facts

    async def run_batch(self) -> int:
        candidates = await self._candidate_facts(
            limit=self._settings.memory_evidence_compaction_batch_size
        )
        # Idle polling needs no durable run or writer. A previously started
        # run still closes under its original identity after recovery.
        run_id = await self._ensure_run(create=bool(candidates))
        if run_id is None:
            return 0
        if not candidates:
            await self._finish_run(run_id)
            return 0
        processed = 0
        for fact_id, provenance, operation_id, evidence_count in candidates:
            item_id = await self._claim_item(
                run_id=run_id,
                fact_id=fact_id,
                provenance=provenance,
                operation_id=operation_id,
                evidence_before=evidence_count,
            )
            if item_id is None:
                continue
            try:
                await self._compact_fact(
                    fact_id=fact_id,
                    provenance=provenance,
                    operation_id=operation_id,
                    item_id=item_id,
                    before=evidence_count,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                async with self._database.sessions() as session, session.begin():
                    await self._finish_item(
                        item_id,
                        status="failed",
                        before=evidence_count,
                        after=evidence_count,
                        error_category=type(exc).__name__,
                        session=session,
                    )
                logger.warning(
                    "memory_evidence_compaction_error fact_id=%d error_category=%s",
                    fact_id,
                    type(exc).__name__,
                )
            processed += 1
        await self._refresh_run(run_id)
        return processed

    async def _ensure_run(self, *, create: bool = True) -> int | None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            current = await session.scalar(
                select(MemoryEvidenceCompactionRunModel)
                .where(MemoryEvidenceCompactionRunModel.status == "running")
                .order_by(MemoryEvidenceCompactionRunModel.id)
                .limit(1)
            )
            if current is not None:
                processing = await session.scalar(
                    select(MemoryEvidenceCompactionItemModel.id)
                    .where(
                        MemoryEvidenceCompactionItemModel.run_id == current.id,
                        MemoryEvidenceCompactionItemModel.status == "processing",
                    )
                    .limit(1)
                )
                if processing is not None:
                    await session.execute(
                        update(MemoryEvidenceCompactionItemModel)
                        .where(
                            MemoryEvidenceCompactionItemModel.run_id == current.id,
                            MemoryEvidenceCompactionItemModel.status == "processing",
                        )
                        .values(status="pending", updated_at=now)
                    )
                return current.id
            if not create:
                return None
            row = MemoryEvidenceCompactionRunModel(
                public_id=str(uuid.uuid4()),
                status="running",
                scan_after_fact_id=0,
                scanned_facts=0,
                completed_items=0,
                skipped_items=0,
                failed_items=0,
                evidence_before=0,
                evidence_after=0,
                error_category=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
            session.add(row)
            await session.flush()
            return row.id

    async def _candidate_facts(self, *, limit: int) -> tuple[tuple[int, str, int | None, int], ...]:
        async with self._database.sessions() as session:
            counts = (
                select(
                    MemoryEvidenceModel.fact_id.label("fact_id"),
                    func.count(MemoryEvidenceModel.id).label("evidence_count"),
                )
                .group_by(MemoryEvidenceModel.fact_id)
                .subquery()
            )
            reflection_facts = (
                select(MemorySelfReflectionResultModel.fact_id.label("fact_id"))
                .where(MemorySelfReflectionResultModel.result_kind == "episode")
                .distinct()
                .subquery()
            )
            dream_results = (
                select(
                    MemoryDreamOperationResultModel.fact_id.label("fact_id"),
                    func.max(MemoryDreamOperationResultModel.operation_id).label("operation_id"),
                )
                .join(
                    MemoryDreamOperationModel,
                    MemoryDreamOperationModel.id == MemoryDreamOperationResultModel.operation_id,
                )
                .where(
                    MemoryDreamOperationModel.operation_type.in_(
                        ("merge", "synthesize", "recompose")
                    ),
                    MemoryDreamOperationModel.status == "committed",
                )
                .group_by(MemoryDreamOperationResultModel.fact_id)
                .subquery()
            )
            rows = (
                await session.execute(
                    select(
                        counts.c.fact_id,
                        counts.c.evidence_count,
                        reflection_facts.c.fact_id.label("reflection_fact_id"),
                        dream_results.c.operation_id,
                    )
                    .outerjoin(
                        reflection_facts,
                        reflection_facts.c.fact_id == counts.c.fact_id,
                    )
                    .outerjoin(
                        dream_results,
                        dream_results.c.fact_id == counts.c.fact_id,
                    )
                    .where(
                        (
                            (reflection_facts.c.fact_id.is_not(None))
                            & (dream_results.c.operation_id.is_(None))
                            & (counts.c.evidence_count > 8)
                        )
                        | (
                            (dream_results.c.operation_id.is_not(None))
                            & (counts.c.evidence_count > 12)
                        )
                    )
                    .where(
                        ~select(MemoryEvidenceCompactionItemModel.id)
                        .where(
                            MemoryEvidenceCompactionItemModel.fact_id == counts.c.fact_id,
                            MemoryEvidenceCompactionItemModel.evidence_before
                            == counts.c.evidence_count,
                            MemoryEvidenceCompactionItemModel.status.in_(
                                ("completed", "skipped", "failed")
                            ),
                        )
                        .exists()
                    )
                    .order_by(counts.c.fact_id)
                    .limit(max(1, limit))
                )
            ).all()
            result: list[tuple[int, str, int | None, int]] = []
            for row in rows:
                fact_id = int(row.fact_id)
                provenance = "dream" if row.operation_id is not None else "self_reflection"
                operation_id = int(row.operation_id) if row.operation_id is not None else None
                result.append((fact_id, provenance, operation_id, int(row.evidence_count)))
                if len(result) >= limit:
                    break
        return tuple(result)

    async def _claim_item(
        self,
        *,
        run_id: int,
        fact_id: int,
        provenance: str,
        operation_id: int | None,
        evidence_before: int,
    ) -> int | None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            existing = await session.scalar(
                select(MemoryEvidenceCompactionItemModel).where(
                    MemoryEvidenceCompactionItemModel.run_id == run_id,
                    MemoryEvidenceCompactionItemModel.fact_id == fact_id,
                    MemoryEvidenceCompactionItemModel.status.in_(("pending", "processing")),
                )
            )
            if existing is not None:
                existing.updated_at = now
                existing.status = "processing"
                return existing.id
            item_id = await session.scalar(
                insert(MemoryEvidenceCompactionItemModel)
                .values(
                    run_id=run_id,
                    fact_id=fact_id,
                    provenance_type=provenance,
                    dream_operation_id=operation_id,
                    status="processing",
                    evidence_before=evidence_before,
                    evidence_after=evidence_before,
                    deleted_count=0,
                    error_category=None,
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        MemoryEvidenceCompactionItemModel.run_id,
                        MemoryEvidenceCompactionItemModel.fact_id,
                    ]
                )
                .returning(MemoryEvidenceCompactionItemModel.id)
            )
            return int(item_id) if item_id is not None else None

    async def _compact_fact(
        self, *, fact_id: int, provenance: str, operation_id: int | None, item_id: int, before: int
    ) -> int:
        async def compact(session: AsyncSession) -> int:
            async def finish(after: int) -> int:
                status = "completed" if after < before else "skipped"
                await self._finish_item(
                    item_id,
                    status=status,
                    before=before,
                    after=after,
                    error_category=None if status == "completed" else "no_safe_reduction",
                    session=session,
                )
                return after

            fact = await self._facts.repository.get_fact(fact_id, session=session)
            if fact is None:
                raise ValueError("compaction fact disappeared")
            evidence = tuple(
                (
                    await session.scalars(
                        select(MemoryEvidenceModel)
                        .where(MemoryEvidenceModel.fact_id == fact_id)
                        .order_by(MemoryEvidenceModel.created_at, MemoryEvidenceModel.id)
                    )
                ).all()
            )
            if provenance == "self_reflection":
                keep_ids = await self._self_reflection_keep_ids(
                    fact_id=fact_id, evidence=evidence, session=session
                )
            else:
                if operation_id is None:
                    return await finish(len(evidence))
                keep_ids = await self._dream_keep_ids(
                    fact_id=fact_id,
                    operation_id=operation_id,
                    evidence=evidence,
                    current_signature=fact_signature(fact),
                    session=session,
                )
            delete_ids = tuple(row.id for row in evidence if row.id not in keep_ids)
            if not delete_ids:
                return await finish(len(evidence))
            readable = await self._facts.repository.list_evidence(
                fact_id, limit=None, session=session
            )
            remaining = tuple(row for row in readable if row.id in keep_ids)
            prepared = await self._facts.prepare_evidence_metadata(fact, remaining)
            rebase = None
            if provenance == "dream" and operation_id is not None:
                rebase = await self._prepare_dream_rebase(
                    fact_id=fact_id,
                    operation_id=operation_id,
                    deleted_ids=delete_ids,
                    session=session,
                )
            updated_at = datetime.now(UTC)
            await session.execute(
                delete(MemoryEvidenceModel).where(MemoryEvidenceModel.id.in_(delete_ids))
            )
            await self._facts.refresh_evidence_metadata(
                fact_id,
                confirmed_at=fact.last_confirmed_at,
                session=session,
                prepared=prepared,
                updated_at=updated_at,
            )
            if rebase is not None:
                refreshed = fact.model_copy(
                    update={
                        "authority": prepared[0],
                        "confidence": prepared[1],
                        "evidence_count": len(remaining),
                        "updated_at": updated_at,
                    }
                )
                self._apply_dream_rebase(
                    rebase=rebase,
                    signature=fact_signature(refreshed),
                )
            return await finish(len(evidence) - len(delete_ids))

        return await self._facts.repository.apply_evidence_write(compact)

    async def _self_reflection_keep_ids(
        self, *, fact_id: int, evidence: tuple[Any, ...], session: Any
    ) -> set[int]:
        mapping = await session.scalar(
            select(MemorySelfReflectionResultModel).where(
                MemorySelfReflectionResultModel.fact_id == fact_id,
                MemorySelfReflectionResultModel.result_kind == "episode",
            )
        )
        if mapping is None:
            return {row.id for row in evidence}
        run = await session.get(MemorySelfReflectionRunModel, mapping.run_id)
        if run is None:
            return {row.id for row in evidence}
        receipts = tuple(
            (
                await session.scalars(
                    select(MemoryMutationReceiptModel)
                    .where(
                        MemoryMutationReceiptModel.new_fact_id == fact_id,
                        MemoryMutationReceiptModel.decision_actor_type == "reflection",
                    )
                    .order_by(MemoryMutationReceiptModel.id)
                )
            ).all()
        )
        if not receipts:
            return {row.id for row in evidence}
        by_event = {row.event_id: row for row in evidence if row.event_id is not None}
        selected: list[Any] = []
        selected.extend(
            by_event[receipt.trigger_event_id]
            for receipt in receipts
            if receipt.trigger_event_id in by_event
        )
        event_rows = tuple(row for row in evidence if row.event_id is not None)
        nonempty_ids = set(
            await session.scalars(
                select(ChatEventModel.id).where(
                    ChatEventModel.id.in_(tuple(row.event_id for row in event_rows)),
                    (func.length(func.trim(ChatEventModel.content)) > 0)
                    | ChatEventModel.audio_transcript.contains('"source":"current"'),
                )
            )
        )
        nonempty = [row for row in event_rows if row.event_id in nonempty_ids]
        if nonempty:
            selected.extend((nonempty[0], nonempty[-1]))
        selected.extend(row for row in evidence if row.tool_receipt_id is not None)
        unique: list[Any] = []
        seen: set[int] = set()
        for row in selected:
            if row is None or row.id in seen:
                continue
            seen.add(row.id)
            unique.append(row)
            if len(unique) >= 8:
                break
        return {row.id for row in unique}

    async def _dream_keep_ids(
        self,
        *,
        fact_id: int,
        operation_id: int,
        evidence: tuple[Any, ...],
        current_signature: str,
        session: Any,
    ) -> set[int]:
        operation = await session.get(MemoryDreamOperationModel, operation_id)
        result = await session.scalar(
            select(MemoryDreamOperationResultModel).where(
                MemoryDreamOperationResultModel.operation_id == operation_id,
                MemoryDreamOperationResultModel.fact_id == fact_id,
            )
        )
        if (
            operation is None
            or result is None
            or operation.status != "committed"
            or operation.operation_type not in {"merge", "synthesize", "recompose"}
            or result.result_signature != current_signature
        ):
            return {row.id for row in evidence}
        dependency = int(
            await session.scalar(
                select(func.count())
                .select_from(MemoryDreamOperationSourceModel)
                .join(
                    MemoryDreamOperationModel,
                    MemoryDreamOperationModel.id == MemoryDreamOperationSourceModel.operation_id,
                )
                .where(
                    MemoryDreamOperationSourceModel.fact_id == fact_id,
                    MemoryDreamOperationModel.id > operation_id,
                    MemoryDreamOperationModel.status == "committed",
                )
            )
            or 0
        )
        if dependency:
            return {row.id for row in evidence}
        added_ids = {int(item) for item in json.loads(operation.added_evidence_ids_json)}
        original = [row for row in evidence if row.id not in added_ids]
        if operation.operation_type != "merge" and original:
            return {row.id for row in evidence}
        source_ids = tuple(
            await session.scalars(
                select(MemoryDreamOperationSourceModel.fact_id)
                .join(
                    MemoryFactRelationModel,
                    MemoryFactRelationModel.source_fact_id
                    == MemoryDreamOperationSourceModel.fact_id,
                )
                .where(
                    MemoryDreamOperationSourceModel.operation_id == operation_id,
                    MemoryFactRelationModel.target_fact_id == fact_id,
                    MemoryFactRelationModel.relation_type.in_(("refines", "equivalent")),
                )
                .order_by(MemoryDreamOperationSourceModel.position)
            )
        )
        chosen: list[Any] = list(original)
        source_rows = (
            await session.execute(
                select(
                    MemoryEvidenceModel.fact_id,
                    MemoryEvidenceModel.event_id,
                    MemoryEvidenceModel.tool_receipt_id,
                ).where(MemoryEvidenceModel.fact_id.in_(source_ids))
            )
        ).all()
        source_keys_by_id: dict[int, set[tuple[int | None, int | None]]] = {}
        for source_id, event_id, receipt_id in source_rows:
            source_keys_by_id.setdefault(source_id, set()).add((event_id, receipt_id))
        for source_id in source_ids:
            source_keys = source_keys_by_id.get(source_id, set())
            matches = [
                row
                for row in evidence
                if row.id in added_ids and (row.event_id, row.tool_receipt_id) in source_keys
            ]
            if len(matches) <= 2:
                chosen.extend(matches)
            elif matches:
                chosen.extend((matches[0], matches[-1]))
        unique: list[Any] = []
        seen: set[int] = set()
        for row in chosen:
            if row.id in seen:
                continue
            seen.add(row.id)
            unique.append(row)
            if operation.operation_type != "merge" and len(unique) >= 12:
                break
        return {row.id for row in unique}

    async def _prepare_dream_rebase(
        self,
        *,
        fact_id: int,
        operation_id: int,
        deleted_ids: tuple[int, ...],
        session: Any,
    ) -> tuple[Any, Any, Any, Any, str]:
        operation = await session.get(MemoryDreamOperationModel, operation_id)
        if operation is None:
            raise RuntimeError("Dream provenance operation disappeared")
        result = await session.scalar(
            select(MemoryDreamOperationResultModel).where(
                MemoryDreamOperationResultModel.operation_id == operation_id,
                MemoryDreamOperationResultModel.fact_id == fact_id,
            )
        )
        source = await session.scalar(
            select(MemoryDreamOperationSourceModel).where(
                MemoryDreamOperationSourceModel.operation_id == operation_id,
                MemoryDreamOperationSourceModel.fact_id == fact_id,
            )
        )
        checkpoint = await session.get(MemoryDreamFactCheckpointModel, fact_id)
        deleted = set(deleted_ids)
        added_json = json.dumps(
            [
                int(item)
                for item in json.loads(operation.added_evidence_ids_json)
                if int(item) not in deleted
            ]
        )
        return operation, result, source, checkpoint, added_json

    @staticmethod
    def _apply_dream_rebase(*, rebase: tuple[Any, Any, Any, Any, str], signature: str) -> None:
        operation, result, source, checkpoint, added_json = rebase
        operation.added_evidence_ids_json = added_json
        if result is not None:
            result.result_signature = signature
            if result.position == 0:
                operation.result_signature = signature
        if source is not None:
            source.after_signature = signature
        if checkpoint is not None and checkpoint.last_operation_id == operation.id:
            checkpoint.signature = signature
            checkpoint.checked_at = datetime.now(UTC)

    async def _finish_item(
        self,
        item_id: int,
        *,
        status: str,
        before: int,
        after: int,
        error_category: str | None,
        session: AsyncSession,
    ) -> None:
        now = datetime.now(UTC)
        await session.execute(
            update(MemoryEvidenceCompactionItemModel)
            .where(
                MemoryEvidenceCompactionItemModel.id == item_id,
                MemoryEvidenceCompactionItemModel.status == "processing",
            )
            .values(
                status=status,
                evidence_after=after,
                deleted_count=max(0, before - after),
                error_category=error_category,
                updated_at=now,
                completed_at=now,
            )
        )

    async def _refresh_run(self, run_id: int) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            rows = (
                await session.execute(
                    select(
                        MemoryEvidenceCompactionItemModel.status,
                        func.count(MemoryEvidenceCompactionItemModel.id),
                        func.sum(MemoryEvidenceCompactionItemModel.evidence_before),
                        func.sum(MemoryEvidenceCompactionItemModel.evidence_after),
                    )
                    .where(MemoryEvidenceCompactionItemModel.run_id == run_id)
                    .group_by(MemoryEvidenceCompactionItemModel.status)
                )
            ).all()
            counts = {str(row[0]): int(row[1]) for row in rows}
            before = sum(int(row[2] or 0) for row in rows)
            after = sum(int(row[3] or 0) for row in rows)
            await session.execute(
                update(MemoryEvidenceCompactionRunModel)
                .where(MemoryEvidenceCompactionRunModel.id == run_id)
                .values(
                    scanned_facts=sum(counts.values()),
                    completed_items=counts.get("completed", 0),
                    skipped_items=counts.get("skipped", 0),
                    failed_items=counts.get("failed", 0),
                    evidence_before=before,
                    evidence_after=after,
                    error_category=("item_failed" if counts.get("failed", 0) else None),
                    updated_at=now,
                )
            )

    async def _finish_run(self, run_id: int) -> None:
        await self._refresh_run(run_id)
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(MemoryEvidenceCompactionRunModel, run_id)
            if row is None or row.status != "running":
                return
            row.status = "partial_failed" if row.failed_items else "completed"
            row.updated_at = now
            row.completed_at = now


class EvidenceCompactionWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        service: EvidenceCompactionService,
    ) -> None:
        self._settings = settings
        self._service = service
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.waiting_for_lock = False
        self.holding_lock = False
        self.last_success_at: datetime | None = None
        self.last_error_category: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self._settings.memory_evidence_compaction_enabled or self.running:
            return
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="memory-evidence-compaction")

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                async with asyncio.timeout(_BATCH_TIMEOUT_SECONDS):
                    self.waiting_for_lock = False
                    self.holding_lock = True
                    try:
                        processed = await self._service.run_batch()
                        self.last_success_at = datetime.now(UTC)
                        self.last_error_category = None
                    finally:
                        self.holding_lock = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error_category = type(exc).__name__
                logger.warning(
                    "memory_evidence_compaction_loop_failed error_category=%s",
                    type(exc).__name__,
                )
                processed = 0
            finally:
                self.waiting_for_lock = False
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=1.0
                    if processed
                    else min(60.0, self._settings.memory_dream_poll_seconds),
                )
            except TimeoutError:
                pass
