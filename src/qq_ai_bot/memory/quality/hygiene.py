"""Fingerprint-guarded, explicit repair for unambiguous derived-data defects."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.engine import CursorResult

from qq_ai_bot.memory.eligibility import sql_fact_tool_evidence_predicate
from qq_ai_bot.memory.embedding.jobs import MemoryEmbeddingJobRepository
from qq_ai_bot.memory.embedding.models import EmbeddingProviderProfile, MemoryEmbeddingProfileRecord
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.quality.audit import MemoryProductionQualityAudit
from qq_ai_bot.memory.quality.event_evidence import facts_with_valid_event_evidence
from qq_ai_bot.memory.quality.models import HygienePlan
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryEmbeddingProfileModel

_SCAN_PAGE_SIZE = 500


class MemoryProvenanceHygiene:
    def __init__(
        self,
        database: Database,
        *,
        metrics: MemoryLifecycleMetrics | None = None,
    ) -> None:
        self._database = database
        self._metrics = metrics

    async def scan(self) -> HygienePlan:
        audit = await MemoryProductionQualityAudit(self._database).run()
        async with self._database.sessions() as session:
            invalid_rows: list[int] = []
            last_id = 0
            while len(invalid_rows) < 500:
                candidates = tuple(
                    int(item)
                    for item in await session.scalars(
                        text(
                            f"""
                        SELECT DISTINCT f.id FROM memory_facts f
                        WHERE f.source_type IN ('automatic','rebuild')
                          AND f.status!='invalidated' AND f.id>:last_id
                          AND NOT EXISTS (
                            SELECT 1 FROM memory_evidence e
                            JOIN memory_tool_receipts t ON t.id=e.tool_receipt_id
                            LEFT JOIN chat_events c ON c.id=t.trigger_event_id
                            LEFT JOIN canonical_conversations v ON v.id=c.canonical_conversation_id
                            WHERE e.fact_id=f.id AND {sql_fact_tool_evidence_predicate()}
                          )
                            ORDER BY f.id LIMIT :page_size
                        """
                        ),
                        {"last_id": last_id, "page_size": _SCAN_PAGE_SIZE},
                    )
                )
                if not candidates:
                    break
                valid = await facts_with_valid_event_evidence(session, candidates)
                invalid_rows.extend(item for item in candidates if item not in valid)
                last_id = candidates[-1]
            invalid = tuple(invalid_rows[:500])
            latest_profile = await session.execute(
                text(
                    "SELECT id, document_template_version FROM memory_embedding_profiles "
                    "ORDER BY id DESC LIMIT 1"
                )
            )
            profile = latest_profile.first()
            missing_embeddings: tuple[int, ...] = ()
            if profile is not None:
                missing_embeddings = tuple(
                    int(item)
                    for item in await session.scalars(
                        text(
                            """
                            SELECT f.id FROM memory_facts f
                            WHERE f.status='active'
                              AND NOT EXISTS (SELECT 1 FROM memory_embeddings e
                                WHERE e.fact_id=f.id AND e.profile_id=:profile_id)
                              AND NOT EXISTS (SELECT 1 FROM memory_embedding_jobs j
                                WHERE j.fact_id=f.id AND j.profile_id=:profile_id
                                  AND j.status IN ('pending','processing'))
                            ORDER BY f.id LIMIT 1000
                            """
                        ),
                        {"profile_id": int(profile.id)},
                    )
                )
            terminal_runs = tuple(
                int(item)
                for item in await session.scalars(
                    text(
                        """
                        SELECT r.id FROM memory_rebuild_runs r
                        WHERE r.status IN ('completed','cancelled','failed')
                          AND (EXISTS (SELECT 1 FROM memory_rebuild_items i WHERE i.run_id=r.id)
                            OR EXISTS (SELECT 1 FROM memory_rebuild_proposals p
                                      WHERE p.run_id=r.id))
                        ORDER BY r.id LIMIT 500
                        """
                    )
                )
            )
        issue_counts = {item.issue_code: item.count for item in audit.issues if item.count}
        rebuild_fts = bool(
            issue_counts.get("fts_missing_active_fact") or issue_counts.get("fts_orphan_row")
        )
        payload = {
            "database_fingerprint": audit.database_fingerprint,
            "issue_counts": issue_counts,
            "invalid_fact_ids": invalid,
            "rebuild_fts": rebuild_fts,
            "enqueue_embedding_fact_ids": missing_embeddings,
            "purge_terminal_rebuild_run_ids": terminal_runs,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return HygienePlan(
            generated_at=datetime.now(UTC),
            database_fingerprint=audit.database_fingerprint,
            fingerprint=fingerprint,
            issue_counts=issue_counts,
            invalid_fact_ids=invalid,
            rebuild_fts=rebuild_fts,
            enqueue_embedding_fact_ids=missing_embeddings,
            purge_terminal_rebuild_run_ids=terminal_runs,
        )

    async def apply(self, fingerprint: str) -> HygienePlan:
        current = await self.scan()
        if current.fingerprint != fingerprint:
            raise RuntimeError("memory hygiene fingerprint changed; run scan again")
        invalidated = 0
        for offset in range(0, len(current.invalid_fact_ids), 128):
            invalidated += await self._invalidate_page(
                current.invalid_fact_ids[offset : offset + 128]
            )
        async with self._database.sessions() as reader:
            profile = await reader.scalar(
                select(MemoryEmbeddingProfileModel)
                .order_by(MemoryEmbeddingProfileModel.id.desc())
                .limit(1)
            )
        if profile is not None:
            record = MemoryEmbeddingProfileRecord(
                id=profile.id,
                created_at=profile.created_at,
                profile=EmbeddingProviderProfile(
                    provider_id=profile.provider_id,
                    model_id=profile.model_id,
                    dimensions=profile.dimensions,
                    output_type=profile.output_type,
                    document_template_version=profile.document_template_version,
                    endpoint_identity=profile.endpoint_identity,
                    fingerprint=profile.fingerprint,
                ),
            )
            jobs = MemoryEmbeddingJobRepository(
                self._database,
                profile=record,
                documents=EmbeddingDocumentBuilder(
                    template_version=profile.document_template_version, max_characters=4000
                ),
            )
            await jobs.enqueue_facts(
                current.enqueue_embedding_fact_ids, only_if_latest_profile=True
            )
        for offset in range(0, len(current.purge_terminal_rebuild_run_ids), 128):
            await self._purge_page(current.purge_terminal_rebuild_run_ids[offset : offset + 128])
        if self._metrics is not None and invalidated:
            self._metrics.increment("hygiene_invalidated_count", invalidated)
        # FTS work remains visible in the returned plan and has its own explicit window.
        return current

    async def _invalidate_page(self, fact_ids: tuple[int, ...]) -> int:
        # A SQLite read snapshot can upgrade only if no competing commit occurred.
        # BUSY_SNAPSHOT rolls the entire page back; rescan before retrying maintenance.
        async with self._database.sessions() as session, session.begin():
            await session.execute(text("BEGIN"))
            valid = await facts_with_valid_event_evidence(session, fact_ids)
            placeholders = ",".join(f":fact_{index}" for index in range(len(fact_ids)))
            params = {f"fact_{index}": value for index, value in enumerate(fact_ids)}
            rows = tuple(
                await session.execute(
                    text(f"""
                SELECT f.id, f.status, f.conflict_state, f.updated_at FROM memory_facts f
                WHERE f.id IN ({placeholders}) AND f.source_type IN ('automatic','rebuild')
                  AND f.status!='invalidated' AND NOT EXISTS (
                    SELECT 1 FROM memory_evidence e
                    JOIN memory_tool_receipts t ON t.id=e.tool_receipt_id
                    LEFT JOIN chat_events c ON c.id=t.trigger_event_id
                    LEFT JOIN canonical_conversations v ON v.id=c.canonical_conversation_id
                    WHERE e.fact_id=f.id AND {sql_fact_tool_evidence_predicate()}
                  )
            """),
                    params,
                )
            )
            prepared = tuple(row for row in rows if row.id not in valid)
            now = datetime.now(UTC)
            invalidated = 0
            for row in prepared:
                result = await session.execute(
                    text("""
                    UPDATE memory_facts SET status='invalidated', conflict_state='clear',
                      invalidated_reason='administrator_invalidated', updated_at=:now
                    WHERE id=:fact_id AND updated_at=:old_time AND status=:from_status
                      AND conflict_state=:from_conflict AND source_type IN ('automatic','rebuild')
                """),
                    dict(
                        fact_id=row.id,
                        old_time=row.updated_at,
                        from_status=row.status,
                        from_conflict=row.conflict_state,
                        now=now,
                    ),
                )
                if cast(CursorResult[Any], result).rowcount != 1:
                    continue
                await session.execute(
                    text("""
                    INSERT INTO memory_fact_state_events (
                      fact_id, action, from_status, to_status, from_conflict_state,
                      to_conflict_state, reason_code, source_event_id, actor_user_id, created_at
                    ) VALUES (:fact_id, 'invalidated', :from_status, 'invalidated',
                      :from_conflict, 'clear', 'invalid_provenance', NULL, NULL, :now)
                """),
                    dict(
                        fact_id=row.id,
                        from_status=row.status,
                        from_conflict=row.conflict_state,
                        now=now,
                    ),
                )
                invalidated += 1
            return invalidated

    async def _purge_page(self, run_ids: tuple[int, ...]) -> None:
        async with self._database.sessions() as session, session.begin():
            await session.execute(text("BEGIN"))
            placeholders = ",".join(f":run_{index}" for index in range(len(run_ids)))
            params = {f"run_{index}": value for index, value in enumerate(run_ids)}
            terminal = tuple(
                await session.scalars(
                    text(f"""
                SELECT id FROM memory_rebuild_runs WHERE id IN ({placeholders})
                  AND status IN ('completed','cancelled','failed')
            """),
                    params,
                )
            )
            if not terminal:
                return
            placeholders = ",".join(f":run_{index}" for index in range(len(terminal)))
            params = {f"run_{index}": value for index, value in enumerate(terminal)}
            for table in ("memory_rebuild_proposals", "memory_rebuild_items"):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE run_id IN ({placeholders})"), params
                )

    async def rebuild_fts(self, fingerprint: str) -> HygienePlan:
        """Explicit full-index maintenance; intentionally owns the writer for rebuild."""
        current = await self.scan()
        if current.fingerprint != fingerprint:
            raise RuntimeError("memory hygiene fingerprint changed; run scan again")
        if current.rebuild_fts:
            async with self._database.immediate_session() as writer:
                await writer.execute(
                    text("INSERT INTO memory_facts_fts(memory_facts_fts) VALUES ('rebuild')")
                )
        return current
