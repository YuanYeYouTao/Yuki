"""Fingerprint-guarded, explicit repair for unambiguous derived-data defects."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import text

from qq_ai_bot.memory.eligibility import sql_fact_event_evidence_predicate
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.quality.audit import MemoryProductionQualityAudit
from qq_ai_bot.memory.quality.models import HygienePlan
from qq_ai_bot.persistence.database import Database


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
            invalid = tuple(
                int(item)
                for item in await session.scalars(
                    text(
                        f"""
                        SELECT DISTINCT f.id FROM memory_facts f
                        WHERE f.source_type IN ('automatic','rebuild')
                          AND f.status!='invalidated'
                          AND NOT EXISTS (
                            SELECT 1 FROM memory_evidence e
                            JOIN chat_events c ON c.id=e.event_id
                            WHERE e.fact_id=f.id
                              AND {sql_fact_event_evidence_predicate()}
                              AND e.source_speaker_user_id=c.sender_user_id
                              AND trim(e.excerpt)!=''
                              AND instr(c.content,e.excerpt)>0
                          )
                        ORDER BY f.id LIMIT 500
                        """
                    )
                )
            )
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
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            invalidated = 0
            for fact_id in current.invalid_fact_ids:
                row = (
                    await session.execute(
                        text("SELECT status, conflict_state FROM memory_facts WHERE id=:fact_id"),
                        {"fact_id": fact_id},
                    )
                ).first()
                if row is None or str(row.status) == "invalidated":
                    continue
                await session.execute(
                    text(
                        """
                        UPDATE memory_facts SET status='invalidated', conflict_state='clear',
                          invalidated_reason='administrator_invalidated', updated_at=:now
                        WHERE id=:fact_id AND source_type IN ('automatic','rebuild')
                        """
                    ),
                    {"fact_id": fact_id, "now": now},
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO memory_fact_state_events (
                          fact_id, action, from_status, to_status,
                          from_conflict_state, to_conflict_state, reason_code,
                          source_event_id, actor_user_id, created_at
                        ) VALUES (
                          :fact_id, 'invalidated', :from_status, 'invalidated',
                          :from_conflict, 'clear', 'invalid_provenance', NULL, NULL, :now
                        )
                        """
                    ),
                    {
                        "fact_id": fact_id,
                        "from_status": str(row.status),
                        "from_conflict": str(row.conflict_state),
                        "now": now,
                    },
                )
                invalidated += 1
            if current.rebuild_fts:
                await session.execute(
                    text("INSERT INTO memory_facts_fts(memory_facts_fts) VALUES ('rebuild')")
                )
            profile = (
                await session.execute(
                    text(
                        "SELECT id, document_template_version FROM memory_embedding_profiles "
                        "ORDER BY id DESC LIMIT 1"
                    )
                )
            ).first()
            if profile is not None:
                builder = EmbeddingDocumentBuilder(
                    template_version=int(profile.document_template_version),
                    max_characters=4000,
                )
                for fact_id in current.enqueue_embedding_fact_ids:
                    fact = (
                        await session.execute(
                            text(
                                "SELECT kind, category, memory_key, content FROM memory_facts "
                                "WHERE id=:fact_id AND status='active'"
                            ),
                            {"fact_id": fact_id},
                        )
                    ).first()
                    if fact is None:
                        continue
                    content_hash = builder.content_hash_fields(
                        kind=str(fact.kind),
                        category=str(fact.category),
                        memory_key=str(fact.memory_key),
                        content=str(fact.content),
                    )
                    await session.execute(
                        text(
                            """
                            INSERT INTO memory_embedding_jobs (
                              fact_id, profile_id, content_hash, status, attempts,
                              next_attempt_at, created_at, updated_at, error_category
                            ) VALUES (
                              :fact_id, :profile_id, :content_hash, 'pending', 0,
                              :now, :now, :now, NULL
                            ) ON CONFLICT(fact_id,profile_id) DO UPDATE SET
                              content_hash=excluded.content_hash, status='pending', attempts=0,
                              next_attempt_at=excluded.next_attempt_at,
                              updated_at=excluded.updated_at, error_category=NULL
                            """
                        ),
                        {
                            "fact_id": fact_id,
                            "profile_id": int(profile.id),
                            "content_hash": content_hash,
                            "now": now,
                        },
                    )
            for run_id in current.purge_terminal_rebuild_run_ids:
                await session.execute(
                    text("DELETE FROM memory_rebuild_proposals WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )
                await session.execute(
                    text("DELETE FROM memory_rebuild_items WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )
        if self._metrics is not None and invalidated:
            self._metrics.increment("hygiene_invalidated_count", invalidated)
        return current
