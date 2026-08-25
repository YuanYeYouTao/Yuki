"""Bridge a completed historical 0048 database to canonical-only 3.8.

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-26

The migration is deliberately fail-closed and has no downgrade. A production
rollback restores the pre-migration SQLite DB/WAL/SHM snapshot.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Final

from alembic import op
from sqlalchemy.engine import Connection

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FAILPOINT: Callable[[str], None] | None = None

_RETIRED_TABLES: Final[tuple[str, ...]] = (
    "conversation_rollup_emergency_overlays",
    "conversation_rollup_jobs",
    "conversation_rollups",
    "conversation_scopes",
    "people",
    "groups",
    "identity_cutover_runs",
    "identity_cutover_manifests",
    "identity_conflicts",
    "identity_backfill_runs",
    "identity_runtime_state",
)

_HISTORICAL_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "identity_runtime_state",
        "identity_cutover_manifests",
        "people",
        "groups",
        "conversation_scopes",
    }
)

_FINAL_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "persons",
        "identity_bindings",
        "spaces",
        "space_bindings",
        "presences",
        "canonical_conversations",
        "conversation_legacy_aliases",
        "canonical_event_receipts",
        "memory_facts",
    }
)

_LEGACY_EMPTY_TABLES: Final[tuple[str, ...]] = (
    "conversation_scopes",
    "conversation_rollups",
    "conversation_rollup_jobs",
    "conversation_rollup_emergency_overlays",
)

_DRAIN_STATUS: Final[dict[str, tuple[str, ...]]] = {
    "memory_jobs": ("pending", "processing"),
    "memory_reflection_jobs": ("pending", "processing"),
    "conversation_rollup_jobs": ("pending", "processing"),
    "canonical_conversation_rollup_jobs": ("pending", "processing"),
    "plugin_notification_outbox": ("processing",),
    "plugin_background_turn_jobs": ("processing",),
    "memory_dream_runs": ("running", "rolling_back"),
    "memory_dream_clusters": ("processing",),
    "memory_evidence_compaction_items": ("processing",),
    "memory_rebuild_runs": ("extracting", "committing"),
    "emoji_jobs": ("processing",),
    "relationship_jobs": ("processing",),
    "memory_embedding_jobs": ("processing",),
    "memory_self_reflection_runs": ("processing",),
    "memory_evidence_compaction_runs": ("running",),
    "memory_dream_operations": ("processing",),
    "memory_mutation_receipts": ("processing",),
    "automation_runs": ("running",),
    "automations": (),
}

_LEASE_STATUS: Final[dict[str, tuple[str, ...]]] = {
    "automations": ("active", "paused"),
    "plugin_notification_outbox": ("pending", "processing"),
    "plugin_background_turn_jobs": ("pending", "processing"),
}

_HISTORICAL_SCHEMA_DIGESTS: Final[frozenset[str]] = frozenset(
    {
        # Frozen, redacted 0048 migration fixture.
        "235100f1310f0362bdcfecd13124da7f7c729bfbfd760c6b9233e0128a9f8168",
        # Read-only digest of the deployed 0048 schema. Its only accepted DDL
        # differences are frozen in the migration tests; arbitrary schemas are
        # never accepted by column-set similarity.
        "11e87cc3e57199863be5ee6bfe8fb72ab90a070bc1253cf5475825fb6cc978b2",
    }
)
_FINAL_SCHEMA_DIGEST: Final[str] = (
    "4ef4a733b476e7dfa8dab29839733d50e46da8b6b4a956a27ae6babc37723cba"
)


class CanonicalBridgeError(RuntimeError):
    """A redacted precondition or integrity failure."""


def _trip(name: str) -> None:
    if _FAILPOINT is not None:
        _FAILPOINT(name)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _tables(connection: Connection) -> frozenset[str]:
    rows = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")
    return frozenset(str(row[0]) for row in rows)


def _columns(connection: Connection, table: str) -> frozenset[str]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({_quote(table)})")
    return frozenset(str(row[1]) for row in rows)


def _scalar(connection: Connection, statement: str, parameters: tuple[Any, ...] = ()) -> Any:
    return connection.exec_driver_sql(statement, parameters).scalar_one()


def _require_zero(
    connection: Connection,
    statement: str,
    *,
    category: str,
    parameters: tuple[Any, ...] = (),
) -> None:
    if int(_scalar(connection, statement, parameters)) != 0:
        raise CanonicalBridgeError(category)


def _require_historical_runtime(connection: Connection) -> None:
    rows = connection.exec_driver_sql(
        "SELECT id, state, cutover_id, source_fingerprint, completed_at "
        "FROM identity_runtime_state ORDER BY id"
    ).all()
    if len(rows) != 1:
        raise CanonicalBridgeError("state_mismatch")
    row = rows[0]
    if (
        int(row[0]) != 1
        or str(row[1]) != "v2"
        or row[2] is None
        or row[3] is None
        or row[4] is None
    ):
        raise CanonicalBridgeError("state_mismatch")
    fingerprint = str(row[3])
    _require_zero(
        connection,
        "SELECT COUNT(*) WHERE NOT EXISTS ("
        "SELECT 1 FROM identity_cutover_manifests WHERE fingerprint = ?)",
        parameters=(fingerprint,),
        category="state_mismatch",
    )
    _require_zero(
        connection,
        "SELECT COUNT(*) WHERE NOT EXISTS ("
        "SELECT 1 FROM identity_cutover_runs WHERE mode = 'apply' "
        "AND status = 'succeeded' AND source_fingerprint = ? "
        "AND finished_at IS NOT NULL)",
        parameters=(fingerprint,),
        category="state_mismatch",
    )
    _require_zero(
        connection,
        "SELECT COUNT(*) FROM identity_conflicts WHERE status = 'open'",
        category="identity_conflicts",
    )


def _require_drained(connection: Connection, tables: frozenset[str]) -> None:
    for table, statuses in _DRAIN_STATUS.items():
        if table not in tables:
            continue
        columns = _columns(connection, table)
        if statuses and "status" in columns:
            placeholders = ", ".join("?" for _ in statuses)
            _require_zero(
                connection,
                f"SELECT COUNT(*) FROM {_quote(table)} WHERE status IN ({placeholders})",
                parameters=statuses,
                category="lease_not_drained",
            )
        lease_columns = tuple(
            column
            for column in (
                "lease_until",
                "claimed_until",
                "lease_owner",
                "lease_token",
                "claimed_by",
            )
            if column in columns
        )
        if not lease_columns:
            continue
        lease_predicate = " OR ".join(f"{_quote(column)} IS NOT NULL" for column in lease_columns)
        lease_statuses = _LEASE_STATUS.get(table, statuses)
        if lease_statuses and "status" in columns:
            placeholders = ", ".join("?" for _ in lease_statuses)
            predicate = f"status IN ({placeholders}) AND ({lease_predicate})"
            parameters = lease_statuses
        else:
            predicate = lease_predicate
            parameters = ()
        _require_zero(
            connection,
            f"SELECT COUNT(*) FROM {_quote(table)} WHERE {predicate}",
            parameters=parameters,
            category="lease_not_drained",
        )


def _require_empty_legacy_carriers(connection: Connection) -> None:
    for table in _LEGACY_EMPTY_TABLES:
        _require_zero(
            connection,
            f"SELECT COUNT(*) FROM {_quote(table)}",
            category="legacy_conversation_state_present",
        )


def _require_canonical_ownership(connection: Connection) -> None:
    checks = (
        (
            "SELECT COUNT(*) FROM people p LEFT JOIN identity_bindings b "
            "ON b.person_id = p.canonical_person_id AND b.platform = 'qq' "
            "AND b.external_account_id = p.user_id "
            "WHERE (p.is_bot = 0 AND (p.canonical_person_id IS NULL OR b.id IS NULL)) "
            "OR (p.is_bot = 1 AND p.canonical_person_id IS NOT NULL)",
            "legacy_identity_forbidden",
        ),
        (
            "SELECT COUNT(*) FROM groups g LEFT JOIN space_bindings b "
            "ON b.space_id = g.canonical_space_id AND b.platform = 'qq' "
            "AND b.external_space_id = g.group_id "
            "WHERE g.canonical_space_id IS NULL OR b.id IS NULL",
            "legacy_identity_forbidden",
        ),
        (
            "SELECT COUNT(*) FROM chat_events WHERE canonical_event_id IS NULL "
            "OR canonical_conversation_id IS NULL OR author_kind IS NULL "
            "OR suppression_status IS NULL",
            "canonical_event_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM (SELECT canonical_event_id FROM chat_events "
            "GROUP BY canonical_event_id HAVING "
            "SUM(CASE WHEN suppression_status = 'keeper' THEN 1 ELSE 0 END) <> 1)",
            "canonical_event_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM canonical_event_receipts r WHERE NOT EXISTS ("
            "SELECT 1 FROM chat_events e WHERE "
            "e.canonical_event_id = r.canonical_event_id "
            "AND e.suppression_status = 'keeper')",
            "canonical_event_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM conversation_scopes s "
            "LEFT JOIN conversation_legacy_aliases a "
            "ON a.scope_key = s.scope_key "
            "AND a.conversation_id = s.canonical_conversation_id "
            "WHERE s.canonical_conversation_id IS NULL OR a.id IS NULL",
            "canonical_conversation_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM person_aliases WHERE canonical_person_id IS NULL "
            "OR (group_scope <> '' AND canonical_space_id IS NULL) "
            "OR (group_scope = '' AND canonical_space_id IS NOT NULL)",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM memberships WHERE canonical_person_id IS NULL "
            "OR canonical_space_id IS NULL",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM person_relationships WHERE canonical_person_id IS NULL",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM relationship_events WHERE canonical_person_id IS NULL",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM relationship_jobs WHERE canonical_person_id IS NULL",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM person_time_settings WHERE canonical_person_id IS NULL",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM person_speech_preferences WHERE canonical_person_id IS NULL",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM runtime_config_overrides WHERE "
            "(scope_type = 'global' AND (canonical_person_id IS NOT NULL "
            "OR canonical_space_id IS NOT NULL)) OR "
            "(scope_type = 'user' AND (canonical_person_id IS NULL "
            "OR canonical_space_id IS NOT NULL)) OR "
            "(scope_type = 'group' AND (canonical_space_id IS NULL "
            "OR canonical_person_id IS NOT NULL))",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM memory_facts WHERE "
            "(scope_type = 'person' AND (canonical_subject_person_id IS NULL "
            "OR canonical_subject_space_id IS NOT NULL)) OR "
            "(scope_type = 'person_group' AND (canonical_subject_person_id IS NULL "
            "OR canonical_subject_space_id IS NULL)) OR "
            "(scope_type = 'group' AND (canonical_subject_person_id IS NOT NULL "
            "OR canonical_subject_space_id IS NULL)) OR "
            "(scope_type = 'self' AND (canonical_subject_person_id IS NOT NULL "
            "OR canonical_subject_space_id IS NOT NULL)) OR "
            "(scope_type NOT IN ('person','person_group','group','self'))",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM memory_facts WHERE "
            "(scope_type <> 'self' AND (canonical_visibility_person_id IS NOT NULL "
            "OR canonical_visibility_space_id IS NOT NULL)) OR "
            "(scope_type = 'self' AND ((visibility_type = 'global' "
            "AND (canonical_visibility_person_id IS NOT NULL "
            "OR canonical_visibility_space_id IS NOT NULL)) OR "
            "(visibility_type = 'private' AND (canonical_visibility_person_id IS NULL "
            "OR canonical_visibility_space_id IS NOT NULL)) OR "
            "(visibility_type = 'group' AND (canonical_visibility_person_id IS NOT NULL "
            "OR canonical_visibility_space_id IS NULL)) OR "
            "visibility_type IS NULL OR visibility_type NOT IN ('global','private','group')))",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM memory_self_reflection_states WHERE "
            "(canonical_person_id IS NULL) = (canonical_space_id IS NULL)",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM memory_tool_receipts WHERE "
            "(canonical_person_id IS NULL) = (canonical_space_id IS NULL)",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM memory_jobs WHERE "
            "(canonical_person_id IS NULL) = (canonical_space_id IS NULL)",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM memory_self_reflection_runs WHERE "
            "(canonical_person_id IS NULL) = (canonical_space_id IS NULL)",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM memory_dream_clusters WHERE "
            "(canonical_visibility_person_id IS NOT NULL "
            "AND canonical_visibility_space_id IS NOT NULL) OR "
            "((canonical_subject_person_id IS NOT NULL "
            "OR canonical_subject_space_id IS NOT NULL) AND "
            "(canonical_visibility_person_id IS NOT NULL "
            "OR canonical_visibility_space_id IS NOT NULL))",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM automations WHERE "
            "(status IN ('active','paused') AND (canonical_creator_person_id IS NULL "
            "OR (canonical_target_person_id IS NULL) = "
            "(canonical_target_space_id IS NULL))) OR "
            "(status IN ('completed','cancelled','failed','blocked') AND "
            "canonical_target_person_id IS NOT NULL "
            "AND canonical_target_space_id IS NOT NULL) OR "
            "status NOT IN ('active','paused','completed','cancelled','failed','blocked')",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM plugin_config_values WHERE "
            "(scope_type = 'global' AND (canonical_person_id IS NOT NULL "
            "OR canonical_space_id IS NOT NULL)) OR "
            "(scope_type = 'user' AND (canonical_person_id IS NULL "
            "OR canonical_space_id IS NOT NULL)) OR "
            "(scope_type = 'group' AND (canonical_person_id IS NOT NULL "
            "OR canonical_space_id IS NULL)) OR "
            "scope_type NOT IN ('global','user','group')",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM plugin_state WHERE subject_user_id IS NOT NULL "
            "AND canonical_person_id IS NULL",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM plugin_agent_sessions WHERE "
            "(scope_type = 'plugin' AND (canonical_owner_person_id IS NOT NULL "
            "OR canonical_space_id IS NOT NULL)) OR "
            "(scope_type = 'user' AND (canonical_owner_person_id IS NULL "
            "OR canonical_space_id IS NOT NULL)) OR "
            "(scope_type = 'group' AND canonical_space_id IS NULL) OR "
            "scope_type NOT IN ('plugin','user','group')",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM plugin_agent_messages WHERE "
            "(canonical_sender_person_id IS NOT NULL AND role <> 'user') "
            "OR role NOT IN ('user','assistant','tool')",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM plugin_background_target_grants WHERE "
            "(canonical_target_person_id IS NOT NULL "
            "AND canonical_target_space_id IS NOT NULL) OR "
            "(canonical_target_person_id IS NOT NULL AND target_type <> 'private') OR "
            "(canonical_target_space_id IS NOT NULL AND target_type <> 'group') OR "
            "(enabled = 1 AND (canonical_created_by_person_id IS NULL "
            "OR canonical_presence_id IS NULL OR "
            "(canonical_target_person_id IS NULL) = "
            "(canonical_target_space_id IS NULL))) OR "
            "target_type NOT IN ('private','group')",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM plugin_notification_outbox WHERE "
            "(canonical_target_person_id IS NOT NULL "
            "AND canonical_target_space_id IS NOT NULL) OR "
            "(canonical_target_person_id IS NOT NULL AND target_type <> 'private') OR "
            "(canonical_target_space_id IS NOT NULL AND target_type <> 'group') OR "
            "(status IN ('pending','processing') AND "
            "(canonical_conversation_id IS NULL OR canonical_presence_id IS NULL OR "
            "(canonical_target_person_id IS NULL) = "
            "(canonical_target_space_id IS NULL) OR NOT EXISTS ("
            "SELECT 1 FROM canonical_conversations c WHERE "
            "c.id = plugin_notification_outbox.canonical_conversation_id AND (("
            "plugin_notification_outbox.target_type = 'private' "
            "AND c.kind = 'private' AND c.person_id = "
            "plugin_notification_outbox.canonical_target_person_id) OR ("
            "plugin_notification_outbox.target_type = 'group' "
            "AND c.kind = 'space' AND c.space_id = "
            "plugin_notification_outbox.canonical_target_space_id))))) OR "
            "target_type NOT IN ('private','group')",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM plugin_background_turn_jobs WHERE "
            "(canonical_target_person_id IS NOT NULL "
            "AND canonical_target_space_id IS NOT NULL) OR "
            "(canonical_target_person_id IS NOT NULL AND target_type <> 'private') OR "
            "(canonical_target_space_id IS NOT NULL AND target_type <> 'group') OR "
            "(status IN ('pending','processing') AND "
            "(canonical_conversation_id IS NULL OR canonical_presence_id IS NULL OR "
            "(canonical_target_person_id IS NULL) = "
            "(canonical_target_space_id IS NULL) OR NOT EXISTS ("
            "SELECT 1 FROM canonical_conversations c WHERE "
            "c.id = plugin_background_turn_jobs.canonical_conversation_id AND (("
            "plugin_background_turn_jobs.target_type = 'private' "
            "AND c.kind = 'private' AND c.person_id = "
            "plugin_background_turn_jobs.canonical_target_person_id) OR ("
            "plugin_background_turn_jobs.target_type = 'group' "
            "AND c.kind = 'space' AND c.space_id = "
            "plugin_background_turn_jobs.canonical_target_space_id))))) OR "
            "target_type NOT IN ('private','group')",
            "canonical_owner_incomplete",
        ),
        (
            "SELECT COUNT(*) FROM emoji_scope_states WHERE "
            "(scope_type = 'global' AND canonical_space_id IS NOT NULL) OR "
            "(scope_type = 'group' AND canonical_space_id IS NULL) "
            "OR scope_type NOT IN ('global','group')",
            "canonical_owner_incomplete",
        ),
    )
    for statement, category in checks:
        _require_zero(connection, statement, category=category)

    collision_checks = (
        "SELECT COUNT(*) FROM (SELECT canonical_person_id FROM person_relationships "
        "GROUP BY canonical_person_id HAVING COUNT(*) > 1)",
        "SELECT COUNT(*) FROM (SELECT canonical_person_id FROM person_time_settings "
        "GROUP BY canonical_person_id HAVING COUNT(*) > 1)",
        "SELECT COUNT(*) FROM (SELECT canonical_person_id FROM person_speech_preferences "
        "GROUP BY canonical_person_id HAVING COUNT(*) > 1)",
        "SELECT COUNT(*) FROM (SELECT config_key, scope_type, canonical_person_id, "
        "canonical_space_id FROM runtime_config_overrides GROUP BY config_key, scope_type, "
        "canonical_person_id, canonical_space_id HAVING COUNT(*) > 1)",
        "SELECT COUNT(*) FROM (SELECT plugin_id, scope_type, canonical_person_id, "
        "canonical_space_id, key FROM plugin_config_values GROUP BY plugin_id, scope_type, "
        "canonical_person_id, canonical_space_id, key HAVING COUNT(*) > 1)",
        "SELECT COUNT(*) FROM (SELECT plugin_id, target_type, canonical_target_person_id, "
        "canonical_target_space_id FROM plugin_background_target_grants GROUP BY plugin_id, "
        "target_type, canonical_target_person_id, canonical_target_space_id "
        "HAVING COUNT(*) > 1) WHERE canonical_target_person_id IS NOT NULL "
        "OR canonical_target_space_id IS NOT NULL",
        "SELECT COUNT(*) FROM (SELECT emoji_id, scope_type, canonical_space_id "
        "FROM emoji_scope_states GROUP BY emoji_id, scope_type, canonical_space_id "
        "HAVING COUNT(*) > 1)",
        "SELECT COUNT(*) FROM (SELECT conversation_id FROM conversation_legacy_aliases "
        "GROUP BY conversation_id HAVING SUM(CASE WHEN is_primary = 1 THEN 1 ELSE 0 END) <> 1)",
    )
    for statement in collision_checks:
        _require_zero(connection, statement, category="canonical_owner_conflict")


def _require_legacy_crosswalks(connection: Connection) -> None:
    """Prove every retiring raw QQ owner resolves to the stored canonical owner."""

    checks = (
        "SELECT COUNT(*) FROM person_aliases x WHERE (NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.user_id "
        "AND b.person_id = x.canonical_person_id) AND NOT ("
        "x.group_scope = '' AND x.canonical_space_id IS NULL "
        "AND x.user_id = x.canonical_person_id "
        "AND EXISTS (SELECT 1 FROM persons p WHERE p.id = x.canonical_person_id) "
        "AND EXISTS (SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.person_id = x.canonical_person_id AND b.status = 'active'))) OR "
        "(x.group_scope <> '' AND NOT EXISTS (SELECT 1 FROM space_bindings b "
        "WHERE b.platform = 'qq' AND b.external_space_id = x.group_scope "
        "AND b.space_id = x.canonical_space_id))",
        "SELECT COUNT(*) FROM memberships x WHERE NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.user_id "
        "AND b.person_id = x.canonical_person_id) OR NOT EXISTS ("
        "SELECT 1 FROM space_bindings b WHERE b.platform = 'qq' "
        "AND b.external_space_id = x.group_id "
        "AND b.space_id = x.canonical_space_id)",
        "SELECT COUNT(*) FROM person_relationships x WHERE NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.user_id "
        "AND b.person_id = x.canonical_person_id)",
        "SELECT COUNT(*) FROM relationship_events x WHERE NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.user_id "
        "AND b.person_id = x.canonical_person_id)",
        "SELECT COUNT(*) FROM relationship_jobs x WHERE NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.user_id "
        "AND b.person_id = x.canonical_person_id)",
        "SELECT COUNT(*) FROM person_time_settings x WHERE NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.user_id "
        "AND b.person_id = x.canonical_person_id)",
        "SELECT COUNT(*) FROM person_speech_preferences x WHERE NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.user_id "
        "AND b.person_id = x.canonical_person_id)",
        "SELECT COUNT(*) FROM runtime_config_overrides x WHERE "
        "(x.scope_type = 'user' AND NOT EXISTS (SELECT 1 FROM identity_bindings b "
        "WHERE b.platform = 'qq' AND b.external_account_id = x.scope_id "
        "AND b.person_id = x.canonical_person_id)) OR "
        "(x.scope_type = 'group' AND NOT EXISTS (SELECT 1 FROM space_bindings b "
        "WHERE b.platform = 'qq' AND b.external_space_id = x.scope_id "
        "AND b.space_id = x.canonical_space_id))",
        "SELECT COUNT(*) FROM memory_facts x WHERE "
        "(x.scope_type IN ('person','person_group') AND NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.subject_user_id "
        "AND b.person_id = x.canonical_subject_person_id)) OR "
        "(x.scope_type IN ('group','person_group') AND NOT EXISTS ("
        "SELECT 1 FROM space_bindings b WHERE b.platform = 'qq' "
        "AND b.external_space_id = x.group_id "
        "AND b.space_id = x.canonical_subject_space_id)) OR "
        "(x.scope_type = 'self' AND x.visibility_type = 'private' AND NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.visibility_user_id "
        "AND b.person_id = x.canonical_visibility_person_id)) OR "
        "(x.scope_type = 'self' AND x.visibility_type = 'group' AND NOT EXISTS ("
        "SELECT 1 FROM space_bindings b WHERE b.platform = 'qq' "
        "AND b.external_space_id = x.visibility_group_id "
        "AND b.space_id = x.canonical_visibility_space_id))",
        "SELECT COUNT(*) FROM memory_self_reflection_states x WHERE "
        "(x.scope_type = 'private' AND NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.private_peer_user_id "
        "AND b.person_id = x.canonical_person_id)) OR "
        "(x.scope_type = 'group' AND NOT EXISTS ("
        "SELECT 1 FROM space_bindings b WHERE b.platform = 'qq' "
        "AND b.external_space_id = x.group_id "
        "AND b.space_id = x.canonical_space_id))",
        "SELECT COUNT(*) FROM plugin_config_values x WHERE "
        "(x.scope_type = 'user' AND NOT EXISTS (SELECT 1 FROM identity_bindings b "
        "WHERE b.platform = 'qq' AND b.external_account_id = x.scope_id "
        "AND b.person_id = x.canonical_person_id)) OR "
        "(x.scope_type = 'group' AND NOT EXISTS (SELECT 1 FROM space_bindings b "
        "WHERE b.platform = 'qq' AND b.external_space_id = x.scope_id "
        "AND b.space_id = x.canonical_space_id))",
        "SELECT COUNT(*) FROM plugin_state x WHERE "
        "x.subject_user_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.subject_user_id "
        "AND b.person_id = x.canonical_person_id)",
        "SELECT COUNT(*) FROM plugin_agent_sessions x WHERE "
        "(x.scope_type = 'user' AND NOT EXISTS (SELECT 1 FROM identity_bindings b "
        "WHERE b.platform = 'qq' AND b.external_account_id = x.owner_user_id "
        "AND b.person_id = x.canonical_owner_person_id)) OR "
        "(x.scope_type = 'group' AND NOT EXISTS (SELECT 1 FROM space_bindings b "
        "WHERE b.platform = 'qq' AND b.external_space_id = x.scope_id "
        "AND b.space_id = x.canonical_space_id)) OR "
        "(x.scope_type = 'group' AND x.owner_user_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.owner_user_id "
        "AND b.person_id = x.canonical_owner_person_id))",
        "SELECT COUNT(*) FROM plugin_agent_messages x WHERE "
        "(x.canonical_sender_person_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.sender_user_id "
        "AND b.person_id = x.canonical_sender_person_id))",
        "SELECT COUNT(*) FROM plugin_background_target_grants x WHERE "
        "(x.canonical_target_person_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.target_id "
        "AND b.person_id = x.canonical_target_person_id)) OR "
        "(x.canonical_target_space_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM space_bindings b WHERE b.platform = 'qq' "
        "AND b.external_space_id = x.target_id "
        "AND b.space_id = x.canonical_target_space_id)) OR "
        "(x.canonical_created_by_person_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.created_by_user_id "
        "AND b.person_id = x.canonical_created_by_person_id)) OR "
        "(x.canonical_presence_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM presences p WHERE p.platform = 'qq' "
        "AND p.external_account_id = x.bot_user_id "
        "AND p.id = x.canonical_presence_id))",
        "SELECT COUNT(*) FROM plugin_notification_outbox x WHERE "
        "(x.canonical_target_person_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.target_id "
        "AND b.person_id = x.canonical_target_person_id)) OR "
        "(x.canonical_target_space_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM space_bindings b WHERE b.platform = 'qq' "
        "AND b.external_space_id = x.target_id "
        "AND b.space_id = x.canonical_target_space_id)) OR "
        "(x.canonical_presence_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM presences p WHERE p.platform = 'qq' "
        "AND p.external_account_id = x.bot_user_id "
        "AND p.id = x.canonical_presence_id))",
        "SELECT COUNT(*) FROM plugin_background_turn_jobs x WHERE "
        "(x.canonical_target_person_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.target_id "
        "AND b.person_id = x.canonical_target_person_id)) OR "
        "(x.canonical_target_space_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM space_bindings b WHERE b.platform = 'qq' "
        "AND b.external_space_id = x.target_id "
        "AND b.space_id = x.canonical_target_space_id)) OR "
        "(x.canonical_presence_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM presences p WHERE p.platform = 'qq' "
        "AND p.external_account_id = x.bot_user_id "
        "AND p.id = x.canonical_presence_id))",
        "SELECT COUNT(*) FROM automations x WHERE "
        "(x.canonical_creator_person_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM identity_bindings b WHERE b.platform = 'qq' "
        "AND b.external_account_id = x.creator_user_id "
        "AND b.person_id = x.canonical_creator_person_id)) OR "
        "(x.canonical_presence_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM presences p WHERE p.platform = 'qq' "
        "AND p.external_account_id = x.bot_user_id "
        "AND p.id = x.canonical_presence_id))",
        "SELECT COUNT(*) FROM emoji_scope_states x WHERE "
        "x.scope_type = 'group' AND NOT EXISTS (SELECT 1 FROM space_bindings b "
        "WHERE b.platform = 'qq' AND b.external_space_id = x.scope_id "
        "AND b.space_id = x.canonical_space_id)",
    )
    for statement in checks:
        _require_zero(connection, statement, category="canonical_crosswalk_mismatch")


def _primary_key_columns(connection: Connection, table: str) -> tuple[str, ...]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({_quote(table)})").all()
    return tuple(
        str(row[1]) for row in sorted(rows, key=lambda item: int(item[5])) if int(row[5]) > 0
    )


def _rows_digest(rows: Iterable[Any]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(
            json.dumps(tuple(row), ensure_ascii=True, separators=(",", ":"), default=str).encode()
        )
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _semantic_alias_summary(connection: Connection) -> tuple[int, str]:
    return _rows_digest(
        connection.exec_driver_sql(
            "SELECT canonical_person_id, COALESCE(canonical_space_id, ''), alias, "
            "MIN(alias_type), MIN(first_seen_at), MAX(last_seen_at) "
            "FROM person_aliases GROUP BY canonical_person_id, canonical_space_id, alias "
            "ORDER BY canonical_person_id, COALESCE(canonical_space_id, ''), alias"
        )
    )


def _semantic_membership_summary(connection: Connection) -> tuple[int, str]:
    return _rows_digest(
        connection.exec_driver_sql(
            "SELECT m.canonical_person_id, m.canonical_space_id, "
            "COALESCE((SELECT m2.group_card FROM memberships m2 "
            "WHERE m2.canonical_person_id = m.canonical_person_id "
            "AND m2.canonical_space_id = m.canonical_space_id "
            "ORDER BY (m2.group_card <> '') DESC, m2.last_seen_at DESC, "
            "m2.group_card, m2.first_seen_at LIMIT 1), ''), "
            "MIN(m.first_seen_at), MAX(m.last_seen_at) FROM memberships m "
            "GROUP BY m.canonical_person_id, m.canonical_space_id "
            "ORDER BY m.canonical_person_id, m.canonical_space_id"
        )
    )


def _content_summary(
    connection: Connection,
    tables: Iterable[str],
) -> dict[str, tuple[int, str]]:
    summary: dict[str, tuple[int, str]] = {}
    for table in sorted(tables):
        if table.startswith(("chat_events_fts", "memory_facts_fts")):
            continue
        if table == "person_aliases":
            summary[table] = _semantic_alias_summary(connection)
            continue
        if table == "memberships":
            summary[table] = _semantic_membership_summary(connection)
            continue
        selected = _COPY_COLUMNS.get(table, tuple(sorted(_columns(connection, table))))
        if not selected:
            continue
        primary_key = _primary_key_columns(connection, table)
        order_columns = primary_key if set(primary_key).issubset(selected) else selected
        projection = ", ".join(_quote(column) for column in selected)
        order = ", ".join(_quote(column) for column in order_columns)
        summary[table] = _rows_digest(
            connection.exec_driver_sql(f"SELECT {projection} FROM {_quote(table)} ORDER BY {order}")
        )
    return summary


def _merge_legacy_identity_metadata(connection: Connection) -> None:
    connection.exec_driver_sql("ALTER TABLE identity_bindings ADD COLUMN first_seen_at DATETIME")
    connection.exec_driver_sql("ALTER TABLE identity_bindings ADD COLUMN last_seen_at DATETIME")
    connection.exec_driver_sql("ALTER TABLE space_bindings ADD COLUMN first_seen_at DATETIME")
    connection.exec_driver_sql("ALTER TABLE space_bindings ADD COLUMN last_seen_at DATETIME")

    connection.exec_driver_sql(
        "UPDATE identity_bindings SET "
        "first_seen_at = COALESCE((SELECT p.first_seen_at FROM people p "
        "WHERE p.user_id = identity_bindings.external_account_id "
        "AND identity_bindings.platform = 'qq'), created_at), "
        "last_seen_at = COALESCE((SELECT p.last_seen_at FROM people p "
        "WHERE p.user_id = identity_bindings.external_account_id "
        "AND identity_bindings.platform = 'qq'), updated_at)"
    )
    connection.exec_driver_sql(
        "UPDATE space_bindings SET "
        "first_seen_at = COALESCE((SELECT g.first_seen_at FROM groups g "
        "WHERE g.group_id = space_bindings.external_space_id "
        "AND space_bindings.platform = 'qq'), created_at), "
        "last_seen_at = COALESCE((SELECT g.last_seen_at FROM groups g "
        "WHERE g.group_id = space_bindings.external_space_id "
        "AND space_bindings.platform = 'qq'), updated_at)"
    )
    connection.exec_driver_sql(
        "UPDATE identity_bindings SET display_name = COALESCE(NULLIF(display_name, ''), "
        "(SELECT p.nickname FROM people p WHERE p.user_id = external_account_id "
        "AND platform = 'qq'), '')"
    )
    connection.exec_driver_sql(
        "UPDATE space_bindings SET display_name = COALESCE(NULLIF(display_name, ''), "
        "(SELECT g.name FROM groups g WHERE g.group_id = external_space_id "
        "AND platform = 'qq'), '')"
    )
    connection.exec_driver_sql(
        "UPDATE spaces SET name = COALESCE(NULLIF(name, ''), (SELECT g.name FROM groups g "
        "WHERE g.canonical_space_id = spaces.id ORDER BY g.last_seen_at DESC LIMIT 1), '')"
    )
    connection.exec_driver_sql(
        "UPDATE persons SET created_at = MIN(created_at, COALESCE((SELECT MIN(p.first_seen_at) "
        "FROM people p WHERE p.canonical_person_id = persons.id), created_at)), "
        "updated_at = MAX(updated_at, COALESCE((SELECT MAX(p.last_seen_at) FROM people p "
        "WHERE p.canonical_person_id = persons.id), updated_at))"
    )
    connection.exec_driver_sql(
        "UPDATE spaces SET created_at = MIN(created_at, COALESCE((SELECT MIN(g.first_seen_at) "
        "FROM groups g WHERE g.canonical_space_id = spaces.id), created_at)), "
        "updated_at = MAX(updated_at, "
        "COALESCE((SELECT MAX(g.last_seen_at) FROM groups g "
        "WHERE g.canonical_space_id = spaces.id), updated_at), "
        "COALESCE((SELECT MAX(g.updated_at) FROM groups g "
        "WHERE g.canonical_space_id = spaces.id), updated_at))"
    )
    _require_zero(
        connection,
        "SELECT COUNT(*) FROM identity_bindings WHERE first_seen_at IS NULL "
        "OR last_seen_at IS NULL OR first_seen_at > last_seen_at",
        category="identity_metadata_conflict",
    )
    _require_zero(
        connection,
        "SELECT COUNT(*) FROM space_bindings WHERE first_seen_at IS NULL "
        "OR last_seen_at IS NULL OR first_seen_at > last_seen_at",
        category="identity_metadata_conflict",
    )
    # Preserve a distinct historical nickname as an ordinary global alias.
    connection.exec_driver_sql(
        "INSERT OR IGNORE INTO person_aliases "
        "(user_id, group_scope, alias, alias_type, first_seen_at, last_seen_at, "
        "canonical_person_id, canonical_space_id) "
        "SELECT p.user_id, '', p.nickname, 'nickname', p.first_seen_at, p.last_seen_at, "
        "p.canonical_person_id, NULL FROM people p "
        "JOIN identity_bindings b ON b.platform = 'qq' "
        "AND b.external_account_id = p.user_id AND b.person_id = p.canonical_person_id "
        "WHERE p.is_bot = 0 AND p.nickname <> '' AND p.nickname <> b.display_name"
    )
    # Preserve every observed group card before many Bindings collapse into one Membership.
    connection.exec_driver_sql(
        "INSERT OR IGNORE INTO person_aliases "
        "(user_id, group_scope, alias, alias_type, first_seen_at, last_seen_at, "
        "canonical_person_id, canonical_space_id) "
        "SELECT m.user_id, m.group_id, m.group_card, 'group_card', "
        "m.first_seen_at, m.last_seen_at, m.canonical_person_id, m.canonical_space_id "
        "FROM memberships m WHERE m.group_card <> ''"
    )
    _require_zero(
        connection,
        "SELECT COUNT(*) FROM (SELECT canonical_person_id, canonical_space_id, alias "
        "FROM person_aliases GROUP BY canonical_person_id, canonical_space_id, alias "
        "HAVING COUNT(DISTINCT alias_type) > 1)",
        category="canonical_owner_conflict",
    )


def _drop_retired_tables(connection: Connection) -> None:
    for table in _RETIRED_TABLES:
        connection.exec_driver_sql(f"DROP TABLE {_quote(table)}")


def _normalize_schema_sql(statement: str) -> str:
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(statement):
        character = statement[index]
        if quote == "'":
            output.append(character)
            if character == "'":
                if index + 1 < len(statement) and statement[index + 1] == "'":
                    output.append("'")
                    index += 1
                else:
                    quote = None
            index += 1
            continue
        if quote is not None:
            if (quote == "]" and character == "]") or character == quote:
                quote = None
            elif not character.isspace():
                output.append(character.lower())
            index += 1
            continue
        if character == "'":
            quote = "'"
            output.append(character)
        elif character == '"':
            quote = '"'
        elif character == "`":
            quote = "`"
        elif character == "[":
            quote = "]"
        elif not character.isspace():
            output.append(character.lower())
        index += 1
    return "".join(output)


def _split_table_components(body: str) -> tuple[str, ...]:
    components: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(body):
        character = body[index]
        if quote == "'":
            current.append(character)
            if character == "'":
                if index + 1 < len(body) and body[index + 1] == "'":
                    current.append("'")
                    index += 1
                else:
                    quote = None
        elif quote is not None:
            current.append(character)
            if (quote == "]" and character == "]") or character == quote:
                quote = None
        elif character in {"'", '"', "`"}:
            quote = character
            current.append(character)
        elif character == "[":
            quote = "]"
            current.append(character)
        elif character == "(":
            depth += 1
            current.append(character)
        elif character == ")":
            depth -= 1
            current.append(character)
        elif character == "," and depth == 0:
            components.append("".join(current))
            current = []
        else:
            current.append(character)
        index += 1
    components.append("".join(current))
    return tuple(components)


def _canonical_schema_sql(
    connection: Connection,
    *,
    kind: str,
    table: str,
    statement: str,
) -> str:
    normalized = _normalize_schema_sql(statement)
    if kind != "table" or normalized.startswith("createvirtualtable"):
        return normalized
    opening = statement.find("(")
    closing = statement.rfind(")")
    if opening < 0 or closing <= opening:
        return normalized
    components = _split_table_components(statement[opening + 1 : closing])
    column_count = len(_columns(connection, table))
    if len(components) < column_count:
        return normalized
    columns = tuple(_normalize_schema_sql(item) for item in components[:column_count])
    constraints = tuple(sorted(_normalize_schema_sql(item) for item in components[column_count:]))
    prefix = _normalize_schema_sql(statement[:opening])
    suffix = _normalize_schema_sql(statement[closing + 1 :])
    return f"{prefix}({','.join((*columns, *constraints))}){suffix}"


def _schema_digest(connection: Connection) -> str:
    rows = connection.exec_driver_sql(
        "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    )
    lines: list[str] = []
    for row in rows:
        kind, name, table, statement = (str(value) for value in row)
        canonical = _canonical_schema_sql(
            connection,
            kind=kind,
            table=table,
            statement=statement,
        )
        lines.append(f"{kind.lower()}|{name.lower()}|{table.lower()}|{canonical}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _validate_fts(connection: Connection) -> None:
    try:
        connection.exec_driver_sql(
            "INSERT INTO chat_events_fts(chat_events_fts, rank) VALUES ('integrity-check', 1)"
        )
        connection.exec_driver_sql(
            "INSERT INTO memory_facts_fts(memory_facts_fts, rank) VALUES ('integrity-check', 1)"
        )
    except Exception:
        raise CanonicalBridgeError("fts_integrity_check") from None


def _validate_database(connection: Connection, tables: frozenset[str]) -> None:
    if not _FINAL_MARKERS.issubset(tables):
        raise CanonicalBridgeError("incomplete_schema")
    if tables & frozenset(_RETIRED_TABLES):
        raise CanonicalBridgeError("retired_schema_present")
    if _schema_digest(connection) != _FINAL_SCHEMA_DIGEST:
        raise CanonicalBridgeError("schema_manifest_mismatch")
    foreign_key = connection.exec_driver_sql("PRAGMA foreign_key_check").first()
    if foreign_key is not None:
        raise CanonicalBridgeError("foreign_key_check")
    quick_check = connection.exec_driver_sql("PRAGMA quick_check").scalar_one()
    if str(quick_check) != "ok":
        raise CanonicalBridgeError("integrity_check")
    _validate_fts(connection)


def _upgrade_historical_0048(connection: Connection, tables: frozenset[str]) -> None:
    if _schema_digest(connection) not in _HISTORICAL_SCHEMA_DIGESTS:
        raise CanonicalBridgeError("historical_schema_manifest_mismatch")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise CanonicalBridgeError("foreign_key_check")
    _require_historical_runtime(connection)
    _require_drained(connection, tables)
    _require_empty_legacy_carriers(connection)
    _require_canonical_ownership(connection)
    _require_legacy_crosswalks(connection)
    _trip("after_preflight")

    retained = (
        frozenset(table for table in tables if not table.startswith("sqlite_"))
        - frozenset(_RETIRED_TABLES)
        - {
            "alembic_version",
        }
    )
    _merge_legacy_identity_metadata(connection)
    _trip("after_identity_merge")
    before = _content_summary(connection, retained)

    # Table rebuilds that remove legacy owner columns and tighten canonical
    # constraints are frozen below once the final 3.8 ORM shape is generated.
    _rebuild_canonical_tables(connection)
    _trip("after_table_rebuild")
    _drop_retired_tables(connection)
    _trip("after_retired_drop")

    after_tables = _tables(connection)
    after = _content_summary(connection, retained)
    if before != after:
        raise CanonicalBridgeError("business_summary_mismatch")
    _validate_database(connection, after_tables)


_REBUILD_TABLES: Final[tuple[str, ...]] = (
    "identity_bindings",
    "space_bindings",
    "person_aliases",
    "memberships",
    "person_relationships",
    "relationship_events",
    "relationship_jobs",
    "person_time_settings",
    "person_speech_preferences",
    "runtime_config_overrides",
    "chat_events",
    "memory_facts",
    "memory_tool_receipts",
    "memory_self_reflection_states",
    "memory_self_reflection_runs",
    "memory_jobs",
    "memory_dream_clusters",
    "automations",
    "plugin_config_values",
    "plugin_state",
    "plugin_agent_sessions",
    "plugin_agent_messages",
    "plugin_background_target_grants",
    "plugin_notification_outbox",
    "plugin_background_turn_jobs",
    "emoji_scope_states",
)

_TABLE_DDL: Final[dict[str, str]] = {
    "identity_bindings": "CREATE TABLE identity_bindings (\n"
    "\tid VARCHAR(36) NOT NULL, \n"
    "\tperson_id VARCHAR(36) NOT NULL, \n"
    "\tplatform VARCHAR(32) NOT NULL, \n"
    "\texternal_account_id VARCHAR(255) NOT NULL, \n"
    "\tdisplay_name VARCHAR(128) NOT NULL, \n"
    "\tstatus VARCHAR(16) NOT NULL, \n"
    "\trevision INTEGER NOT NULL, \n"
    "\tfirst_seen_at DATETIME NOT NULL, \n"
    "\tlast_seen_at DATETIME NOT NULL, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT uq_identity_bindings_platform_account UNIQUE (platform, "
    "external_account_id), \n"
    "\tCONSTRAINT ck_identity_bindings_id CHECK (length(id) = 36 AND id = "
    "lower(id) AND id GLOB "
    "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
    "\tCONSTRAINT ck_identity_bindings_person_id CHECK (length(person_id) = 36 "
    "AND person_id = lower(person_id) AND person_id GLOB "
    "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
    "\tCONSTRAINT ck_identity_bindings_platform CHECK (length(platform) > 0 AND "
    "length(platform) <= 32 AND platform = lower(platform) AND platform = "
    "trim(platform)), \n"
    "\tCONSTRAINT ck_identity_bindings_external_account_id CHECK "
    "(length(external_account_id) > 0 AND length(external_account_id) <= 255 AND "
    "external_account_id = trim(external_account_id)), \n"
    "\tCONSTRAINT ck_identity_bindings_status CHECK (status IN ('active', "
    "'disabled')), \n"
    "\tCONSTRAINT ck_identity_bindings_revision CHECK (revision >= 1), \n"
    "\tCONSTRAINT ck_identity_bindings_seen_range CHECK (first_seen_at <= "
    "last_seen_at), \n"
    "\tFOREIGN KEY(person_id) REFERENCES persons (id) ON DELETE RESTRICT ON "
    "UPDATE RESTRICT\n"
    ")",
    "space_bindings": "CREATE TABLE space_bindings (\n"
    "\tid VARCHAR(36) NOT NULL, \n"
    "\tspace_id VARCHAR(36) NOT NULL, \n"
    "\tplatform VARCHAR(32) NOT NULL, \n"
    "\texternal_space_id VARCHAR(255) NOT NULL, \n"
    "\tdisplay_name VARCHAR(128) NOT NULL, \n"
    "\tstatus VARCHAR(16) NOT NULL, \n"
    "\trevision INTEGER NOT NULL, \n"
    "\tfirst_seen_at DATETIME NOT NULL, \n"
    "\tlast_seen_at DATETIME NOT NULL, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT uq_space_bindings_platform_space UNIQUE (platform, "
    "external_space_id), \n"
    "\tCONSTRAINT ck_space_bindings_id CHECK (length(id) = 36 AND id = lower(id) "
    "AND id GLOB "
    "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
    "\tCONSTRAINT ck_space_bindings_space_id CHECK (length(space_id) = 36 AND "
    "space_id = lower(space_id) AND space_id GLOB "
    "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
    "\tCONSTRAINT ck_space_bindings_platform CHECK (length(platform) > 0 AND "
    "length(platform) <= 32 AND platform = lower(platform) AND platform = "
    "trim(platform)), \n"
    "\tCONSTRAINT ck_space_bindings_external_space_id CHECK "
    "(length(external_space_id) > 0 AND length(external_space_id) <= 255 AND "
    "external_space_id = trim(external_space_id)), \n"
    "\tCONSTRAINT ck_space_bindings_status CHECK (status IN ('active', "
    "'disabled')), \n"
    "\tCONSTRAINT ck_space_bindings_revision CHECK (revision >= 1), \n"
    "\tCONSTRAINT ck_space_bindings_seen_range CHECK (first_seen_at <= "
    "last_seen_at), \n"
    "\tFOREIGN KEY(space_id) REFERENCES spaces (id) ON DELETE RESTRICT ON UPDATE "
    "RESTRICT\n"
    ")",
    "person_aliases": "CREATE TABLE person_aliases (\n"
    "\tid INTEGER NOT NULL, \n"
    "\talias VARCHAR(128) NOT NULL, \n"
    "\talias_type VARCHAR(24) NOT NULL, \n"
    "\tfirst_seen_at DATETIME NOT NULL, \n"
    "\tlast_seen_at DATETIME NOT NULL, \n"
    "\tcanonical_person_id VARCHAR(36) NOT NULL, \n"
    "\tcanonical_space_id VARCHAR(36), \n"
    "\tPRIMARY KEY (id), \n"
    "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON DELETE RESTRICT "
    "ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_space_id) REFERENCES spaces (id) ON DELETE RESTRICT ON "
    "UPDATE RESTRICT\n"
    ")",
    "memberships": "CREATE TABLE memberships (\n"
    "\tgroup_card VARCHAR(128) NOT NULL, \n"
    "\tfirst_seen_at DATETIME NOT NULL, \n"
    "\tlast_seen_at DATETIME NOT NULL, \n"
    "\tcanonical_person_id VARCHAR(36) NOT NULL, \n"
    "\tcanonical_space_id VARCHAR(36) NOT NULL, \n"
    "\tPRIMARY KEY (canonical_person_id, canonical_space_id), \n"
    "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON DELETE RESTRICT ON "
    "UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_space_id) REFERENCES spaces (id) ON DELETE RESTRICT ON "
    "UPDATE RESTRICT\n"
    ")",
    "person_relationships": "CREATE TABLE person_relationships (\n"
    "\tcanonical_person_id VARCHAR(36) NOT NULL, \n"
    "\taffection_score INTEGER NOT NULL, \n"
    "\ttrust_score INTEGER NOT NULL, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tlast_automatic_change_at DATETIME, \n"
    "\tPRIMARY KEY (canonical_person_id), \n"
    "\tCONSTRAINT ck_person_relationships_affection_range CHECK "
    "(affection_score >= 0 AND affection_score <= 100), \n"
    "\tCONSTRAINT ck_person_relationships_trust_range CHECK (trust_score >= 0 "
    "AND trust_score <= 100), \n"
    "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "relationship_events": "CREATE TABLE relationship_events (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tsource_event_id INTEGER, \n"
    "\tactor_user_id VARCHAR(64), \n"
    "\tchange_type VARCHAR(16) NOT NULL, \n"
    "\taffection_before INTEGER NOT NULL, \n"
    "\taffection_delta INTEGER NOT NULL, \n"
    "\taffection_after INTEGER NOT NULL, \n"
    "\ttrust_before INTEGER NOT NULL, \n"
    "\ttrust_delta INTEGER NOT NULL, \n"
    "\ttrust_after INTEGER NOT NULL, \n"
    "\treason_code VARCHAR(64) NOT NULL, \n"
    "\tconfidence FLOAT, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tcanonical_person_id VARCHAR(36) NOT NULL, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT ck_relationship_events_change_type CHECK (change_type IN "
    "('automatic', 'manual')), \n"
    "\tFOREIGN KEY(source_event_id) REFERENCES chat_events (id) ON DELETE SET "
    "NULL, \n"
    "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "relationship_jobs": "CREATE TABLE relationship_jobs (\n"
    "\tid INTEGER NOT NULL, \n"
    "\ttrigger_event_id INTEGER NOT NULL, \n"
    "\tconversation_key VARCHAR(255) NOT NULL, \n"
    "\tstatus VARCHAR(16) NOT NULL, \n"
    "\tattempts INTEGER NOT NULL, \n"
    "\tnext_attempt_at DATETIME NOT NULL, \n"
    "\terror_category VARCHAR(64), \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tcanonical_person_id VARCHAR(36) NOT NULL, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT uq_relationship_jobs_trigger_event UNIQUE "
    "(trigger_event_id), \n"
    "\tCONSTRAINT ck_relationship_jobs_status CHECK (status IN ('pending', "
    "'processing', 'completed', 'failed')), \n"
    "\tFOREIGN KEY(trigger_event_id) REFERENCES chat_events (id) ON DELETE "
    "CASCADE, \n"
    "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "person_time_settings": "CREATE TABLE person_time_settings (\n"
    "\tcanonical_person_id VARCHAR(36) NOT NULL, \n"
    "\ttimezone VARCHAR(64) NOT NULL, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tPRIMARY KEY (canonical_person_id), \n"
    "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "person_speech_preferences": "CREATE TABLE person_speech_preferences (\n"
    "\tcanonical_person_id VARCHAR(36) NOT NULL, \n"
    "\tmode VARCHAR(32) NOT NULL, \n"
    "\tsource_message_id VARCHAR(128) NOT NULL, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tPRIMARY KEY (canonical_person_id), \n"
    "\tCONSTRAINT ck_person_speech_preferences_mode CHECK (mode IN "
    "('text_only', 'auto', 'prefer_voice')), \n"
    "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON "
    "DELETE RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "runtime_config_overrides": "CREATE TABLE runtime_config_overrides (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tconfig_key VARCHAR(128) NOT NULL, \n"
    "\tscope_type VARCHAR(16) NOT NULL, \n"
    "\tvalue_json TEXT NOT NULL, \n"
    "\tvalue_type VARCHAR(16) NOT NULL, \n"
    "\tapply_mode VARCHAR(32) NOT NULL, \n"
    "\tversion INTEGER NOT NULL, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tupdated_by VARCHAR(64) NOT NULL, \n"
    "\tcanonical_person_id VARCHAR(36), \n"
    "\tcanonical_space_id VARCHAR(36), \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT ck_runtime_config_overrides_scope_type CHECK "
    "(scope_type IN ('global', 'group', 'user')), \n"
    "\tCONSTRAINT ck_runtime_config_overrides_scope_owner CHECK "
    "((scope_type = 'global' AND canonical_person_id IS NULL AND "
    "canonical_space_id IS NULL) OR (scope_type = 'user' AND "
    "canonical_person_id IS NOT NULL AND canonical_space_id IS NULL) OR "
    "(scope_type = 'group' AND canonical_person_id IS NULL AND "
    "canonical_space_id IS NOT NULL)), \n"
    "\tCONSTRAINT ck_runtime_config_overrides_value_type CHECK "
    "(value_type IN ('string', 'integer', 'number', 'boolean', "
    "'enum')), \n"
    "\tCONSTRAINT ck_runtime_config_overrides_apply_mode CHECK "
    "(apply_mode IN ('hot', 'future_only', 'restart_required')), \n"
    "\tCONSTRAINT ck_runtime_config_overrides_version CHECK (version >= "
    "1), \n"
    "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_space_id) REFERENCES spaces (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "chat_events": "CREATE TABLE chat_events (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tbot_user_id VARCHAR(64) NOT NULL, \n"
    "\tplatform_message_id VARCHAR(128) NOT NULL, \n"
    "\tscope_type VARCHAR(16) NOT NULL, \n"
    "\tgroup_id VARCHAR(64), \n"
    "\tprivate_peer_user_id VARCHAR(64), \n"
    "\tsender_user_id VARCHAR(64) NOT NULL, \n"
    "\tsender_nickname VARCHAR(128) DEFAULT '' NOT NULL, \n"
    "\tsender_group_card VARCHAR(128) DEFAULT '' NOT NULL, \n"
    "\tdirection VARCHAR(16) NOT NULL, \n"
    "\tevent_kind VARCHAR(32) DEFAULT 'message' NOT NULL, \n"
    "\tsource_plugin_id VARCHAR(128), \n"
    "\texternal_source VARCHAR(64), \n"
    "\texternal_event_key VARCHAR(255), \n"
    "\texternal_event_type VARCHAR(128), \n"
    "\texternal_payload_json TEXT, \n"
    "\texternal_target_id VARCHAR(64), \n"
    "\tcontent TEXT NOT NULL, \n"
    "\tvisual_summary TEXT NOT NULL, \n"
    "\tsegments_json TEXT NOT NULL, \n"
    "\treply_to_message_id VARCHAR(128), \n"
    "\torigin VARCHAR(32) NOT NULL, \n"
    "\tautomation_id INTEGER, \n"
    "\tautomation_run_id INTEGER, \n"
    "\toccurred_at DATETIME NOT NULL, \n"
    "\tobserved_at DATETIME NOT NULL, \n"
    "\tcanonical_event_id VARCHAR(36) NOT NULL, \n"
    "\tcanonical_conversation_id VARCHAR(36) NOT NULL, \n"
    "\tauthor_kind VARCHAR(16) NOT NULL, \n"
    "\tauthor_person_id VARCHAR(36), \n"
    "\tauthor_presence_id VARCHAR(36), \n"
    "\tingress_presence_id VARCHAR(36), \n"
    "\tutterance_fingerprint VARCHAR(64), \n"
    "\tsuppression_status VARCHAR(16) NOT NULL, \n"
    "\tingress_provider VARCHAR(32), \n"
    "\tingress_gateway_instance_id VARCHAR(128), \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT ck_chat_events_kind_payload CHECK ((event_kind = 'message' AND "
    "source_plugin_id IS NULL AND external_source IS NULL AND external_event_key IS "
    "NULL AND external_event_type IS NULL AND external_payload_json IS NULL AND "
    "external_target_id IS NULL) OR (event_kind = 'external_event' AND "
    "source_plugin_id IS NOT NULL AND external_source IS NOT NULL AND "
    "external_event_key IS NOT NULL AND external_event_type IS NOT NULL AND "
    "external_payload_json IS NOT NULL AND external_target_id IS NOT NULL AND origin = "
    "'plugin_background' AND direction = 'external')), \n"
    "\tCONSTRAINT ck_chat_events_author_kind CHECK (author_kind IN ('person', 'yuki', "
    "'external_bot', 'system')), \n"
    "\tCONSTRAINT ck_chat_events_canonical_event_id CHECK (length(canonical_event_id) "
    "= 36 AND canonical_event_id = lower(canonical_event_id) AND canonical_event_id "
    "GLOB "
    "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
    "\tCONSTRAINT ck_chat_events_author CHECK ((author_kind = 'person' AND "
    "author_person_id IS NOT NULL AND author_presence_id IS NULL) OR (author_kind = "
    "'yuki' AND author_person_id IS NULL AND author_presence_id IS NOT NULL) OR "
    "(author_kind IN ('external_bot', 'system') AND author_person_id IS NULL AND "
    "author_presence_id IS NULL)), \n"
    "\tCONSTRAINT ck_chat_events_suppression_status CHECK (suppression_status IN "
    "('keeper', 'duplicate')), \n"
    "\tCONSTRAINT ck_chat_events_duplicate_fingerprint CHECK (suppression_status != "
    "'duplicate' OR utterance_fingerprint IS NOT NULL), \n"
    "\tFOREIGN KEY(automation_id) REFERENCES automations (id) ON DELETE SET NULL, \n"
    "\tFOREIGN KEY(automation_run_id) REFERENCES automation_runs (id) ON DELETE SET "
    "NULL, \n"
    "\tFOREIGN KEY(canonical_conversation_id) REFERENCES canonical_conversations (id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(author_person_id) REFERENCES persons (id) ON DELETE RESTRICT ON "
    "UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(author_presence_id) REFERENCES presences (id) ON DELETE RESTRICT ON "
    "UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(ingress_presence_id) REFERENCES presences (id) ON DELETE RESTRICT "
    "ON UPDATE RESTRICT\n"
    ")",
    "memory_facts": "CREATE TABLE memory_facts (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tscope_type VARCHAR(16) NOT NULL, \n"
    "\tvisibility_type VARCHAR(16), \n"
    "\tkind VARCHAR(16) NOT NULL, \n"
    "\tmemory_key VARCHAR(128) NOT NULL, \n"
    "\tcategory VARCHAR(64) NOT NULL, \n"
    "\tcontent TEXT NOT NULL, \n"
    "\tnormalized_content TEXT NOT NULL, \n"
    "\timportance INTEGER NOT NULL, \n"
    "\tconfidence FLOAT NOT NULL, \n"
    "\tsource_type VARCHAR(16) NOT NULL, \n"
    "\tauthority VARCHAR(16) DEFAULT 'self_report' NOT NULL, \n"
    "\tstatus VARCHAR(16) NOT NULL, \n"
    "\tconflict_state VARCHAR(16) DEFAULT 'clear' NOT NULL, \n"
    "\tsupersedes_id INTEGER, \n"
    "\tvalid_from DATETIME, \n"
    "\tvalid_until DATETIME, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tlast_confirmed_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, \n"
    "\tinvalidated_reason VARCHAR(40), \n"
    "\tlast_injected_at DATETIME, \n"
    "\tvalidation_version VARCHAR(32) DEFAULT 'legacy' NOT NULL, \n"
    "\tlast_audited_at DATETIME, \n"
    "\treview_state VARCHAR(24) DEFAULT 'legacy_unreviewed' NOT NULL, \n"
    "\tcanonical_subject_person_id VARCHAR(36), \n"
    "\tcanonical_subject_space_id VARCHAR(36), \n"
    "\tcanonical_visibility_person_id VARCHAR(36), \n"
    "\tcanonical_visibility_space_id VARCHAR(36), \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT ck_memory_facts_scope_type CHECK (scope_type IN ('person', "
    "'person_group', 'group', 'self')), \n"
    "\tCONSTRAINT ck_memory_facts_kind CHECK (kind IN ('fact', 'preference', "
    "'episode')), \n"
    "\tCONSTRAINT ck_memory_facts_source_type CHECK (source_type IN ('automatic', "
    "'explicit', 'rebuild')), \n"
    "\tCONSTRAINT ck_memory_facts_status CHECK (status IN ('active', 'contested', "
    "'superseded', 'invalidated')), \n"
    "\tCONSTRAINT ck_memory_facts_review_state CHECK (review_state IN "
    "('legacy_unreviewed', 'verified', 'quarantined')), \n"
    "\tCONSTRAINT ck_memory_facts_authority CHECK (authority IN ('explicit', "
    "'self_report', 'group_report', 'third_party', 'agent_reflection')), \n"
    "\tCONSTRAINT ck_memory_facts_agent_reflection_scope CHECK (authority != "
    "'agent_reflection' OR scope_type = 'self'), \n"
    "\tCONSTRAINT ck_memory_facts_conflict_state CHECK (conflict_state IN ('clear', "
    "'contested')), \n"
    "\tCONSTRAINT ck_memory_facts_contested_state CHECK (status != 'contested' OR "
    "conflict_state = 'contested'), \n"
    "\tCONSTRAINT ck_memory_facts_invalidation_reason CHECK ((status = 'invalidated' "
    "AND invalidated_reason IS NOT NULL) OR (status != 'invalidated' AND "
    "invalidated_reason IS NULL)), \n"
    "\tCONSTRAINT ck_memory_facts_importance CHECK (importance BETWEEN 1 AND 5), \n"
    "\tCONSTRAINT ck_memory_facts_confidence CHECK (confidence BETWEEN 0 AND 1), \n"
    "\tCONSTRAINT ck_memory_facts_scope_identity CHECK ((scope_type = 'person' AND "
    "canonical_subject_person_id IS NOT NULL AND canonical_subject_space_id IS NULL) "
    "OR (scope_type = 'person_group' AND canonical_subject_person_id IS NOT NULL AND "
    "canonical_subject_space_id IS NOT NULL) OR (scope_type = 'group' AND "
    "canonical_subject_person_id IS NULL AND canonical_subject_space_id IS NOT NULL) "
    "OR (scope_type = 'self' AND canonical_subject_person_id IS NULL AND "
    "canonical_subject_space_id IS NULL)), \n"
    "\tCONSTRAINT ck_memory_facts_self_visibility CHECK ((scope_type != 'self' AND "
    "visibility_type IS NULL AND canonical_visibility_person_id IS NULL AND "
    "canonical_visibility_space_id IS NULL) OR (scope_type = 'self' AND "
    "((visibility_type = 'global' AND canonical_visibility_person_id IS NULL AND "
    "canonical_visibility_space_id IS NULL) OR (visibility_type = 'private' AND "
    "canonical_visibility_person_id IS NOT NULL AND canonical_visibility_space_id IS "
    "NULL) OR (visibility_type = 'group' AND canonical_visibility_person_id IS NULL "
    "AND canonical_visibility_space_id IS NOT NULL)))), \n"
    "\tFOREIGN KEY(supersedes_id) REFERENCES memory_facts (id) ON DELETE SET NULL, \n"
    "\tFOREIGN KEY(canonical_subject_person_id) REFERENCES persons (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_subject_space_id) REFERENCES spaces (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_visibility_person_id) REFERENCES persons (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_visibility_space_id) REFERENCES spaces (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "memory_tool_receipts": "CREATE TABLE memory_tool_receipts (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tconversation_key_hash VARCHAR(64) NOT NULL, \n"
    "\ttrigger_event_id INTEGER NOT NULL, \n"
    "\tbot_user_id VARCHAR(64) NOT NULL, \n"
    "\tcanonical_person_id VARCHAR(36), \n"
    "\tcanonical_space_id VARCHAR(36), \n"
    "\tprovider_id VARCHAR(128) NOT NULL, \n"
    "\ttool_name VARCHAR(255) NOT NULL, \n"
    "\tsuccess BOOLEAN NOT NULL, \n"
    "\tresult_excerpt TEXT NOT NULL, \n"
    "\tresult_characters INTEGER NOT NULL, \n"
    "\terror_category VARCHAR(128), \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\texpires_at DATETIME NOT NULL, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT ck_memory_tool_receipts_size CHECK (result_characters >= "
    "0), \n"
    "\tCONSTRAINT ck_memory_tool_receipts_owner CHECK ((canonical_person_id "
    "IS NOT NULL AND canonical_space_id IS NULL) OR (canonical_person_id IS "
    "NULL AND canonical_space_id IS NOT NULL)), \n"
    "\tFOREIGN KEY(trigger_event_id) REFERENCES chat_events (id) ON DELETE "
    "CASCADE, \n"
    "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_space_id) REFERENCES spaces (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "memory_self_reflection_states": "CREATE TABLE memory_self_reflection_states (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tconversation_key_hash VARCHAR(64) NOT NULL, \n"
    "\tbot_user_id VARCHAR(64) NOT NULL, \n"
    "\tcanonical_person_id VARCHAR(36), \n"
    "\tcanonical_space_id VARCHAR(36), \n"
    "\tlast_event_id INTEGER NOT NULL, \n"
    "\tlatest_event_id INTEGER NOT NULL, \n"
    "\tpending_events INTEGER NOT NULL, \n"
    "\tpending_characters INTEGER NOT NULL, \n"
    "\tpending_since DATETIME, \n"
    "\thas_yuki_reply BOOLEAN NOT NULL, \n"
    "\thas_tool_result BOOLEAN NOT NULL, \n"
    "\thigh_value_signal BOOLEAN NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT ck_memory_self_reflection_states_owner CHECK "
    "((canonical_person_id IS NOT NULL AND canonical_space_id IS "
    "NULL) OR (canonical_person_id IS NULL AND canonical_space_id IS "
    "NOT NULL)), \n"
    "\tCONSTRAINT ck_self_reflection_state_pending CHECK "
    "(pending_events >= 0 AND pending_characters >= 0), \n"
    "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON "
    "DELETE RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_space_id) REFERENCES spaces (id) ON "
    "DELETE RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "memory_self_reflection_runs": "CREATE TABLE memory_self_reflection_runs (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tconversation_key_hash VARCHAR(64) NOT NULL, \n"
    "\tbot_user_id VARCHAR(64) NOT NULL, \n"
    "\tcanonical_person_id VARCHAR(36), \n"
    "\tcanonical_space_id VARCHAR(36), \n"
    "\tscheduled_slot VARCHAR(32) NOT NULL, \n"
    "\ttrigger_reason VARCHAR(32) NOT NULL, \n"
    "\tfirst_event_id INTEGER NOT NULL, \n"
    "\tlast_event_id INTEGER NOT NULL, \n"
    "\tstatus VARCHAR(16) NOT NULL, \n"
    "\tproposal_count INTEGER NOT NULL, \n"
    "\tcommitted_count INTEGER NOT NULL, \n"
    "\terror_category VARCHAR(64), \n"
    "\tstarted_at DATETIME NOT NULL, \n"
    "\tcompleted_at DATETIME, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT ck_memory_self_reflection_runs_owner CHECK "
    "((canonical_person_id IS NOT NULL AND canonical_space_id IS NULL) "
    "OR (canonical_person_id IS NULL AND canonical_space_id IS NOT "
    "NULL)), \n"
    "\tCONSTRAINT ck_self_reflection_run_status CHECK (status IN "
    "('processing','completed','failed')), \n"
    "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON "
    "DELETE RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_space_id) REFERENCES spaces (id) ON "
    "DELETE RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "memory_jobs": "CREATE TABLE memory_jobs (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tevent_id INTEGER NOT NULL, \n"
    "\tconversation_key VARCHAR(255) NOT NULL, \n"
    "\tcanonical_person_id VARCHAR(36), \n"
    "\tcanonical_space_id VARCHAR(36), \n"
    "\tstatus VARCHAR(16) NOT NULL, \n"
    "\tattempts INTEGER NOT NULL, \n"
    "\tnext_attempt_at DATETIME NOT NULL, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\terror_category VARCHAR(64), \n"
    "\tprocessing_source VARCHAR(16) DEFAULT 'live' NOT NULL, \n"
    "\trebuild_run_id INTEGER, \n"
    "\toutcome VARCHAR(32), \n"
    "\tcompleted_at DATETIME, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT uq_memory_jobs_event UNIQUE (event_id), \n"
    "\tCONSTRAINT ck_memory_jobs_owner CHECK ((canonical_person_id IS NOT NULL AND "
    "canonical_space_id IS NULL) OR (canonical_person_id IS NULL AND "
    "canonical_space_id IS NOT NULL)), \n"
    "\tCONSTRAINT ck_memory_jobs_status CHECK (status IN ('pending', 'processing', "
    "'done', 'failed')), \n"
    "\tCONSTRAINT ck_memory_jobs_processing_source CHECK (processing_source IN "
    "('live', 'rebuild')), \n"
    "\tCONSTRAINT ck_memory_jobs_outcome CHECK (outcome IS NULL OR outcome IN "
    "('claims_applied', 'candidates_staged', 'no_claims', 'all_rejected', "
    "'already_processed')), \n"
    "\tFOREIGN KEY(event_id) REFERENCES chat_events (id) ON DELETE CASCADE, \n"
    "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON DELETE RESTRICT ON "
    "UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_space_id) REFERENCES spaces (id) ON DELETE RESTRICT ON "
    "UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(rebuild_run_id) REFERENCES memory_rebuild_runs (id) ON DELETE SET "
    "NULL\n"
    ")",
    "memory_dream_clusters": "CREATE TABLE memory_dream_clusters (\n"
    "\tid INTEGER NOT NULL, \n"
    "\trun_id INTEGER NOT NULL, \n"
    "\tcluster_key VARCHAR(64) NOT NULL, \n"
    "\tpartition_key VARCHAR(64) NOT NULL, \n"
    "\tbot_user_id VARCHAR(64) NOT NULL, \n"
    "\tcanonical_subject_person_id VARCHAR(36), \n"
    "\tcanonical_subject_space_id VARCHAR(36), \n"
    "\tcanonical_visibility_person_id VARCHAR(36), \n"
    "\tcanonical_visibility_space_id VARCHAR(36), \n"
    "\tkind VARCHAR(16) NOT NULL, \n"
    "\tstatus VARCHAR(16) NOT NULL, \n"
    "\tfact_ids_json TEXT NOT NULL, \n"
    "\tfingerprint VARCHAR(64) NOT NULL, \n"
    "\tattempts INTEGER NOT NULL, \n"
    "\tmodel_calls INTEGER NOT NULL, \n"
    "\toperation_count INTEGER NOT NULL, \n"
    "\terror_category VARCHAR(64), \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tcompleted_at DATETIME, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT uq_memory_dream_clusters_run_key UNIQUE (run_id, "
    "cluster_key), \n"
    "\tCONSTRAINT ck_memory_dream_clusters_status CHECK (status IN "
    "('pending','processing','completed','failed','stale','skipped','rolled_back')), \n"
    "\tCONSTRAINT ck_dream_cluster_kind CHECK (kind IN "
    "('fact','preference','episode')), \n"
    "\tCONSTRAINT ck_memory_dream_clusters_owner CHECK (NOT "
    "(canonical_visibility_person_id IS NOT NULL AND "
    "canonical_visibility_space_id IS NOT NULL) AND NOT "
    "((canonical_subject_person_id IS NOT NULL OR canonical_subject_space_id "
    "IS NOT NULL) AND (canonical_visibility_person_id IS NOT NULL OR "
    "canonical_visibility_space_id IS NOT NULL))), \n"
    "\tFOREIGN KEY(run_id) REFERENCES memory_dream_runs (id) ON DELETE "
    "CASCADE, \n"
    "\tFOREIGN KEY(canonical_subject_person_id) REFERENCES persons (id) ON "
    "DELETE RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_subject_space_id) REFERENCES spaces (id) ON "
    "DELETE RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_visibility_person_id) REFERENCES persons (id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_visibility_space_id) REFERENCES spaces (id) ON "
    "DELETE RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "automations": "CREATE TABLE automations (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tcreator_user_id VARCHAR(64) NOT NULL, \n"
    "\tbot_user_id VARCHAR(64) NOT NULL, \n"
    "\tname VARCHAR(128) NOT NULL, \n"
    "\tstatus VARCHAR(16) NOT NULL, \n"
    "\ttimezone VARCHAR(64) NOT NULL, \n"
    "\tschedule_json TEXT NOT NULL, \n"
    "\tscript_json TEXT NOT NULL, \n"
    "\tscript_hash VARCHAR(64) NOT NULL, \n"
    "\trequired_capabilities_json TEXT NOT NULL, \n"
    "\tauthority_snapshot_json TEXT NOT NULL, \n"
    "\tcreated_from_message_id VARCHAR(128) NOT NULL, \n"
    "\tnext_run_at DATETIME, \n"
    "\tlast_run_at DATETIME, \n"
    "\trun_count INTEGER NOT NULL, \n"
    "\tmax_runs INTEGER, \n"
    "\tconsecutive_failures INTEGER NOT NULL, \n"
    "\tmisfire_grace_seconds INTEGER NOT NULL, \n"
    "\tclaimed_by VARCHAR(64), \n"
    "\tclaimed_until DATETIME, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tcanonical_creator_person_id VARCHAR(36), \n"
    "\tcanonical_target_person_id VARCHAR(36), \n"
    "\tcanonical_target_space_id VARCHAR(36), \n"
    "\tcanonical_presence_id VARCHAR(36), \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT ck_automations_status CHECK (status IN ('active', 'paused', "
    "'completed', 'cancelled', 'failed', 'blocked')), \n"
    "\tCONSTRAINT ck_automations_run_count CHECK (run_count >= 0), \n"
    "\tCONSTRAINT ck_automations_consecutive_failures CHECK (consecutive_failures >= "
    "0), \n"
    "\tCONSTRAINT ck_automations_canonical_owner CHECK ((status IN ('active', "
    "'paused') AND canonical_creator_person_id IS NOT NULL AND "
    "((canonical_target_person_id IS NOT NULL AND canonical_target_space_id IS NULL) "
    "OR (canonical_target_person_id IS NULL AND canonical_target_space_id IS NOT "
    "NULL))) OR (status IN ('completed', 'cancelled', 'failed', 'blocked') AND NOT "
    "(canonical_target_person_id IS NOT NULL AND canonical_target_space_id IS NOT "
    "NULL))), \n"
    "\tFOREIGN KEY(canonical_creator_person_id) REFERENCES persons (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_target_person_id) REFERENCES persons (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_target_space_id) REFERENCES spaces (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_presence_id) REFERENCES presences (id) ON DELETE RESTRICT "
    "ON UPDATE RESTRICT\n"
    ")",
    "plugin_config_values": "CREATE TABLE plugin_config_values (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tplugin_id VARCHAR(128) NOT NULL, \n"
    "\tscope_type VARCHAR(16) NOT NULL, \n"
    '\t"key" VARCHAR(128) NOT NULL, \n'
    "\tvalue_json TEXT NOT NULL, \n"
    "\tversion INTEGER NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tcanonical_person_id VARCHAR(36), \n"
    "\tcanonical_space_id VARCHAR(36), \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT ck_plugin_config_values_scope_type CHECK (scope_type IN "
    "('global', 'group', 'user')), \n"
    "\tCONSTRAINT ck_plugin_config_values_scope_owner CHECK ((scope_type = "
    "'global' AND canonical_person_id IS NULL AND canonical_space_id IS NULL) "
    "OR (scope_type = 'user' AND canonical_person_id IS NOT NULL AND "
    "canonical_space_id IS NULL) OR (scope_type = 'group' AND "
    "canonical_person_id IS NULL AND canonical_space_id IS NOT NULL)), \n"
    "\tCONSTRAINT ck_plugin_config_values_version CHECK (version >= 1), \n"
    "\tFOREIGN KEY(plugin_id) REFERENCES plugin_installations (plugin_id) ON "
    "DELETE CASCADE, \n"
    "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_space_id) REFERENCES spaces (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "plugin_state": "CREATE TABLE plugin_state (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tplugin_id VARCHAR(128) NOT NULL, \n"
    "\tnamespace VARCHAR(128) NOT NULL, \n"
    '\t"key" VARCHAR(128) NOT NULL, \n'
    "\tvalue_json TEXT NOT NULL, \n"
    "\tversion INTEGER NOT NULL, \n"
    "\texpires_at DATETIME, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tcanonical_person_id VARCHAR(36), \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT uq_plugin_state_namespace_key UNIQUE (plugin_id, namespace, "
    '"key"), \n'
    "\tCONSTRAINT ck_plugin_state_version CHECK (version >= 1), \n"
    "\tFOREIGN KEY(plugin_id) REFERENCES plugin_installations (plugin_id) ON DELETE "
    "CASCADE, \n"
    "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON DELETE RESTRICT ON "
    "UPDATE RESTRICT\n"
    ")",
    "plugin_agent_sessions": "CREATE TABLE plugin_agent_sessions (\n"
    "\tsession_id VARCHAR(64) NOT NULL, \n"
    "\tplugin_id VARCHAR(128) NOT NULL, \n"
    "\tscope_type VARCHAR(16) NOT NULL, \n"
    "\tname VARCHAR(128) NOT NULL, \n"
    "\tmodel VARCHAR(128) NOT NULL, \n"
    "\tinstructions TEXT NOT NULL, \n"
    "\tpersistence VARCHAR(16) NOT NULL, \n"
    "\tcontext_profile VARCHAR(32) NOT NULL, \n"
    "\tallowed_capabilities_json TEXT NOT NULL, \n"
    "\tstatus VARCHAR(16) NOT NULL, \n"
    "\tnext_sequence INTEGER NOT NULL, \n"
    "\tturn_count INTEGER NOT NULL, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tlast_active_at DATETIME NOT NULL, \n"
    "\texpires_at DATETIME, \n"
    "\tcanonical_owner_person_id VARCHAR(36), \n"
    "\tcanonical_space_id VARCHAR(36), \n"
    "\tPRIMARY KEY (session_id), \n"
    "\tCONSTRAINT ck_plugin_agent_sessions_scope_type CHECK (scope_type IN "
    "('user', 'group', 'plugin')), \n"
    "\tCONSTRAINT ck_plugin_agent_sessions_scope_owner CHECK ((scope_type = "
    "'plugin' AND canonical_owner_person_id IS NULL AND canonical_space_id "
    "IS NULL) OR (scope_type = 'user' AND canonical_owner_person_id IS NOT "
    "NULL AND canonical_space_id IS NULL) OR (scope_type = 'group' AND "
    "canonical_space_id IS NOT NULL)), \n"
    "\tCONSTRAINT ck_plugin_agent_sessions_status CHECK (status IN "
    "('active', 'closed', 'expired', 'blocked')), \n"
    "\tCONSTRAINT ck_plugin_agent_sessions_persistence CHECK (persistence IN "
    "('ephemeral', 'durable')), \n"
    "\tCONSTRAINT ck_plugin_agent_sessions_context_profile CHECK "
    "(context_profile IN ('none', 'current_user', 'current_group')), \n"
    "\tCONSTRAINT ck_plugin_agent_sessions_instructions CHECK "
    "(length(instructions) >= 1 AND length(instructions) <= 8000), \n"
    "\tCONSTRAINT ck_plugin_agent_sessions_sequence CHECK (next_sequence >= "
    "1), \n"
    "\tCONSTRAINT ck_plugin_agent_sessions_turn_count CHECK (turn_count >= "
    "0), \n"
    "\tFOREIGN KEY(plugin_id) REFERENCES plugin_installations (plugin_id) ON "
    "DELETE CASCADE, \n"
    "\tFOREIGN KEY(canonical_owner_person_id) REFERENCES persons (id) ON "
    "DELETE RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_space_id) REFERENCES spaces (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "plugin_agent_messages": "CREATE TABLE plugin_agent_messages (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tsession_id VARCHAR(64) NOT NULL, \n"
    "\tsequence INTEGER NOT NULL, \n"
    "\trole VARCHAR(16) NOT NULL, \n"
    "\tsender_user_id VARCHAR(64), \n"
    "\tcontent TEXT NOT NULL, \n"
    "\tmetadata_json TEXT NOT NULL, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tcanonical_sender_person_id VARCHAR(36), \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT uq_plugin_agent_messages_session_sequence UNIQUE "
    "(session_id, sequence), \n"
    "\tCONSTRAINT ck_plugin_agent_messages_role CHECK (role IN ('user', "
    "'assistant', 'tool')), \n"
    "\tCONSTRAINT ck_plugin_agent_messages_sequence CHECK (sequence >= 1), \n"
    "\tCONSTRAINT ck_plugin_agent_messages_sender CHECK "
    "(canonical_sender_person_id IS NULL OR role = 'user'), \n"
    "\tFOREIGN KEY(session_id) REFERENCES plugin_agent_sessions (session_id) "
    "ON DELETE CASCADE, \n"
    "\tFOREIGN KEY(canonical_sender_person_id) REFERENCES persons (id) ON "
    "DELETE RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "plugin_background_target_grants": "CREATE TABLE plugin_background_target_grants (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tplugin_id VARCHAR(128) NOT NULL, \n"
    "\ttarget_type VARCHAR(16) NOT NULL, \n"
    "\ttarget_id VARCHAR(64) NOT NULL, \n"
    "\tbot_user_id VARCHAR(64) NOT NULL, \n"
    "\tenabled BOOLEAN NOT NULL, \n"
    "\tcreated_by_user_id VARCHAR(64) NOT NULL, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tcanonical_target_person_id VARCHAR(36), \n"
    "\tcanonical_target_space_id VARCHAR(36), \n"
    "\tcanonical_created_by_person_id VARCHAR(36), \n"
    "\tcanonical_presence_id VARCHAR(36), \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT ck_plugin_background_target_type CHECK "
    "(target_type IN ('group', 'private')), \n"
    "\tCONSTRAINT ck_plugin_background_target_owner CHECK (NOT "
    "(canonical_target_person_id IS NOT NULL AND "
    "canonical_target_space_id IS NOT NULL) AND "
    "(canonical_target_person_id IS NULL OR target_type = "
    "'private') AND (canonical_target_space_id IS NULL OR "
    "target_type = 'group') AND (enabled = 0 OR "
    "(canonical_created_by_person_id IS NOT NULL AND "
    "canonical_presence_id IS NOT NULL AND ((target_type = "
    "'private' AND canonical_target_person_id IS NOT NULL AND "
    "canonical_target_space_id IS NULL) OR (target_type = 'group' "
    "AND canonical_target_person_id IS NULL AND "
    "canonical_target_space_id IS NOT NULL))))), \n"
    "\tFOREIGN KEY(plugin_id) REFERENCES plugin_installations "
    "(plugin_id) ON DELETE CASCADE, \n"
    "\tFOREIGN KEY(canonical_target_person_id) REFERENCES persons "
    "(id) ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_target_space_id) REFERENCES spaces "
    "(id) ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_created_by_person_id) REFERENCES "
    "persons (id) ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_presence_id) REFERENCES presences "
    "(id) ON DELETE RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "plugin_notification_outbox": "CREATE TABLE plugin_notification_outbox (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tnotification_id VARCHAR(64) NOT NULL, \n"
    "\tpart_key VARCHAR(255) NOT NULL, \n"
    "\tsource_event_id INTEGER NOT NULL, \n"
    "\tplugin_id VARCHAR(128) NOT NULL, \n"
    "\ttarget_type VARCHAR(16) NOT NULL, \n"
    "\ttarget_id VARCHAR(64) NOT NULL, \n"
    "\tbot_user_id VARCHAR(64) NOT NULL, \n"
    "\tpart_type VARCHAR(16) NOT NULL, \n"
    "\ttext TEXT NOT NULL, \n"
    "\tmedia_handle_id VARCHAR(128), \n"
    "\tstatus VARCHAR(16) NOT NULL, \n"
    "\tattempts INTEGER NOT NULL, \n"
    "\tmax_attempts INTEGER NOT NULL, \n"
    "\tnext_attempt_at DATETIME NOT NULL, \n"
    "\tlease_until DATETIME, \n"
    "\tplatform_message_id VARCHAR(128), \n"
    "\tlast_error_category VARCHAR(64), \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tsent_at DATETIME, \n"
    "\tcanonical_target_person_id VARCHAR(36), \n"
    "\tcanonical_target_space_id VARCHAR(36), \n"
    "\tcanonical_conversation_id VARCHAR(36), \n"
    "\tcanonical_presence_id VARCHAR(36), \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT uq_plugin_notification_outbox_part UNIQUE "
    "(notification_id, part_key), \n"
    "\tCONSTRAINT ck_plugin_outbox_target_type CHECK (target_type IN "
    "('group', 'private')), \n"
    "\tCONSTRAINT ck_plugin_outbox_target_owner CHECK (NOT "
    "(canonical_target_person_id IS NOT NULL AND "
    "canonical_target_space_id IS NOT NULL) AND "
    "(canonical_target_person_id IS NULL OR target_type = 'private') "
    "AND (canonical_target_space_id IS NULL OR target_type = 'group') "
    "AND (status NOT IN ('pending', 'processing') OR "
    "(canonical_conversation_id IS NOT NULL AND canonical_presence_id "
    "IS NOT NULL AND ((target_type = 'private' AND "
    "canonical_target_person_id IS NOT NULL AND "
    "canonical_target_space_id IS NULL) OR (target_type = 'group' AND "
    "canonical_target_person_id IS NULL AND canonical_target_space_id "
    "IS NOT NULL))))), \n"
    "\tCONSTRAINT ck_plugin_notification_outbox_part_type CHECK "
    "(part_type IN ('text', 'media', 'agent_reply')), \n"
    "\tCONSTRAINT ck_plugin_notification_outbox_status CHECK (status IN "
    "('pending', 'processing', 'sent', 'failed', 'uncertain', "
    "'cancelled')), \n"
    "\tCONSTRAINT ck_plugin_outbox_attempts CHECK (attempts >= 0 AND "
    "max_attempts >= 1), \n"
    "\tFOREIGN KEY(source_event_id) REFERENCES chat_events (id) ON "
    "DELETE CASCADE, \n"
    "\tFOREIGN KEY(plugin_id) REFERENCES plugin_installations "
    "(plugin_id) ON DELETE CASCADE, \n"
    "\tFOREIGN KEY(media_handle_id) REFERENCES plugin_media_artifacts "
    "(handle_id) ON DELETE SET NULL, \n"
    "\tFOREIGN KEY(canonical_target_person_id) REFERENCES persons (id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_target_space_id) REFERENCES spaces (id) ON "
    "DELETE RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_conversation_id) REFERENCES "
    "canonical_conversations (id) ON DELETE RESTRICT ON UPDATE "
    "RESTRICT, \n"
    "\tFOREIGN KEY(canonical_presence_id) REFERENCES presences (id) ON "
    "DELETE RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "plugin_background_turn_jobs": "CREATE TABLE plugin_background_turn_jobs (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tsource_event_id INTEGER NOT NULL, \n"
    "\tplugin_id VARCHAR(128) NOT NULL, \n"
    "\ttarget_type VARCHAR(16) NOT NULL, \n"
    "\ttarget_id VARCHAR(64) NOT NULL, \n"
    "\tbot_user_id VARCHAR(64) NOT NULL, \n"
    "\tagent_intent VARCHAR(1000) NOT NULL, \n"
    "\tstatus VARCHAR(16) NOT NULL, \n"
    "\tattempts INTEGER NOT NULL, \n"
    "\tmax_attempts INTEGER NOT NULL, \n"
    "\tnext_attempt_at DATETIME NOT NULL, \n"
    "\tlease_until DATETIME, \n"
    "\tgenerated_text TEXT NOT NULL, \n"
    "\ttool_calls_used INTEGER NOT NULL, \n"
    "\tmodel_requests INTEGER NOT NULL, \n"
    "\tlast_error_category VARCHAR(64), \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tcompleted_at DATETIME, \n"
    "\tcanonical_target_person_id VARCHAR(36), \n"
    "\tcanonical_target_space_id VARCHAR(36), \n"
    "\tcanonical_conversation_id VARCHAR(36), \n"
    "\tcanonical_presence_id VARCHAR(36), \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT uq_plugin_background_turn_source UNIQUE "
    "(source_event_id), \n"
    "\tCONSTRAINT ck_plugin_turn_target_type CHECK (target_type IN "
    "('group', 'private')), \n"
    "\tCONSTRAINT ck_plugin_turn_target_owner CHECK (NOT "
    "(canonical_target_person_id IS NOT NULL AND "
    "canonical_target_space_id IS NOT NULL) AND "
    "(canonical_target_person_id IS NULL OR target_type = 'private') "
    "AND (canonical_target_space_id IS NULL OR target_type = 'group') "
    "AND (status NOT IN ('pending', 'processing') OR "
    "(canonical_conversation_id IS NOT NULL AND canonical_presence_id "
    "IS NOT NULL AND ((target_type = 'private' AND "
    "canonical_target_person_id IS NOT NULL AND "
    "canonical_target_space_id IS NULL) OR (target_type = 'group' AND "
    "canonical_target_person_id IS NULL AND canonical_target_space_id "
    "IS NOT NULL))))), \n"
    "\tCONSTRAINT ck_plugin_background_turn_status CHECK (status IN "
    "('pending', 'processing', 'completed', 'failed', 'cancelled')), \n"
    "\tCONSTRAINT ck_plugin_turn_attempts CHECK (attempts >= 0 AND "
    "max_attempts >= 1), \n"
    "\tFOREIGN KEY(source_event_id) REFERENCES chat_events (id) ON "
    "DELETE CASCADE, \n"
    "\tFOREIGN KEY(plugin_id) REFERENCES plugin_installations "
    "(plugin_id) ON DELETE CASCADE, \n"
    "\tFOREIGN KEY(canonical_target_person_id) REFERENCES persons (id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_target_space_id) REFERENCES spaces (id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
    "\tFOREIGN KEY(canonical_conversation_id) REFERENCES "
    "canonical_conversations (id) ON DELETE RESTRICT ON UPDATE "
    "RESTRICT, \n"
    "\tFOREIGN KEY(canonical_presence_id) REFERENCES presences (id) ON "
    "DELETE RESTRICT ON UPDATE RESTRICT\n"
    ")",
    "emoji_scope_states": "CREATE TABLE emoji_scope_states (\n"
    "\tid INTEGER NOT NULL, \n"
    "\temoji_id VARCHAR(36) NOT NULL, \n"
    "\tscope_type VARCHAR(16) NOT NULL, \n"
    "\tenabled BOOLEAN NOT NULL, \n"
    "\tweight FLOAT NOT NULL, \n"
    "\tadopted_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tcanonical_space_id VARCHAR(36), \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT ck_emoji_scope_scope_type CHECK (scope_type IN ('global', "
    "'group')), \n"
    "\tCONSTRAINT ck_emoji_scope_owner CHECK ((scope_type = 'global' AND "
    "canonical_space_id IS NULL) OR (scope_type = 'group' AND "
    "canonical_space_id IS NOT NULL)), \n"
    "\tCONSTRAINT ck_emoji_scope_weight CHECK (weight >= 0), \n"
    "\tFOREIGN KEY(emoji_id) REFERENCES emoji_assets (id) ON DELETE CASCADE, \n"
    "\tFOREIGN KEY(canonical_space_id) REFERENCES spaces (id) ON DELETE "
    "RESTRICT ON UPDATE RESTRICT\n"
    ")",
}

_TABLE_INDEX_DDL: Final[dict[str, tuple[str, ...]]] = {
    "identity_bindings": (
        "CREATE INDEX ix_identity_bindings_person_id ON identity_bindings (person_id)",
    ),
    "space_bindings": ("CREATE INDEX ix_space_bindings_space_id ON space_bindings (space_id)",),
    "person_aliases": (
        "CREATE INDEX ix_person_aliases_canonical_person_id ON person_aliases "
        "(canonical_person_id)",
        "CREATE INDEX ix_person_aliases_canonical_space_id ON person_aliases (canonical_space_id)",
        "CREATE INDEX ix_person_aliases_person_last_seen ON person_aliases "
        "(canonical_person_id, last_seen_at)",
        "CREATE UNIQUE INDEX uq_person_alias_scope ON person_aliases "
        "(canonical_person_id, COALESCE(canonical_space_id, ''), alias)",
    ),
    "memberships": (
        "CREATE INDEX ix_memberships_canonical_person_id ON memberships (canonical_person_id)",
        "CREATE INDEX ix_memberships_canonical_space_id ON memberships (canonical_space_id)",
    ),
    "person_relationships": (),
    "relationship_events": (
        "CREATE INDEX ix_relationship_events_canonical_person_id ON "
        "relationship_events (canonical_person_id)",
        "CREATE INDEX ix_relationship_events_person_created ON "
        "relationship_events (canonical_person_id, created_at)",
        "CREATE UNIQUE INDEX uq_relationship_events_automatic_source ON "
        "relationship_events (source_event_id) WHERE source_event_id IS NOT NULL "
        "AND change_type = 'automatic'",
    ),
    "relationship_jobs": (
        "CREATE INDEX ix_relationship_jobs_canonical_person_id ON relationship_jobs "
        "(canonical_person_id)",
        "CREATE INDEX ix_relationship_jobs_status_next ON relationship_jobs "
        "(status, next_attempt_at)",
    ),
    "person_time_settings": (),
    "person_speech_preferences": (
        "CREATE INDEX ix_person_speech_preferences_updated ON "
        "person_speech_preferences (updated_at)",
    ),
    "runtime_config_overrides": (
        "CREATE INDEX ix_runtime_config_overrides_canonical_person_id ON "
        "runtime_config_overrides (canonical_person_id)",
        "CREATE INDEX ix_runtime_config_overrides_canonical_space_id ON "
        "runtime_config_overrides (canonical_space_id)",
        "CREATE INDEX ix_runtime_config_overrides_scope_key ON "
        "runtime_config_overrides (scope_type, config_key)",
        "CREATE UNIQUE INDEX uq_runtime_config_overrides_global_key ON "
        "runtime_config_overrides (config_key) WHERE scope_type = 'global'",
        "CREATE UNIQUE INDEX uq_runtime_config_overrides_person_key ON "
        "runtime_config_overrides (config_key, canonical_person_id) WHERE "
        "scope_type = 'user'",
        "CREATE UNIQUE INDEX uq_runtime_config_overrides_space_key ON "
        "runtime_config_overrides (config_key, canonical_space_id) WHERE "
        "scope_type = 'group'",
    ),
    "chat_events": (
        "CREATE INDEX ix_chat_events_automation ON chat_events (automation_id, automation_run_id)",
        "CREATE INDEX ix_chat_events_bot_scope_group_id ON chat_events (bot_user_id, "
        "scope_type, group_id, id)",
        "CREATE INDEX ix_chat_events_bot_scope_private_id ON chat_events (bot_user_id, "
        "scope_type, private_peer_user_id, id)",
        "CREATE INDEX ix_chat_events_canonical_conversation_id ON chat_events "
        "(canonical_conversation_id)",
        "CREATE INDEX ix_chat_events_canonical_event_id ON chat_events (canonical_event_id)",
        "CREATE INDEX ix_chat_events_group_time ON chat_events (group_id, occurred_at)",
        "CREATE INDEX ix_chat_events_private_peer_time ON chat_events "
        "(private_peer_user_id, occurred_at)",
        "CREATE INDEX ix_chat_events_scope_time ON chat_events (scope_type, occurred_at)",
        "CREATE INDEX ix_chat_events_sender_time ON chat_events (sender_user_id, occurred_at)",
        "CREATE UNIQUE INDEX uq_chat_events_canonical_event_keeper ON chat_events "
        "(canonical_event_id) WHERE suppression_status = 'keeper'",
        "CREATE UNIQUE INDEX uq_chat_events_external_event_target ON chat_events "
        "(source_plugin_id, external_event_key, scope_type, external_target_id) WHERE "
        "event_kind = 'external_event'",
    ),
    "memory_facts": (
        "CREATE INDEX ix_memory_facts_canonical_subject_person_id ON memory_facts "
        "(canonical_subject_person_id)",
        "CREATE INDEX ix_memory_facts_canonical_subject_space_id ON memory_facts "
        "(canonical_subject_space_id)",
        "CREATE INDEX ix_memory_facts_canonical_visibility_person_id ON memory_facts "
        "(canonical_visibility_person_id)",
        "CREATE INDEX ix_memory_facts_canonical_visibility_space_id ON memory_facts "
        "(canonical_visibility_space_id)",
        "CREATE INDEX ix_memory_facts_scope_status_updated ON memory_facts (scope_type, "
        "canonical_subject_person_id, canonical_subject_space_id, status, updated_at)",
        "CREATE UNIQUE INDEX uq_memory_facts_active_canonical_group_key ON memory_facts "
        "(canonical_subject_space_id, kind, memory_key) WHERE status = 'active' AND "
        "scope_type = 'group' AND canonical_subject_space_id IS NOT NULL AND "
        "canonical_subject_person_id IS NULL",
        "CREATE UNIQUE INDEX uq_memory_facts_active_canonical_person_group_key ON "
        "memory_facts (canonical_subject_person_id, canonical_subject_space_id, kind, "
        "memory_key) WHERE status = 'active' AND scope_type = 'person_group' AND "
        "canonical_subject_person_id IS NOT NULL AND canonical_subject_space_id IS NOT "
        "NULL",
        "CREATE UNIQUE INDEX uq_memory_facts_active_canonical_person_key ON memory_facts "
        "(canonical_subject_person_id, kind, memory_key) WHERE status = 'active' AND "
        "scope_type = 'person' AND canonical_subject_person_id IS NOT NULL AND "
        "canonical_subject_space_id IS NULL",
        "CREATE UNIQUE INDEX uq_memory_facts_active_canonical_self_key ON memory_facts "
        "(memory_key, visibility_type, COALESCE(canonical_visibility_person_id, ''), "
        "COALESCE(canonical_visibility_space_id, '')) WHERE status = 'active' AND "
        "scope_type = 'self'",
    ),
    "memory_tool_receipts": (
        "CREATE INDEX ix_memory_tool_receipts_canonical_person_id ON "
        "memory_tool_receipts (canonical_person_id)",
        "CREATE INDEX ix_memory_tool_receipts_canonical_space_id ON "
        "memory_tool_receipts (canonical_space_id)",
        "CREATE INDEX ix_memory_tool_receipts_conversation_created ON "
        "memory_tool_receipts (conversation_key_hash, created_at)",
        "CREATE INDEX ix_memory_tool_receipts_expires ON memory_tool_receipts (expires_at)",
    ),
    "memory_self_reflection_states": (
        "CREATE INDEX ix_memory_self_reflection_state_pending ON "
        "memory_self_reflection_states (pending_since, last_event_id)",
        "CREATE INDEX "
        "ix_memory_self_reflection_states_canonical_person_id ON "
        "memory_self_reflection_states (canonical_person_id)",
        "CREATE INDEX "
        "ix_memory_self_reflection_states_canonical_space_id ON "
        "memory_self_reflection_states (canonical_space_id)",
        "CREATE UNIQUE INDEX "
        "uq_memory_self_reflection_states_canonical_person ON "
        "memory_self_reflection_states (canonical_person_id) WHERE "
        "canonical_person_id IS NOT NULL AND canonical_space_id IS NULL",
        "CREATE UNIQUE INDEX "
        "uq_memory_self_reflection_states_canonical_space ON "
        "memory_self_reflection_states (canonical_space_id) WHERE "
        "canonical_space_id IS NOT NULL AND canonical_person_id IS "
        "NULL",
    ),
    "memory_self_reflection_runs": (
        "CREATE INDEX ix_memory_self_reflection_runs_canonical_person_id "
        "ON memory_self_reflection_runs (canonical_person_id)",
        "CREATE INDEX ix_memory_self_reflection_runs_canonical_space_id "
        "ON memory_self_reflection_runs (canonical_space_id)",
        "CREATE INDEX ix_memory_self_reflection_runs_slot ON "
        "memory_self_reflection_runs (scheduled_slot, status)",
        "CREATE UNIQUE INDEX "
        "uq_memory_self_reflection_runs_canonical_person_slot ON "
        "memory_self_reflection_runs (canonical_person_id, "
        "scheduled_slot) WHERE canonical_person_id IS NOT NULL AND "
        "canonical_space_id IS NULL",
        "CREATE UNIQUE INDEX "
        "uq_memory_self_reflection_runs_canonical_space_slot ON "
        "memory_self_reflection_runs (canonical_space_id, scheduled_slot) "
        "WHERE canonical_space_id IS NOT NULL AND canonical_person_id IS "
        "NULL",
    ),
    "memory_jobs": (
        "CREATE INDEX ix_memory_jobs_canonical_person_id ON memory_jobs (canonical_person_id)",
        "CREATE INDEX ix_memory_jobs_canonical_space_id ON memory_jobs (canonical_space_id)",
        "CREATE INDEX ix_memory_jobs_conversation ON memory_jobs (conversation_key, id)",
        "CREATE INDEX ix_memory_jobs_status_next ON memory_jobs (status, next_attempt_at)",
    ),
    "memory_dream_clusters": (
        "CREATE INDEX ix_memory_dream_clusters_canonical_subject_person_id ON "
        "memory_dream_clusters (canonical_subject_person_id)",
        "CREATE INDEX ix_memory_dream_clusters_canonical_subject_space_id ON "
        "memory_dream_clusters (canonical_subject_space_id)",
        "CREATE INDEX ix_memory_dream_clusters_canonical_visibility_person_id "
        "ON memory_dream_clusters (canonical_visibility_person_id)",
        "CREATE INDEX ix_memory_dream_clusters_canonical_visibility_space_id ON "
        "memory_dream_clusters (canonical_visibility_space_id)",
        "CREATE INDEX ix_memory_dream_clusters_run_status ON "
        "memory_dream_clusters (run_id, status, id)",
    ),
    "automations": (
        "CREATE INDEX ix_automations_canonical_creator_person_id ON automations "
        "(canonical_creator_person_id)",
        "CREATE INDEX ix_automations_canonical_presence_id ON automations (canonical_presence_id)",
        "CREATE INDEX ix_automations_canonical_target_person_id ON automations "
        "(canonical_target_person_id)",
        "CREATE INDEX ix_automations_canonical_target_space_id ON automations "
        "(canonical_target_space_id)",
        "CREATE INDEX ix_automations_claim ON automations (claimed_until, claimed_by)",
        "CREATE INDEX ix_automations_creator_updated ON automations (creator_user_id, updated_at)",
        "CREATE INDEX ix_automations_status_next ON automations (status, next_run_at)",
    ),
    "plugin_config_values": (
        "CREATE INDEX ix_plugin_config_values_canonical_person_id ON "
        "plugin_config_values (canonical_person_id)",
        "CREATE INDEX ix_plugin_config_values_canonical_space_id ON "
        "plugin_config_values (canonical_space_id)",
        "CREATE INDEX ix_plugin_config_values_plugin_scope ON "
        "plugin_config_values (plugin_id, scope_type)",
        "CREATE UNIQUE INDEX uq_plugin_config_values_global_key ON "
        "plugin_config_values (plugin_id, \"key\") WHERE scope_type = 'global'",
        "CREATE UNIQUE INDEX uq_plugin_config_values_person_key ON "
        'plugin_config_values (plugin_id, canonical_person_id, "key") WHERE '
        "scope_type = 'user'",
        "CREATE UNIQUE INDEX uq_plugin_config_values_space_key ON "
        'plugin_config_values (plugin_id, canonical_space_id, "key") WHERE '
        "scope_type = 'group'",
    ),
    "plugin_state": (
        "CREATE INDEX ix_plugin_state_canonical_person_id ON plugin_state (canonical_person_id)",
        "CREATE INDEX ix_plugin_state_expires ON plugin_state (expires_at)",
        "CREATE INDEX ix_plugin_state_plugin_namespace ON plugin_state (plugin_id, namespace)",
    ),
    "plugin_agent_sessions": (
        "CREATE INDEX ix_plugin_agent_sessions_canonical_owner_person_id ON "
        "plugin_agent_sessions (canonical_owner_person_id)",
        "CREATE INDEX ix_plugin_agent_sessions_canonical_space_id ON "
        "plugin_agent_sessions (canonical_space_id)",
        "CREATE INDEX ix_plugin_agent_sessions_expires ON plugin_agent_sessions (expires_at)",
        "CREATE INDEX ix_plugin_agent_sessions_plugin_scope ON "
        "plugin_agent_sessions (plugin_id, scope_type)",
    ),
    "plugin_agent_messages": (
        "CREATE INDEX ix_plugin_agent_messages_canonical_sender_person_id ON "
        "plugin_agent_messages (canonical_sender_person_id)",
        "CREATE INDEX ix_plugin_agent_messages_sender ON plugin_agent_messages (sender_user_id)",
        "CREATE INDEX ix_plugin_agent_messages_session_created ON "
        "plugin_agent_messages (session_id, created_at)",
    ),
    "plugin_background_target_grants": (
        "CREATE INDEX ix_plugin_background_target_enabled ON "
        "plugin_background_target_grants (plugin_id, enabled)",
        "CREATE INDEX "
        "ix_plugin_background_target_grants_canonical_created_by_person_id "
        "ON plugin_background_target_grants "
        "(canonical_created_by_person_id)",
        "CREATE INDEX "
        "ix_plugin_background_target_grants_canonical_presence_id ON "
        "plugin_background_target_grants (canonical_presence_id)",
        "CREATE INDEX "
        "ix_plugin_background_target_grants_canonical_target_person_id "
        "ON plugin_background_target_grants "
        "(canonical_target_person_id)",
        "CREATE INDEX "
        "ix_plugin_background_target_grants_canonical_target_space_id "
        "ON plugin_background_target_grants "
        "(canonical_target_space_id)",
        "CREATE UNIQUE INDEX uq_plugin_background_target_person ON "
        "plugin_background_target_grants (plugin_id, "
        "canonical_target_person_id) WHERE target_type = 'private'",
        "CREATE UNIQUE INDEX uq_plugin_background_target_space ON "
        "plugin_background_target_grants (plugin_id, "
        "canonical_target_space_id) WHERE target_type = 'group'",
    ),
    "plugin_notification_outbox": (
        "CREATE INDEX "
        "ix_plugin_notification_outbox_canonical_conversation_id ON "
        "plugin_notification_outbox (canonical_conversation_id)",
        "CREATE INDEX ix_plugin_notification_outbox_canonical_presence_id "
        "ON plugin_notification_outbox (canonical_presence_id)",
        "CREATE INDEX "
        "ix_plugin_notification_outbox_canonical_target_person_id ON "
        "plugin_notification_outbox (canonical_target_person_id)",
        "CREATE INDEX "
        "ix_plugin_notification_outbox_canonical_target_space_id ON "
        "plugin_notification_outbox (canonical_target_space_id)",
        "CREATE INDEX ix_plugin_notification_outbox_due ON "
        "plugin_notification_outbox (status, next_attempt_at)",
        "CREATE INDEX ix_plugin_notification_outbox_plugin ON "
        "plugin_notification_outbox (plugin_id, status)",
        "CREATE INDEX ix_plugin_notification_outbox_source ON "
        "plugin_notification_outbox (source_event_id)",
    ),
    "plugin_background_turn_jobs": (
        "CREATE INDEX ix_plugin_background_turn_due ON "
        "plugin_background_turn_jobs (status, next_attempt_at)",
        "CREATE INDEX "
        "ix_plugin_background_turn_jobs_canonical_conversation_id ON "
        "plugin_background_turn_jobs (canonical_conversation_id)",
        "CREATE INDEX "
        "ix_plugin_background_turn_jobs_canonical_presence_id ON "
        "plugin_background_turn_jobs (canonical_presence_id)",
        "CREATE INDEX "
        "ix_plugin_background_turn_jobs_canonical_target_person_id ON "
        "plugin_background_turn_jobs (canonical_target_person_id)",
        "CREATE INDEX "
        "ix_plugin_background_turn_jobs_canonical_target_space_id ON "
        "plugin_background_turn_jobs (canonical_target_space_id)",
        "CREATE INDEX ix_plugin_background_turn_plugin ON "
        "plugin_background_turn_jobs (plugin_id, status)",
    ),
    "emoji_scope_states": (
        "CREATE INDEX ix_emoji_scope_lookup ON emoji_scope_states (scope_type, "
        "canonical_space_id, enabled)",
        "CREATE INDEX ix_emoji_scope_states_canonical_space_id ON "
        "emoji_scope_states (canonical_space_id)",
        "CREATE UNIQUE INDEX uq_emoji_scope_state_global ON emoji_scope_states "
        "(emoji_id) WHERE scope_type = 'global'",
        "CREATE UNIQUE INDEX uq_emoji_scope_state_space ON emoji_scope_states "
        "(emoji_id, canonical_space_id) WHERE scope_type = 'group'",
    ),
}

_COPY_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "identity_bindings": (
        "id",
        "person_id",
        "platform",
        "external_account_id",
        "display_name",
        "status",
        "revision",
        "first_seen_at",
        "last_seen_at",
        "created_at",
        "updated_at",
    ),
    "space_bindings": (
        "id",
        "space_id",
        "platform",
        "external_space_id",
        "display_name",
        "status",
        "revision",
        "first_seen_at",
        "last_seen_at",
        "created_at",
        "updated_at",
    ),
    "person_aliases": (
        "id",
        "alias",
        "alias_type",
        "first_seen_at",
        "last_seen_at",
        "canonical_person_id",
        "canonical_space_id",
    ),
    "memberships": (
        "group_card",
        "first_seen_at",
        "last_seen_at",
        "canonical_person_id",
        "canonical_space_id",
    ),
    "person_relationships": (
        "canonical_person_id",
        "affection_score",
        "trust_score",
        "created_at",
        "updated_at",
        "last_automatic_change_at",
    ),
    "relationship_events": (
        "id",
        "source_event_id",
        "actor_user_id",
        "change_type",
        "affection_before",
        "affection_delta",
        "affection_after",
        "trust_before",
        "trust_delta",
        "trust_after",
        "reason_code",
        "confidence",
        "created_at",
        "canonical_person_id",
    ),
    "relationship_jobs": (
        "id",
        "trigger_event_id",
        "conversation_key",
        "status",
        "attempts",
        "next_attempt_at",
        "error_category",
        "created_at",
        "updated_at",
        "canonical_person_id",
    ),
    "person_time_settings": ("canonical_person_id", "timezone", "created_at", "updated_at"),
    "person_speech_preferences": (
        "canonical_person_id",
        "mode",
        "source_message_id",
        "created_at",
        "updated_at",
    ),
    "runtime_config_overrides": (
        "id",
        "config_key",
        "scope_type",
        "value_json",
        "value_type",
        "apply_mode",
        "version",
        "created_at",
        "updated_at",
        "updated_by",
        "canonical_person_id",
        "canonical_space_id",
    ),
    "chat_events": (
        "id",
        "bot_user_id",
        "platform_message_id",
        "scope_type",
        "group_id",
        "private_peer_user_id",
        "sender_user_id",
        "sender_nickname",
        "sender_group_card",
        "direction",
        "event_kind",
        "source_plugin_id",
        "external_source",
        "external_event_key",
        "external_event_type",
        "external_payload_json",
        "external_target_id",
        "content",
        "visual_summary",
        "segments_json",
        "reply_to_message_id",
        "origin",
        "automation_id",
        "automation_run_id",
        "occurred_at",
        "observed_at",
        "canonical_event_id",
        "canonical_conversation_id",
        "author_kind",
        "author_person_id",
        "author_presence_id",
        "ingress_presence_id",
        "utterance_fingerprint",
        "suppression_status",
        "ingress_provider",
        "ingress_gateway_instance_id",
    ),
    "memory_facts": (
        "id",
        "scope_type",
        "visibility_type",
        "kind",
        "memory_key",
        "category",
        "content",
        "normalized_content",
        "importance",
        "confidence",
        "source_type",
        "authority",
        "status",
        "conflict_state",
        "supersedes_id",
        "valid_from",
        "valid_until",
        "created_at",
        "updated_at",
        "last_confirmed_at",
        "invalidated_reason",
        "last_injected_at",
        "validation_version",
        "last_audited_at",
        "review_state",
        "canonical_subject_person_id",
        "canonical_subject_space_id",
        "canonical_visibility_person_id",
        "canonical_visibility_space_id",
    ),
    "memory_tool_receipts": (
        "id",
        "conversation_key_hash",
        "trigger_event_id",
        "bot_user_id",
        "canonical_person_id",
        "canonical_space_id",
        "provider_id",
        "tool_name",
        "success",
        "result_excerpt",
        "result_characters",
        "error_category",
        "created_at",
        "expires_at",
    ),
    "memory_self_reflection_states": (
        "id",
        "conversation_key_hash",
        "bot_user_id",
        "canonical_person_id",
        "canonical_space_id",
        "last_event_id",
        "latest_event_id",
        "pending_events",
        "pending_characters",
        "pending_since",
        "has_yuki_reply",
        "has_tool_result",
        "high_value_signal",
        "updated_at",
    ),
    "memory_self_reflection_runs": (
        "id",
        "conversation_key_hash",
        "bot_user_id",
        "canonical_person_id",
        "canonical_space_id",
        "scheduled_slot",
        "trigger_reason",
        "first_event_id",
        "last_event_id",
        "status",
        "proposal_count",
        "committed_count",
        "error_category",
        "started_at",
        "completed_at",
    ),
    "memory_jobs": (
        "id",
        "event_id",
        "conversation_key",
        "canonical_person_id",
        "canonical_space_id",
        "status",
        "attempts",
        "next_attempt_at",
        "created_at",
        "updated_at",
        "error_category",
        "processing_source",
        "rebuild_run_id",
        "outcome",
        "completed_at",
    ),
    "memory_dream_clusters": (
        "id",
        "run_id",
        "cluster_key",
        "partition_key",
        "bot_user_id",
        "canonical_subject_person_id",
        "canonical_subject_space_id",
        "canonical_visibility_person_id",
        "canonical_visibility_space_id",
        "kind",
        "status",
        "fact_ids_json",
        "fingerprint",
        "attempts",
        "model_calls",
        "operation_count",
        "error_category",
        "created_at",
        "updated_at",
        "completed_at",
    ),
    "automations": (
        "id",
        "creator_user_id",
        "bot_user_id",
        "name",
        "status",
        "timezone",
        "schedule_json",
        "script_json",
        "script_hash",
        "required_capabilities_json",
        "authority_snapshot_json",
        "created_from_message_id",
        "next_run_at",
        "last_run_at",
        "run_count",
        "max_runs",
        "consecutive_failures",
        "misfire_grace_seconds",
        "claimed_by",
        "claimed_until",
        "created_at",
        "updated_at",
        "canonical_creator_person_id",
        "canonical_target_person_id",
        "canonical_target_space_id",
        "canonical_presence_id",
    ),
    "plugin_config_values": (
        "id",
        "plugin_id",
        "scope_type",
        "key",
        "value_json",
        "version",
        "updated_at",
        "canonical_person_id",
        "canonical_space_id",
    ),
    "plugin_state": (
        "id",
        "plugin_id",
        "namespace",
        "key",
        "value_json",
        "version",
        "expires_at",
        "updated_at",
        "canonical_person_id",
    ),
    "plugin_agent_sessions": (
        "session_id",
        "plugin_id",
        "scope_type",
        "name",
        "model",
        "instructions",
        "persistence",
        "context_profile",
        "allowed_capabilities_json",
        "status",
        "next_sequence",
        "turn_count",
        "created_at",
        "updated_at",
        "last_active_at",
        "expires_at",
        "canonical_owner_person_id",
        "canonical_space_id",
    ),
    "plugin_agent_messages": (
        "id",
        "session_id",
        "sequence",
        "role",
        "sender_user_id",
        "content",
        "metadata_json",
        "created_at",
        "canonical_sender_person_id",
    ),
    "plugin_background_target_grants": (
        "id",
        "plugin_id",
        "target_type",
        "target_id",
        "bot_user_id",
        "enabled",
        "created_by_user_id",
        "created_at",
        "updated_at",
        "canonical_target_person_id",
        "canonical_target_space_id",
        "canonical_created_by_person_id",
        "canonical_presence_id",
    ),
    "plugin_notification_outbox": (
        "id",
        "notification_id",
        "part_key",
        "source_event_id",
        "plugin_id",
        "target_type",
        "target_id",
        "bot_user_id",
        "part_type",
        "text",
        "media_handle_id",
        "status",
        "attempts",
        "max_attempts",
        "next_attempt_at",
        "lease_until",
        "platform_message_id",
        "last_error_category",
        "created_at",
        "updated_at",
        "sent_at",
        "canonical_target_person_id",
        "canonical_target_space_id",
        "canonical_conversation_id",
        "canonical_presence_id",
    ),
    "plugin_background_turn_jobs": (
        "id",
        "source_event_id",
        "plugin_id",
        "target_type",
        "target_id",
        "bot_user_id",
        "agent_intent",
        "status",
        "attempts",
        "max_attempts",
        "next_attempt_at",
        "lease_until",
        "generated_text",
        "tool_calls_used",
        "model_requests",
        "last_error_category",
        "created_at",
        "updated_at",
        "completed_at",
        "canonical_target_person_id",
        "canonical_target_space_id",
        "canonical_conversation_id",
        "canonical_presence_id",
    ),
    "emoji_scope_states": (
        "id",
        "emoji_id",
        "scope_type",
        "enabled",
        "weight",
        "adopted_at",
        "updated_at",
        "canonical_space_id",
    ),
}

_HISTORICAL_TRIGGER_NAMES: Final[tuple[str, ...]] = (
    "chat_events_fts_ad",
    "chat_events_fts_ai",
    "chat_events_fts_au",
    "ck_chat_events_kind_payload_insert",
    "ck_chat_events_kind_payload_update",
    "memory_facts_fts_ad",
    "memory_facts_fts_ai",
    "memory_facts_fts_au",
    "trg_automations_extension_shadow_insert",
    "trg_automations_extension_shadow_update",
    "trg_canonical_conversations_primary_alias_immutable",
    "trg_chat_events_canonical_shadow_insert",
    "trg_chat_events_canonical_shadow_update",
    "trg_conversation_legacy_aliases_primary_delete",
    "trg_conversation_legacy_aliases_primary_update",
    "trg_conversation_scopes_canonical_shadow_insert",
    "trg_conversation_scopes_canonical_shadow_update",
    "trg_emoji_assets_extension_shadow_insert",
    "trg_emoji_assets_extension_shadow_update",
    "trg_emoji_scope_states_extension_shadow_insert",
    "trg_emoji_scope_states_extension_shadow_update",
    "trg_emoji_usage_events_extension_shadow_insert",
    "trg_emoji_usage_events_extension_shadow_update",
    "trg_groups_ownership_shadow_insert",
    "trg_groups_ownership_shadow_update",
    "trg_identity_bindings_route_consistency_update",
    "trg_memberships_ownership_shadow_insert",
    "trg_memberships_ownership_shadow_update",
    "trg_memory_dream_clusters_memory_owner_insert",
    "trg_memory_dream_clusters_memory_owner_update",
    "trg_memory_facts_ownership_shadow_insert",
    "trg_memory_facts_ownership_shadow_update",
    "trg_memory_jobs_memory_owner_insert",
    "trg_memory_jobs_memory_owner_update",
    "trg_memory_self_reflection_runs_memory_owner_insert",
    "trg_memory_self_reflection_runs_memory_owner_update",
    "trg_memory_self_reflection_states_memory_owner_insert",
    "trg_memory_self_reflection_states_memory_owner_update",
    "trg_memory_tool_receipts_memory_owner_insert",
    "trg_memory_tool_receipts_memory_owner_update",
    "trg_model_invocations_extension_shadow_insert",
    "trg_model_invocations_extension_shadow_update",
    "trg_people_ownership_shadow_insert",
    "trg_people_ownership_shadow_update",
    "trg_person_active_routes_consistency_insert",
    "trg_person_active_routes_consistency_update",
    "trg_person_aliases_ownership_shadow_insert",
    "trg_person_aliases_ownership_shadow_update",
    "trg_person_relationships_ownership_shadow_insert",
    "trg_person_relationships_ownership_shadow_update",
    "trg_person_speech_preferences_ownership_shadow_insert",
    "trg_person_speech_preferences_ownership_shadow_update",
    "trg_person_time_settings_ownership_shadow_insert",
    "trg_person_time_settings_ownership_shadow_update",
    "trg_plugin_agent_messages_extension_shadow_insert",
    "trg_plugin_agent_messages_extension_shadow_update",
    "trg_plugin_agent_sessions_extension_shadow_insert",
    "trg_plugin_agent_sessions_extension_shadow_update",
    "trg_plugin_background_target_grants_extension_shadow_insert",
    "trg_plugin_background_target_grants_extension_shadow_update",
    "trg_plugin_background_turn_jobs_extension_shadow_insert",
    "trg_plugin_background_turn_jobs_extension_shadow_update",
    "trg_plugin_config_values_extension_shadow_insert",
    "trg_plugin_config_values_extension_shadow_update",
    "trg_plugin_notification_outbox_extension_shadow_insert",
    "trg_plugin_notification_outbox_extension_shadow_update",
    "trg_plugin_state_extension_shadow_insert",
    "trg_plugin_state_extension_shadow_update",
    "trg_presences_route_consistency_update",
    "trg_relationship_events_ownership_shadow_insert",
    "trg_relationship_events_ownership_shadow_update",
    "trg_relationship_jobs_ownership_shadow_insert",
    "trg_relationship_jobs_ownership_shadow_update",
    "trg_reply_effect_events_extension_shadow_insert",
    "trg_reply_effect_events_extension_shadow_update",
    "trg_runtime_config_overrides_extension_shadow_insert",
    "trg_runtime_config_overrides_extension_shadow_update",
    "trg_runtime_turn_observations_extension_shadow_insert",
    "trg_runtime_turn_observations_extension_shadow_update",
    "trg_space_active_routes_consistency_insert",
    "trg_space_active_routes_consistency_update",
    "trg_space_binding_ingest_routes_consistency_insert",
    "trg_space_binding_ingest_routes_consistency_update",
    "trg_space_bindings_route_consistency_update",
    "trg_speech_generations_extension_shadow_insert",
    "trg_speech_generations_extension_shadow_update",
    "trg_tool_invocations_extension_shadow_insert",
    "trg_tool_invocations_extension_shadow_update",
    "trg_web_search_runs_extension_shadow_insert",
    "trg_web_search_runs_extension_shadow_update",
)

_FINAL_TRIGGER_DDL: Final[tuple[str, ...]] = (
    "CREATE TRIGGER chat_events_fts_ad\n"
    "            AFTER DELETE ON chat_events BEGIN\n"
    "                INSERT INTO chat_events_fts(chat_events_fts, rowid, content)\n"
    "                VALUES ('delete', old.id, old.content);\n"
    "            END",
    "CREATE TRIGGER chat_events_fts_ai\n"
    "            AFTER INSERT ON chat_events BEGIN\n"
    "                INSERT INTO chat_events_fts(rowid, content) VALUES (new.id, new.content);\n"
    "            END",
    "CREATE TRIGGER chat_events_fts_au\n"
    "            AFTER UPDATE OF content ON chat_events BEGIN\n"
    "                INSERT INTO chat_events_fts(chat_events_fts, rowid, content)\n"
    "                VALUES ('delete', old.id, old.content);\n"
    "                INSERT INTO chat_events_fts(rowid, content) VALUES (new.id, new.content);\n"
    "            END",
    "CREATE TRIGGER memory_facts_fts_ad\n"
    "            AFTER DELETE ON memory_facts BEGIN\n"
    "                INSERT INTO memory_facts_fts(\n"
    "                    memory_facts_fts, rowid, content, memory_key, category\n"
    "                ) VALUES ('delete', old.id, old.content, old.memory_key, old.category);\n"
    "            END",
    "CREATE TRIGGER memory_facts_fts_ai\n"
    "            AFTER INSERT ON memory_facts BEGIN\n"
    "                INSERT INTO memory_facts_fts(rowid, content, memory_key, category)\n"
    "                VALUES (new.id, new.content, new.memory_key, new.category);\n"
    "            END",
    "CREATE TRIGGER memory_facts_fts_au\n"
    "            AFTER UPDATE OF content, memory_key, category ON memory_facts BEGIN\n"
    "                INSERT INTO memory_facts_fts(\n"
    "                    memory_facts_fts, rowid, content, memory_key, category\n"
    "                ) VALUES ('delete', old.id, old.content, old.memory_key, old.category);\n"
    "                INSERT INTO memory_facts_fts(rowid, content, memory_key, category)\n"
    "                VALUES (new.id, new.content, new.memory_key, new.category);\n"
    "            END",
    "CREATE TRIGGER trg_canonical_conversations_primary_alias_immutable\n"
    "BEFORE UPDATE OF primary_alias_id, primary_marker ON canonical_conversations\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'canonical conversation primary alias pointer is immutable')\n"
    "    WHERE NEW.primary_alias_id IS NOT OLD.primary_alias_id\n"
    "       OR NEW.primary_marker IS NOT OLD.primary_marker;\n"
    "END",
    "CREATE TRIGGER trg_conversation_legacy_aliases_primary_delete\n"
    "BEFORE DELETE ON conversation_legacy_aliases\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'pinned primary alias cannot be deleted')\n"
    "    WHERE EXISTS (\n"
    "        SELECT 1 FROM canonical_conversations\n"
    "        WHERE id = OLD.conversation_id AND primary_alias_id = OLD.id\n"
    "    );\n"
    "END",
    "CREATE TRIGGER trg_conversation_legacy_aliases_primary_update\n"
    "BEFORE UPDATE ON conversation_legacy_aliases\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'pinned primary alias cannot be changed')\n"
    "    WHERE EXISTS (\n"
    "        SELECT 1 FROM canonical_conversations\n"
    "        WHERE id = OLD.conversation_id AND primary_alias_id = OLD.id\n"
    "    )\n"
    "    AND (\n"
    "        NEW.id IS NOT OLD.id\n"
    "        OR NEW.conversation_id IS NOT OLD.conversation_id\n"
    "        OR NEW.is_primary IS NOT OLD.is_primary\n"
    "        OR NEW.scope_key IS NOT OLD.scope_key\n"
    "    );\n"
    "END",
    "CREATE TRIGGER trg_identity_bindings_route_consistency_update\n"
    "BEFORE UPDATE OF person_id, platform ON identity_bindings\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'identity binding update would break person active route')\n"
    "    WHERE EXISTS (\n"
    "        SELECT 1\n"
    "        FROM person_active_routes AS route\n"
    "        JOIN presences AS presence ON presence.id = route.presence_id\n"
    "        WHERE route.identity_binding_id = NEW.id\n"
    "          AND (\n"
    "            route.person_id IS NOT NEW.person_id\n"
    "            OR NEW.platform IS NOT presence.platform\n"
    "          )\n"
    "    );\n"
    "END",
    "CREATE TRIGGER trg_person_active_routes_consistency_insert\n"
    "BEFORE INSERT ON person_active_routes\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'person active route ownership or platform mismatch')\n"
    "    WHERE NOT EXISTS (\n"
    "        SELECT 1\n"
    "        FROM identity_bindings AS binding\n"
    "        JOIN presences AS presence ON presence.id = NEW.presence_id\n"
    "        WHERE binding.id = NEW.identity_binding_id\n"
    "          AND binding.person_id = NEW.person_id\n"
    "          AND binding.platform = presence.platform\n"
    "    );\n"
    "END",
    "CREATE TRIGGER trg_person_active_routes_consistency_update\n"
    "BEFORE UPDATE ON person_active_routes\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'person active route ownership or platform mismatch')\n"
    "    WHERE NOT EXISTS (\n"
    "        SELECT 1\n"
    "        FROM identity_bindings AS binding\n"
    "        JOIN presences AS presence ON presence.id = NEW.presence_id\n"
    "        WHERE binding.id = NEW.identity_binding_id\n"
    "          AND binding.person_id = NEW.person_id\n"
    "          AND binding.platform = presence.platform\n"
    "    );\n"
    "END",
    "CREATE TRIGGER trg_presences_route_consistency_update\n"
    "BEFORE UPDATE OF platform ON presences\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'presence platform update would break routes')\n"
    "    WHERE EXISTS (\n"
    "        SELECT 1\n"
    "        FROM person_active_routes AS route\n"
    "        JOIN identity_bindings AS binding ON binding.id = route.identity_binding_id\n"
    "        WHERE route.presence_id = NEW.id\n"
    "          AND binding.platform IS NOT NEW.platform\n"
    "    )\n"
    "    OR EXISTS (\n"
    "        SELECT 1\n"
    "        FROM space_binding_ingest_routes AS route\n"
    "        JOIN space_bindings AS binding ON binding.id = route.space_binding_id\n"
    "        WHERE route.ingest_presence_id = NEW.id\n"
    "          AND binding.platform IS NOT NEW.platform\n"
    "    )\n"
    "    OR EXISTS (\n"
    "        SELECT 1\n"
    "        FROM space_active_routes AS route\n"
    "        JOIN space_bindings AS binding ON binding.id = route.space_binding_id\n"
    "        WHERE route.presence_id = NEW.id\n"
    "          AND binding.platform IS NOT NEW.platform\n"
    "    );\n"
    "END",
    "CREATE TRIGGER trg_space_active_routes_consistency_insert\n"
    "BEFORE INSERT ON space_active_routes\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'space active route ownership or platform mismatch')\n"
    "    WHERE NOT EXISTS (\n"
    "        SELECT 1\n"
    "        FROM space_bindings AS binding\n"
    "        JOIN presences AS presence ON presence.id = NEW.presence_id\n"
    "        WHERE binding.id = NEW.space_binding_id\n"
    "          AND binding.space_id = NEW.space_id\n"
    "          AND binding.platform = presence.platform\n"
    "    );\n"
    "END",
    "CREATE TRIGGER trg_space_active_routes_consistency_update\n"
    "BEFORE UPDATE ON space_active_routes\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'space active route ownership or platform mismatch')\n"
    "    WHERE NOT EXISTS (\n"
    "        SELECT 1\n"
    "        FROM space_bindings AS binding\n"
    "        JOIN presences AS presence ON presence.id = NEW.presence_id\n"
    "        WHERE binding.id = NEW.space_binding_id\n"
    "          AND binding.space_id = NEW.space_id\n"
    "          AND binding.platform = presence.platform\n"
    "    );\n"
    "END",
    "CREATE TRIGGER trg_space_binding_ingest_routes_consistency_insert\n"
    "BEFORE INSERT ON space_binding_ingest_routes\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'space binding ingest route platform mismatch')\n"
    "    WHERE NOT EXISTS (\n"
    "        SELECT 1\n"
    "        FROM space_bindings AS binding\n"
    "        JOIN presences AS presence ON presence.id = NEW.ingest_presence_id\n"
    "        WHERE binding.id = NEW.space_binding_id\n"
    "          AND binding.platform = presence.platform\n"
    "    );\n"
    "END",
    "CREATE TRIGGER trg_space_binding_ingest_routes_consistency_update\n"
    "BEFORE UPDATE ON space_binding_ingest_routes\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'space binding ingest route platform mismatch')\n"
    "    WHERE NOT EXISTS (\n"
    "        SELECT 1\n"
    "        FROM space_bindings AS binding\n"
    "        JOIN presences AS presence ON presence.id = NEW.ingest_presence_id\n"
    "        WHERE binding.id = NEW.space_binding_id\n"
    "          AND binding.platform = presence.platform\n"
    "    );\n"
    "END",
    "CREATE TRIGGER trg_space_bindings_route_consistency_update\n"
    "BEFORE UPDATE OF space_id, platform ON space_bindings\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'space binding update would break space routes')\n"
    "    WHERE EXISTS (\n"
    "        SELECT 1\n"
    "        FROM space_binding_ingest_routes AS route\n"
    "        JOIN presences AS presence ON presence.id = route.ingest_presence_id\n"
    "        WHERE route.space_binding_id = NEW.id\n"
    "          AND NEW.platform IS NOT presence.platform\n"
    "    )\n"
    "    OR EXISTS (\n"
    "        SELECT 1\n"
    "        FROM space_active_routes AS route\n"
    "        JOIN presences AS presence ON presence.id = route.presence_id\n"
    "        WHERE route.space_binding_id = NEW.id\n"
    "          AND (\n"
    "            route.space_id IS NOT NEW.space_id\n"
    "            OR NEW.platform IS NOT presence.platform\n"
    "          )\n"
    "    );\n"
    "END",
)

_FTS_TABLE_DDL: Final[dict[str, str]] = {
    "chat_events_fts": "CREATE VIRTUAL TABLE chat_events_fts USING fts5(\n"
    "                content,\n"
    "                content='chat_events',\n"
    "                content_rowid='id',\n"
    "                tokenize='trigram'\n"
    "            )",
    "memory_facts_fts": "CREATE VIRTUAL TABLE memory_facts_fts USING fts5(\n"
    "                content,\n"
    "                memory_key,\n"
    "                category,\n"
    "                content='memory_facts',\n"
    "                content_rowid='id',\n"
    "                tokenize='trigram'\n"
    "            )",
}


def _temporary_table_ddl(table: str) -> tuple[str, str]:
    temporary = f"__0049_{table}"
    statement = _TABLE_DDL[table]
    body = statement[statement.index("(") :]
    return temporary, f"CREATE TABLE {_quote(temporary)} {body}"


def _copy_aliases(connection: Connection, temporary: str) -> None:
    connection.exec_driver_sql(
        f"INSERT INTO {_quote(temporary)} "
        "(id, alias, alias_type, first_seen_at, last_seen_at, "
        "canonical_person_id, canonical_space_id) "
        "SELECT MIN(id), alias, MIN(alias_type), MIN(first_seen_at), MAX(last_seen_at), "
        "canonical_person_id, canonical_space_id FROM person_aliases "
        "GROUP BY canonical_person_id, canonical_space_id, alias"
    )


def _copy_memberships(connection: Connection, temporary: str) -> None:
    connection.exec_driver_sql(
        f"INSERT INTO {_quote(temporary)} "
        "(group_card, first_seen_at, last_seen_at, canonical_person_id, canonical_space_id) "
        "SELECT COALESCE((SELECT m2.group_card FROM memberships m2 "
        "WHERE m2.canonical_person_id = m.canonical_person_id "
        "AND m2.canonical_space_id = m.canonical_space_id "
        "ORDER BY (m2.group_card <> '') DESC, m2.last_seen_at DESC, "
        "m2.group_card, m2.first_seen_at LIMIT 1), ''), "
        "MIN(m.first_seen_at), MAX(m.last_seen_at), "
        "m.canonical_person_id, m.canonical_space_id FROM memberships m "
        "GROUP BY m.canonical_person_id, m.canonical_space_id"
    )


def _copy_table(connection: Connection, table: str, temporary: str) -> None:
    if table == "person_aliases":
        _copy_aliases(connection, temporary)
        return
    if table == "memberships":
        _copy_memberships(connection, temporary)
        return
    columns = _COPY_COLUMNS[table]
    projection = ", ".join(_quote(column) for column in columns)
    connection.exec_driver_sql(
        f"INSERT INTO {_quote(temporary)} ({projection}) SELECT {projection} FROM {_quote(table)}"
    )


def _capture_sequences(connection: Connection) -> dict[str, int]:
    if "sqlite_sequence" not in _tables(connection):
        return {}
    return {
        str(row[0]): int(row[1])
        for row in connection.exec_driver_sql(
            "SELECT name, seq FROM sqlite_sequence WHERE name IN ("
            + ", ".join("?" for _ in _REBUILD_TABLES)
            + ")",
            _REBUILD_TABLES,
        )
    }


def _restore_sequences(connection: Connection, sequences: dict[str, int]) -> None:
    if not sequences:
        return
    for table, sequence in sequences.items():
        connection.exec_driver_sql("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
        connection.exec_driver_sql(
            "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
            (table, sequence),
        )


def _rebuild_canonical_tables(connection: Connection) -> None:
    """Rebuild changed tables using frozen, explicit 3.8 mappings."""

    sequences = _capture_sequences(connection)
    for trigger in _HISTORICAL_TRIGGER_NAMES:
        connection.exec_driver_sql(f"DROP TRIGGER {_quote(trigger)}")
    connection.exec_driver_sql("DROP TABLE chat_events_fts")
    connection.exec_driver_sql("DROP TABLE memory_facts_fts")

    for table in _REBUILD_TABLES:
        temporary, statement = _temporary_table_ddl(table)
        connection.exec_driver_sql(statement)
        _copy_table(connection, table, temporary)
        connection.exec_driver_sql(f"DROP TABLE {_quote(table)}")
        connection.exec_driver_sql(f"ALTER TABLE {_quote(temporary)} RENAME TO {_quote(table)}")
        for index_statement in _TABLE_INDEX_DDL[table]:
            connection.exec_driver_sql(index_statement)

    for statement in _FTS_TABLE_DDL.values():
        connection.exec_driver_sql(statement)
    for statement in _FINAL_TRIGGER_DDL:
        connection.exec_driver_sql(statement)
    connection.exec_driver_sql("INSERT INTO chat_events_fts(chat_events_fts) VALUES ('rebuild')")
    connection.exec_driver_sql("INSERT INTO memory_facts_fts(memory_facts_fts) VALUES ('rebuild')")
    _restore_sequences(connection, sequences)


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        raise CanonicalBridgeError("unsupported_database")
    tables = _tables(connection)
    historical = _HISTORICAL_MARKERS.issubset(tables)
    final = _FINAL_MARKERS.issubset(tables) and not tables.intersection(_RETIRED_TABLES)
    if historical and final:
        # This is the expected historical 0048 shape: canonical tables coexist
        # with retired carriers until this bridge commits.
        final = False
    if historical:
        _upgrade_historical_0048(connection, tables)
        return
    if final:
        _validate_database(connection, tables)
        return
    raise CanonicalBridgeError("unsupported_0048_shape")


def downgrade() -> None:
    raise CanonicalBridgeError("0049_has_no_downgrade_restore_snapshot")
