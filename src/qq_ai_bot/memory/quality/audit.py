"""Content-free, model-free production Memory V2 integrity audit."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from qq_ai_bot.memory.eligibility import (
    sql_fact_tool_evidence_predicate,
    sql_human_evidence_predicate,
)
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.quality.event_evidence import audit_event_evidence
from qq_ai_bot.memory.quality.models import ProductionAuditIssue, ProductionAuditReport
from qq_ai_bot.persistence.database import Database

_SAMPLE_LIMIT: Final = 20


class MemoryProductionQualityAudit:
    """Report bounded row IDs and counts, never fact or event content."""

    def __init__(
        self,
        database: Database,
        *,
        metrics: MemoryLifecycleMetrics | None = None,
    ) -> None:
        self._database = database
        self._metrics = metrics

    async def run(self) -> ProductionAuditReport:
        checks = self._checks()
        issues: list[ProductionAuditIssue] = []
        async with self._database.sessions() as session:
            try:
                issues.extend(await audit_event_evidence(session))
            except (DatabaseError, ValueError, TypeError):
                issues.append(
                    ProductionAuditIssue(
                        issue_code="event_evidence_query_failed", severity="error", count=1
                    )
                )
            for code, severity, query in checks:
                try:
                    rows = tuple((await session.execute(text(query))).all())
                except DatabaseError:
                    issues.append(
                        ProductionAuditIssue(
                            issue_code=f"{code}_query_failed",
                            severity="error",
                            count=1,
                        )
                    )
                    continue
                count = int(rows[0][0]) if rows else 0
                samples = tuple(int(row[1]) for row in rows if row[1] is not None)
                issues.append(
                    ProductionAuditIssue(
                        issue_code=code,
                        severity=severity,
                        count=count,
                        sample_ids=samples[:_SAMPLE_LIMIT],
                    )
                )
        report = ProductionAuditReport(
            generated_at=datetime.now(UTC),
            database_fingerprint=hashlib.sha256(self._database.url.encode("utf-8")).hexdigest(),
            issues=tuple(issues),
        )
        if self._metrics is not None:
            self._metrics.increment("audit_issue_count", sum(item.count for item in issues))
        return report

    @staticmethod
    def _checks() -> tuple[tuple[str, str, str], ...]:
        """Each query returns total count plus at most 20 content-free row IDs."""

        return (
            (
                "fact_scope_identity_invalid",
                "error",
                _query(
                    "memory_facts",
                    "NOT ((scope_type='person' AND canonical_subject_person_id IS NOT NULL "
                    "AND canonical_subject_space_id IS NULL) OR (scope_type='person_group' "
                    "AND canonical_subject_person_id IS NOT NULL "
                    "AND canonical_subject_space_id IS NOT NULL) OR (scope_type='group' "
                    "AND canonical_subject_person_id IS NULL "
                    "AND canonical_subject_space_id IS NOT NULL) OR (scope_type='self' "
                    "AND canonical_subject_person_id IS NULL "
                    "AND canonical_subject_space_id IS NULL))",
                ),
            ),
            (
                "self_visibility_invalid",
                "error",
                _query(
                    "memory_facts",
                    "(scope_type!='self' AND (visibility_type IS NOT NULL OR "
                    "canonical_visibility_person_id IS NOT NULL OR "
                    "canonical_visibility_space_id IS NOT NULL)) OR "
                    "(scope_type='self' AND NOT ((visibility_type='global' AND "
                    "canonical_visibility_person_id IS NULL AND "
                    "canonical_visibility_space_id IS NULL) OR "
                    "(visibility_type='private' AND canonical_visibility_person_id IS NOT NULL "
                    "AND canonical_visibility_space_id IS NULL) OR (visibility_type='group' AND "
                    "canonical_visibility_person_id IS NULL "
                    "AND canonical_visibility_space_id IS NOT NULL)))",
                ),
            ),
            (
                "third_party_scope_invalid",
                "error",
                _query(
                    "memory_facts",
                    "authority='third_party' AND scope_type!='person_group'",
                ),
            ),
            (
                "active_slot_duplicate",
                "error",
                """
                WITH bad AS (
                  SELECT MIN(id) AS id FROM memory_facts WHERE status='active'
                  GROUP BY scope_type, COALESCE(canonical_subject_person_id,''),
                           COALESCE(canonical_subject_space_id,''),
                           COALESCE(visibility_type,''),
                           COALESCE(canonical_visibility_person_id,''),
                           COALESCE(canonical_visibility_space_id,''),
                           CASE WHEN scope_type='self' THEN '' ELSE kind END,
                           memory_key HAVING COUNT(*) > 1
                ), tally AS (SELECT COUNT(*) AS n FROM bad)
                SELECT tally.n, sample.id FROM tally LEFT JOIN
                  (SELECT id FROM bad ORDER BY id LIMIT 20) sample ON 1=1
                """,
            ),
            (
                "contested_state_invalid",
                "error",
                _query(
                    "memory_facts",
                    # A challenged active fact stays active; its alternative has
                    # status=contested. Both carry conflict_state=contested.
                    "status='contested' AND conflict_state!='contested'",
                ),
            ),
            (
                "fact_authority_invalid",
                "error",
                _query(
                    "memory_facts",
                    "authority NOT IN ('explicit','self_report','group_report','third_party',"
                    "'agent_reflection')",
                ),
            ),
            (
                "fact_temporal_range_invalid",
                "error",
                _query(
                    "memory_facts",
                    "valid_from IS NOT NULL AND valid_until IS NOT NULL AND valid_from>valid_until",
                ),
            ),
            (
                "invalidation_reason_invalid",
                "error",
                _query(
                    "memory_facts",
                    "(status='invalidated' AND invalidated_reason IS NULL) "
                    "OR (status!='invalidated' AND invalidated_reason IS NOT NULL)",
                ),
            ),
            (
                "fact_without_evidence",
                "warning",
                _query(
                    "memory_facts f",
                    "f.source_type IN ('automatic','rebuild') AND NOT EXISTS "
                    "(SELECT 1 FROM memory_evidence e WHERE e.fact_id=f.id)",
                    id_expression="f.id",
                ),
            ),
            (
                "superseded_without_chain",
                "error",
                _query(
                    "memory_facts f",
                    "f.status='superseded' AND NOT EXISTS "
                    "(SELECT 1 FROM memory_facts n WHERE n.supersedes_id=f.id) AND NOT EXISTS "
                    "(SELECT 1 FROM memory_fact_relations r "
                    "WHERE r.source_fact_id=f.id OR r.target_fact_id=f.id) AND NOT EXISTS "
                    "(SELECT 1 FROM memory_fact_state_events s "
                    "WHERE s.fact_id=f.id AND s.action IN ('superseded','merged'))",
                    id_expression="f.id",
                ),
            ),
            (
                "evidence_source_event_missing",
                "error",
                _query(
                    "memory_evidence e",
                    "e.event_id IS NOT NULL AND NOT EXISTS "
                    "(SELECT 1 FROM chat_events c WHERE c.id=e.event_id)",
                    id_expression="e.id",
                ),
            ),
            (
                "evidence_tool_source_invalid",
                "error",
                _query(
                    "memory_evidence e JOIN memory_facts f ON f.id=e.fact_id",
                    "e.tool_receipt_id IS NOT NULL AND NOT EXISTS ("
                    "SELECT 1 FROM memory_tool_receipts t "
                    "JOIN chat_events c ON c.id=t.trigger_event_id "
                    "JOIN canonical_conversations v ON v.id=c.canonical_conversation_id "
                    "WHERE t.id=e.tool_receipt_id "
                    f"AND {sql_fact_tool_evidence_predicate()})",
                    id_expression="e.id",
                ),
            ),
            (
                "evidence_relation_authority_mismatch",
                "error",
                _query(
                    "memory_evidence e",
                    "(e.relation='explicit_command' AND e.authority!='explicit') OR "
                    "(e.relation='group_statement' AND e.authority!='group_report') OR "
                    "(e.relation='third_party_statement' AND e.authority!='third_party') OR "
                    "(e.relation IN ('self_statement','confirmation','correction','retraction') "
                    "AND e.authority NOT IN ('self_report','explicit'))",
                    id_expression="e.id",
                ),
            ),
            (
                "evidence_authority_exceeds_fact",
                "error",
                _query(
                    "memory_evidence e JOIN memory_facts f ON f.id=e.fact_id",
                    "CASE e.authority WHEN 'explicit' THEN 3 WHEN 'self_report' THEN 2 "
                    "WHEN 'group_report' THEN 1 ELSE 0 END > CASE f.authority "
                    "WHEN 'explicit' THEN 3 WHEN 'self_report' THEN 2 "
                    "WHEN 'group_report' THEN 1 ELSE 0 END",
                    id_expression="e.id",
                ),
            ),
            (
                "evidence_duplicate_event",
                "error",
                """
                WITH bad AS (
                  SELECT MIN(id) AS id FROM memory_evidence
                  GROUP BY fact_id,event_id HAVING COUNT(*)>1
                ), tally AS (SELECT COUNT(*) AS n FROM bad)
                SELECT tally.n,sample.id FROM tally LEFT JOIN
                  (SELECT id FROM bad ORDER BY id LIMIT 20) sample ON 1=1
                """,
            ),
            (
                "orphan_relation",
                "error",
                _query(
                    "memory_fact_relations r",
                    "NOT EXISTS (SELECT 1 FROM memory_facts f WHERE f.id=r.source_fact_id) "
                    "OR NOT EXISTS (SELECT 1 FROM memory_facts f WHERE f.id=r.target_fact_id)",
                    id_expression="r.id",
                ),
            ),
            (
                "cross_target_relation",
                "error",
                _query(
                    "memory_fact_relations r JOIN memory_facts s ON s.id=r.source_fact_id "
                    "JOIN memory_facts t ON t.id=r.target_fact_id",
                    "s.scope_type!=t.scope_type OR "
                    "COALESCE(s.canonical_subject_person_id,'')!="
                    "COALESCE(t.canonical_subject_person_id,'') OR "
                    "COALESCE(s.canonical_subject_space_id,'')!="
                    "COALESCE(t.canonical_subject_space_id,'') OR "
                    "COALESCE(s.visibility_type,'')!=COALESCE(t.visibility_type,'') OR "
                    "COALESCE(s.canonical_visibility_person_id,'')!="
                    "COALESCE(t.canonical_visibility_person_id,'') OR "
                    "COALESCE(s.canonical_visibility_space_id,'')!="
                    "COALESCE(t.canonical_visibility_space_id,'')",
                    id_expression="r.id",
                ),
            ),
            (
                "self_relation",
                "error",
                _query(
                    "memory_fact_relations r",
                    "r.source_fact_id=r.target_fact_id",
                    id_expression="r.id",
                ),
            ),
            (
                "relation_type_invalid",
                "error",
                _query(
                    "memory_fact_relations r",
                    "r.relation_type NOT IN ('supports','contradicts','refines','equivalent')",
                    id_expression="r.id",
                ),
            ),
            (
                "contradiction_state_mismatch",
                "error",
                _query(
                    "memory_fact_relations r JOIN memory_facts s ON s.id=r.source_fact_id "
                    "JOIN memory_facts t ON t.id=r.target_fact_id",
                    "r.relation_type='contradicts' AND s.status='active' AND t.status='active' "
                    "AND (s.conflict_state!='contested' OR t.conflict_state!='contested')",
                    id_expression="r.id",
                ),
            ),
            (
                "orphan_state_event",
                "error",
                _query(
                    "memory_fact_state_events s",
                    "NOT EXISTS (SELECT 1 FROM memory_facts f WHERE f.id=s.fact_id)",
                    id_expression="s.id",
                ),
            ),
            (
                "fts_missing_active_fact",
                "error",
                _query(
                    "memory_facts f",
                    "f.status='active' AND NOT EXISTS "
                    "(SELECT 1 FROM memory_facts_fts_docsize d WHERE d.id=f.id)",
                    id_expression="f.id",
                ),
            ),
            (
                "fts_orphan_row",
                "error",
                _query(
                    "memory_facts_fts_docsize d",
                    "NOT EXISTS (SELECT 1 FROM memory_facts f WHERE f.id=d.id)",
                    id_expression="d.id",
                ),
            ),
            (
                "fts_trigger_missing",
                "error",
                """
                WITH tally AS (
                  SELECT CASE WHEN COUNT(*)=3 THEN 0 ELSE 1 END AS n
                  FROM sqlite_master
                  WHERE type='trigger' AND name IN (
                    'memory_facts_fts_ai','memory_facts_fts_ad','memory_facts_fts_au'
                  )
                ) SELECT n, NULL FROM tally
                """,
            ),
            (
                "fts_query_smoke_failed",
                "error",
                """
                WITH tally AS (
                  SELECT CASE WHEN EXISTS (
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='memory_facts_fts'
                  ) THEN 0 ELSE 1 END AS n
                ) SELECT n, NULL FROM tally
                """,
            ),
            (
                "embedding_orphan_row",
                "error",
                _query(
                    "memory_embeddings e",
                    "NOT EXISTS (SELECT 1 FROM memory_facts f WHERE f.id=e.fact_id) "
                    "OR NOT EXISTS (SELECT 1 FROM memory_embedding_profiles p "
                    "WHERE p.id=e.profile_id)",
                    id_expression="e.id",
                ),
            ),
            (
                "embedding_job_orphan",
                "error",
                _query(
                    "memory_embedding_jobs j",
                    "NOT EXISTS (SELECT 1 FROM memory_facts f WHERE f.id=j.fact_id) "
                    "OR NOT EXISTS (SELECT 1 FROM memory_embedding_profiles p "
                    "WHERE p.id=j.profile_id)",
                    id_expression="j.id",
                ),
            ),
            (
                "embedding_dimension_mismatch",
                "error",
                _query(
                    "memory_embeddings e JOIN memory_embedding_profiles p ON p.id=e.profile_id",
                    "length(e.vector_blob)!=p.dimensions*4",
                    id_expression="e.id",
                ),
            ),
            (
                "embedding_hash_mismatch",
                "error",
                _query(
                    "memory_embeddings e JOIN memory_embedding_jobs j "
                    "ON j.fact_id=e.fact_id AND j.profile_id=e.profile_id",
                    "j.status='done' AND j.content_hash!=e.content_hash",
                    id_expression="e.id",
                ),
            ),
            (
                "embedding_missing_current_vector",
                "warning",
                _query(
                    "memory_facts f",
                    "f.status='active' AND EXISTS "
                    "(SELECT 1 FROM memory_embedding_profiles) AND NOT EXISTS "
                    "(SELECT 1 FROM memory_embeddings e WHERE e.fact_id=f.id AND "
                    "e.profile_id=(SELECT id FROM memory_embedding_profiles "
                    "ORDER BY id DESC LIMIT 1))",
                    id_expression="f.id",
                ),
            ),
            (
                "embedding_failed_job",
                "warning",
                _query(
                    "memory_embedding_jobs j",
                    "j.status='failed'",
                    id_expression="j.id",
                ),
            ),
            (
                "embedding_old_profile",
                "warning",
                _query(
                    "memory_embedding_profiles p",
                    "p.id!=(SELECT id FROM memory_embedding_profiles ORDER BY id DESC LIMIT 1) "
                    "AND EXISTS (SELECT 1 FROM memory_embeddings e WHERE e.profile_id=p.id)",
                    id_expression="p.id",
                ),
            ),
            (
                "memory_job_source_invalid",
                "error",
                _query(
                    "memory_jobs j JOIN chat_events c ON c.id=j.event_id",
                    "c.direction!='inbound' OR trim(c.content)='' "
                    f"OR NOT ({sql_human_evidence_predicate('c')})",
                    id_expression="j.id",
                ),
            ),
            (
                "multiple_active_rebuilds",
                "error",
                """
                WITH tally AS (
                  SELECT CASE WHEN COUNT(*) > 1 THEN COUNT(*) ELSE 0 END AS n,
                         MIN(id) AS id
                  FROM memory_rebuild_runs
                  WHERE status IN ('extracting','committing')
                ) SELECT n, CASE WHEN n>0 THEN id ELSE NULL END FROM tally
                """,
            ),
            (
                "rebuild_stuck_processing",
                "warning",
                _query(
                    "memory_rebuild_items i",
                    "i.status='extracting' AND i.updated_at<datetime('now','-1 hour')",
                    id_expression="i.id",
                ),
            ),
            (
                "rebuild_pending_review",
                "warning",
                _query(
                    "memory_rebuild_proposals p",
                    "p.review_status='pending'",
                    id_expression="p.id",
                ),
            ),
            (
                "rebuild_committed_without_receipt",
                "error",
                _query(
                    "memory_rebuild_proposals p",
                    "p.commit_status='committed' AND NOT EXISTS "
                    "(SELECT 1 FROM memory_jobs j WHERE j.event_id=p.event_id "
                    "AND j.rebuild_run_id=p.run_id AND j.status='done')",
                    id_expression="p.id",
                ),
            ),
            (
                "rebuild_receipt_without_proposal",
                "error",
                _query(
                    "memory_jobs j",
                    "j.processing_source='rebuild' AND j.rebuild_run_id IS NOT NULL "
                    "AND j.status='done' AND j.outcome='claims_applied' AND NOT EXISTS "
                    "(SELECT 1 FROM memory_rebuild_proposals p WHERE p.event_id=j.event_id "
                    "AND p.run_id=j.rebuild_run_id)",
                    id_expression="j.id",
                ),
            ),
            (
                "terminal_rebuild_staging",
                "warning",
                _query(
                    "memory_rebuild_runs r",
                    "r.status IN ('completed','cancelled','failed') AND "
                    "(EXISTS (SELECT 1 FROM memory_rebuild_items i WHERE i.run_id=r.id) "
                    "OR EXISTS (SELECT 1 FROM memory_rebuild_proposals p WHERE p.run_id=r.id))",
                    id_expression="r.id",
                ),
            ),
        )


def _query(table: str, condition: str, *, id_expression: str = "id") -> str:
    return f"""
    WITH tally AS (SELECT COUNT(*) AS n FROM {table} WHERE {condition}),
    sample AS (SELECT {id_expression} AS id FROM {table} WHERE {condition}
               ORDER BY {id_expression} LIMIT {_SAMPLE_LIMIT})
    SELECT tally.n, sample.id FROM tally LEFT JOIN sample ON 1=1
    """
