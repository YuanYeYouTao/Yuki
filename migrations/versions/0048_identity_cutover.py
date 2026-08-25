"""Identity cutover tables and drop legacy people/groups carrier FKs.

Revision ID: 0048
Revises: 0047
Create Date: 2026-08-24

This revision is frozen and self-contained. It must not import application
modules or create tables from current ORM metadata.

It records cutover run/manifest tables, then rebuilds every business table
that still required a people/groups carrier row so v2 can write canonical
owners without inserting those legacy rows. conversation_scopes.id children
are unchanged. Historical people/groups/conversation_scopes rows stay.

Downgrade is a schema-only reverse allowed only while identity_runtime_state
is v1 and no succeeded apply record exists. It rebuilds every affected table
back to the frozen 0047 people/groups carrier-FK schema, restores the full
legacy uq_chat_events_bot_platform_message table constraint, then drops
0048-owned tables. It is not a post-cutover data rollback; after apply,
restore the DB/WAL/SHM snapshots recorded by --plan.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEX64 = "[0-9a-f]" * 64
_UUID4_GLOB = (
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-"
    "[89ab][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
)


def _uuid4_sql(column: str) -> str:
    return f"length({column}) = 36 AND {column} = lower({column}) AND {column} GLOB '{_UUID4_GLOB}'"


_REBUILD_TABLES: tuple[str, ...] = (
    "automations",
    "chat_events",
    "conversation_scopes",
    "emoji_assets",
    "memberships",
    "memory_dream_runs",
    "memory_evidence",
    "memory_fact_state_events",
    "memory_facts",
    "memory_mutation_receipts",
    "memory_rebuild_proposals",
    "memory_rebuild_runs",
    "memory_tool_receipts",
    "person_aliases",
    "person_relationships",
    "person_speech_preferences",
    "person_time_settings",
    "plugin_agent_messages",
    "plugin_agent_sessions",
    "plugin_background_target_grants",
    "plugin_state",
    "relationship_events",
    "relationship_jobs",
)

# Frozen from a 0047 PRAGMA foreign_key_list + sqlite_master dump.
# (table, local, parent, remote, on_update, on_delete, clause)
# Do not infer ON UPDATE / ON DELETE defaults at migration runtime.
_CARRIER_FOREIGN_KEYS: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    (
        "automations",
        "creator_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(creator_user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "chat_events",
        "group_id",
        "groups",
        "group_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(group_id) REFERENCES groups (group_id) ON DELETE CASCADE",
    ),
    (
        "chat_events",
        "private_peer_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(private_peer_user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "chat_events",
        "sender_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(sender_user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "conversation_scopes",
        "bot_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(bot_user_id) REFERENCES people(user_id) ON DELETE CASCADE",
    ),
    (
        "conversation_scopes",
        "group_id",
        "groups",
        "group_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(group_id) REFERENCES groups(group_id) ON DELETE CASCADE",
    ),
    (
        "conversation_scopes",
        "private_peer_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(private_peer_user_id) REFERENCES people(user_id) ON DELETE CASCADE",
    ),
    (
        "emoji_assets",
        "first_seen_group_id",
        "groups",
        "group_id",
        "NO ACTION",
        "SET NULL",
        "FOREIGN KEY(first_seen_group_id) REFERENCES groups (group_id) ON DELETE SET NULL",
    ),
    (
        "emoji_assets",
        "first_seen_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "SET NULL",
        "FOREIGN KEY(first_seen_user_id) REFERENCES people (user_id) ON DELETE SET NULL",
    ),
    (
        "memberships",
        "group_id",
        "groups",
        "group_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(group_id) REFERENCES groups (group_id) ON DELETE CASCADE",
    ),
    (
        "memberships",
        "user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "memory_dream_runs",
        "created_by_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "SET NULL",
        "FOREIGN KEY(created_by_user_id) REFERENCES people (user_id) ON DELETE SET NULL",
    ),
    (
        "memory_evidence",
        "source_speaker_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(source_speaker_user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "memory_fact_state_events",
        "actor_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "SET NULL",
        "FOREIGN KEY(actor_user_id) REFERENCES people(user_id) ON DELETE SET NULL",
    ),
    (
        "memory_facts",
        "group_id",
        "groups",
        "group_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(group_id) REFERENCES groups (group_id) ON DELETE CASCADE",
    ),
    (
        "memory_facts",
        "subject_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(subject_user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "memory_mutation_receipts",
        "current_group_id",
        "groups",
        "group_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(current_group_id) REFERENCES groups (group_id) ON DELETE CASCADE",
    ),
    (
        "memory_mutation_receipts",
        "executed_by_bot_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "SET NULL",
        "FOREIGN KEY(executed_by_bot_user_id) REFERENCES people (user_id) ON DELETE SET NULL",
    ),
    (
        "memory_mutation_receipts",
        "trigger_actor_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(trigger_actor_user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "memory_rebuild_proposals",
        "group_id",
        "groups",
        "group_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(group_id) REFERENCES groups (group_id) ON DELETE CASCADE",
    ),
    (
        "memory_rebuild_proposals",
        "reviewed_by_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "SET NULL",
        "FOREIGN KEY(reviewed_by_user_id) REFERENCES people (user_id) ON DELETE SET NULL",
    ),
    (
        "memory_rebuild_proposals",
        "subject_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(subject_user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "memory_rebuild_runs",
        "created_by_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "SET NULL",
        "FOREIGN KEY(created_by_user_id) REFERENCES people (user_id) ON DELETE SET NULL",
    ),
    (
        "memory_tool_receipts",
        "bot_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(bot_user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "person_aliases",
        "user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "person_relationships",
        "user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "person_speech_preferences",
        "user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "person_time_settings",
        "user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "plugin_agent_messages",
        "sender_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(sender_user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "plugin_agent_sessions",
        "owner_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(owner_user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "plugin_background_target_grants",
        "created_by_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "RESTRICT",
        "FOREIGN KEY(created_by_user_id) REFERENCES people (user_id) ON DELETE RESTRICT",
    ),
    (
        "plugin_state",
        "subject_user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(subject_user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "relationship_events",
        "user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
    (
        "relationship_jobs",
        "user_id",
        "people",
        "user_id",
        "NO ACTION",
        "CASCADE",
        "FOREIGN KEY(user_id) REFERENCES people (user_id) ON DELETE CASCADE",
    ),
)
_LEGACY_CHAT_EVENTS_UNIQUE = (
    "CONSTRAINT uq_chat_events_bot_platform_message UNIQUE (bot_user_id, platform_message_id)"
)

_ON_ACTIONS = (
    r"(?:\s+ON\s+(?:DELETE|UPDATE)\s+"
    r"(?:SET\s+NULL|SET\s+DEFAULT|CASCADE|RESTRICT|NO\s+ACTION))*"
)
_NAMED_TABLE_FK = re.compile(
    r",?\s*CONSTRAINT\s+\S+\s+FOREIGN KEY\s*\([^)]+\)\s*REFERENCES\s+"
    rf"(?:people|groups)\s*\([^)]+\){_ON_ACTIONS}",
    re.IGNORECASE,
)
_TABLE_FK = re.compile(
    r",?\s*FOREIGN KEY\s*\([^)]+\)\s*REFERENCES\s+(?:people|groups)\s*\([^)]+\)"
    rf"{_ON_ACTIONS}",
    re.IGNORECASE,
)
_COLUMN_REF = re.compile(
    rf"\s+REFERENCES\s+(?:people|groups)\s*\([^)]+\){_ON_ACTIONS}",
    re.IGNORECASE,
)
_DANGLING_FK = re.compile(
    r",?\s*(?:CONSTRAINT\s+\S+\s+)?FOREIGN KEY\s*\([^)]+\)\s*(?=,|\))",
    re.IGNORECASE,
)
_NAMED_LEGACY_UNIQUE = re.compile(
    r",?\s*CONSTRAINT\s+[\"']?uq_chat_events_bot_platform_message[\"']?"
    r"\s+UNIQUE\s*\(\s*bot_user_id\s*,\s*platform_message_id\s*\)",
    re.IGNORECASE,
)
_BARE_LEGACY_UNIQUE = re.compile(
    r",?\s*UNIQUE\s*\(\s*bot_user_id\s*,\s*platform_message_id\s*\)",
    re.IGNORECASE,
)
_DOUBLE_COMMA = re.compile(r",\s*,")

_CHAT_EVENTS_FTS = """
CREATE VIRTUAL TABLE chat_events_fts USING fts5(
    content,
    content='chat_events',
    content_rowid='id',
    tokenize='trigram'
)
"""
_CHAT_EVENTS_FTS_TRIGGERS: tuple[str, ...] = (
    """
    CREATE TRIGGER chat_events_fts_ai AFTER INSERT ON chat_events BEGIN
        INSERT INTO chat_events_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
    """
    CREATE TRIGGER chat_events_fts_ad AFTER DELETE ON chat_events BEGIN
        INSERT INTO chat_events_fts(chat_events_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
    END
    """,
    """
    CREATE TRIGGER chat_events_fts_au AFTER UPDATE OF content ON chat_events BEGIN
        INSERT INTO chat_events_fts(chat_events_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
        INSERT INTO chat_events_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
)

# Exact 0005/0047 sqlite_master text. Upgrade literals above stay unchanged.
_CHAT_EVENTS_FTS_0047 = """CREATE VIRTUAL TABLE chat_events_fts USING fts5(
            content,
            content='chat_events',
            content_rowid='id',
            tokenize='trigram'
        )"""
_CHAT_EVENTS_FTS_TRIGGERS_0047: tuple[str, ...] = (
    """CREATE TRIGGER chat_events_fts_ai AFTER INSERT ON chat_events BEGIN
            INSERT INTO chat_events_fts(rowid, content) VALUES (new.id, new.content);
        END""",
    """CREATE TRIGGER chat_events_fts_ad AFTER DELETE ON chat_events BEGIN
            INSERT INTO chat_events_fts(chat_events_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
        END""",
    """CREATE TRIGGER chat_events_fts_au AFTER UPDATE OF content ON chat_events BEGIN
            INSERT INTO chat_events_fts(chat_events_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
            INSERT INTO chat_events_fts(rowid, content) VALUES (new.id, new.content);
        END""",
)


def _strip_legacy_fk_sql(create_sql: str) -> str:
    stripped = _NAMED_TABLE_FK.sub("", create_sql)
    stripped = _TABLE_FK.sub("", stripped)
    stripped = _COLUMN_REF.sub("", stripped)
    stripped = _DANGLING_FK.sub("", stripped)
    stripped = _NAMED_LEGACY_UNIQUE.sub("", stripped)
    stripped = _BARE_LEGACY_UNIQUE.sub("", stripped)
    stripped = _DOUBLE_COMMA.sub(",", stripped)
    stripped = re.sub(r",\s*\)", ")", stripped)
    return stripped


def _create_cutover_tables() -> None:
    op.create_table(
        "identity_cutover_manifests",
        sa.Column("fingerprint", sa.String(length=64), primary_key=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"length(fingerprint) = 64 AND fingerprint = lower(fingerprint) "
            f"AND fingerprint GLOB '{_HEX64}'",
            name="ck_identity_cutover_manifests_fingerprint",
        ),
        sa.CheckConstraint(
            "length(payload_json) > 0",
            name="ck_identity_cutover_manifests_payload",
        ),
    )
    op.create_table(
        "identity_cutover_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("git_revision", sa.String(length=64), nullable=False),
        sa.Column("downtime_token", sa.String(length=128), nullable=False),
        sa.Column("snapshot_db", sa.String(length=512), nullable=False),
        sa.Column("snapshot_wal", sa.String(length=512), nullable=False),
        sa.Column("snapshot_shm", sa.String(length=512), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("mode IN ('plan', 'apply')", name="ck_identity_cutover_runs_mode"),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed', 'blocked')",
            name="ck_identity_cutover_runs_status",
        ),
        sa.CheckConstraint(
            "length(git_revision) > 0 AND git_revision = trim(git_revision)",
            name="ck_identity_cutover_runs_git_revision",
        ),
        sa.CheckConstraint(
            "length(downtime_token) > 0",
            name="ck_identity_cutover_runs_downtime_token",
        ),
        sa.CheckConstraint(
            "source_fingerprint IS NULL OR ("
            f"length(source_fingerprint) = 64 "
            f"AND source_fingerprint = lower(source_fingerprint) "
            f"AND source_fingerprint GLOB '{_HEX64}'"
            ")",
            name="ck_identity_cutover_runs_source_fingerprint",
        ),
    )
    op.create_index(
        "ix_identity_cutover_runs_status_created",
        "identity_cutover_runs",
        ["status", "created_at"],
    )
    op.create_table(
        "canonical_conversation_rollups",
        sa.Column("conversation_id", sa.String(length=36), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("covered_through_event_id", sa.Integer(), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("summary_kind", sa.String(length=16), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["canonical_conversations.id"],
            name="fk_canonical_conversation_rollups_conversation",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "summary_kind IN ('model', 'extractive', 'migration')",
            name="ck_canonical_conversation_rollups_kind",
        ),
        sa.CheckConstraint(
            "generation >= 1 AND covered_through_event_id >= 0 "
            "AND revision >= 1 AND length(summary_text) > 0",
            name="ck_canonical_conversation_rollups_state",
        ),
        sa.CheckConstraint(
            f"length(source_fingerprint) = 64 AND source_fingerprint = lower(source_fingerprint) "
            f"AND source_fingerprint GLOB '{_HEX64}'",
            name="ck_canonical_conversation_rollups_fingerprint",
        ),
    )
    op.create_table(
        "canonical_conversation_rollup_jobs",
        sa.Column("conversation_id", sa.String(length=36), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("signal_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error_category", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["canonical_conversations.id"],
            name="fk_canonical_conversation_rollup_jobs_conversation",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing')",
            name="ck_canonical_conversation_rollup_jobs_status",
        ),
        sa.CheckConstraint(
            "generation >= 1 AND signal_revision >= 1 AND failure_count >= 0",
            name="ck_canonical_conversation_rollup_jobs_state",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_until IS NULL) OR (status = 'processing' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_canonical_conversation_rollup_jobs_lease",
        ),
    )
    op.create_index(
        "ix_canonical_conversation_rollup_jobs_claim",
        "canonical_conversation_rollup_jobs",
        ["status", "next_attempt_at", "lease_until"],
    )
    op.create_table(
        "conversation_rollup_emergency_overlays",
        sa.Column("scope_id", sa.Integer(), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("covered_through_event_id", sa.Integer(), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("base_semantic_revision", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope_id"],
            ["conversation_scopes.id"],
            name="fk_conversation_rollup_emergency_overlays_scope",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "generation >= 1 AND covered_through_event_id >= 0 "
            "AND base_semantic_revision >= 0 AND revision >= 1 "
            "AND length(summary_text) > 0",
            name="ck_conversation_rollup_emergency_overlays_state",
        ),
        sa.CheckConstraint(
            f"length(source_fingerprint) = 64 AND source_fingerprint = lower(source_fingerprint) "
            f"AND source_fingerprint GLOB '{_HEX64}'",
            name="ck_conversation_rollup_emergency_overlays_fingerprint",
        ),
    )
    op.create_table(
        "canonical_conversation_rollup_emergency_overlays",
        sa.Column("conversation_id", sa.String(length=36), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("covered_through_event_id", sa.Integer(), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("base_semantic_revision", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["canonical_conversations.id"],
            name="fk_canonical_conversation_rollup_emergency_overlays_conversation",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            _uuid4_sql("conversation_id"),
            name="ck_canonical_conversation_rollup_emergency_overlays_conversation_id",
        ),
        sa.CheckConstraint(
            "generation >= 1 AND covered_through_event_id >= 0 "
            "AND base_semantic_revision >= 0 AND revision >= 1 "
            "AND length(summary_text) > 0",
            name="ck_canonical_conversation_rollup_emergency_overlays_state",
        ),
        sa.CheckConstraint(
            f"length(source_fingerprint) = 64 AND source_fingerprint = lower(source_fingerprint) "
            f"AND source_fingerprint GLOB '{_HEX64}'",
            name="ck_canonical_conversation_rollup_emergency_overlays_fingerprint",
        ),
    )


def _scalar_sql(bind: sa.Connection, sql: str, params: dict[str, object]) -> str | None:
    return bind.execute(sa.text(sql), params).scalar()


def _list_sql(bind: sa.Connection, sql: str, params: dict[str, object]) -> list[str]:
    return [str(item) for item in bind.execute(sa.text(sql), params).scalars().all()]


def _drop_chat_events_fts(bind: sa.Connection) -> None:
    bind.execute(sa.text("DROP TRIGGER IF EXISTS chat_events_fts_ai"))
    bind.execute(sa.text("DROP TRIGGER IF EXISTS chat_events_fts_ad"))
    bind.execute(sa.text("DROP TRIGGER IF EXISTS chat_events_fts_au"))
    bind.execute(sa.text("DROP TABLE IF EXISTS chat_events_fts"))


def _rebuild_chat_events_fts(bind: sa.Connection) -> None:
    existing = {
        str(name)
        for name in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        ).scalars()
    }
    if "chat_events" not in existing or "chat_events_fts" not in existing:
        return
    bind.exec_driver_sql("INSERT INTO chat_events_fts(chat_events_fts) VALUES('rebuild')")


def _create_chat_events_fts(bind: sa.Connection) -> None:
    existing = {
        str(name)
        for name in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')")
        ).scalars()
    }
    if "chat_events" not in existing:
        return
    if "chat_events_fts" not in existing:
        bind.execute(sa.text(_CHAT_EVENTS_FTS))
    for statement in _CHAT_EVENTS_FTS_TRIGGERS:
        bind.execute(sa.text(statement))
    _rebuild_chat_events_fts(bind)


def _install_partial_legacy_unique(bind: sa.Connection) -> None:
    existing = set(inspect(bind).get_table_names())
    if "chat_events" not in existing:
        return
    columns = {str(row[1]) for row in bind.execute(sa.text('PRAGMA table_info("chat_events")'))}
    if "canonical_event_id" not in columns:
        return
    bind.execute(sa.text("DROP INDEX IF EXISTS uq_chat_events_bot_platform_message"))
    bind.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_chat_events_bot_platform_message "
            "ON chat_events (bot_user_id, platform_message_id) "
            "WHERE canonical_event_id IS NULL"
        )
    )


def _rebuild_without_carrier_fks(bind: sa.Connection) -> None:
    existing = set(inspect(bind).get_table_names())
    for table_name in _REBUILD_TABLES:
        if table_name not in existing:
            continue
        create_sql = _scalar_sql(
            bind,
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name",
            {"name": table_name},
        )
        if not create_sql:
            continue
        needs_rebuild = (
            "REFERENCES" in create_sql.upper()
            or "uq_chat_events_bot_platform_message" in create_sql
            or "UNIQUE (bot_user_id, platform_message_id)" in create_sql
            or "UNIQUE(bot_user_id, platform_message_id)" in create_sql
        )
        if not needs_rebuild:
            continue
        indexes = _list_sql(
            bind,
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=:name "
            "AND sql IS NOT NULL AND name != 'uq_chat_events_bot_platform_message'",
            {"name": table_name},
        )
        triggers = _list_sql(
            bind,
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=:name "
            "AND sql IS NOT NULL AND name NOT LIKE 'chat_events_fts_%'",
            {"name": table_name},
        )
        rebuilt = create_sql.replace(
            f'CREATE TABLE "{table_name}"',
            f'CREATE TABLE "{table_name}__c48"',
            1,
        )
        rebuilt = rebuilt.replace(
            f"CREATE TABLE {table_name}",
            f"CREATE TABLE {table_name}__c48",
            1,
        )
        rebuilt = _strip_legacy_fk_sql(rebuilt)
        bind.execute(sa.text(rebuilt))
        bind.execute(sa.text(f'INSERT INTO "{table_name}__c48" SELECT * FROM "{table_name}"'))
        bind.execute(sa.text(f'DROP TABLE "{table_name}"'))
        bind.execute(sa.text(f'ALTER TABLE "{table_name}__c48" RENAME TO "{table_name}"'))
        for sql in indexes:
            bind.execute(sa.text(sql))
        for sql in triggers:
            bind.execute(sa.text(sql))


def upgrade() -> None:
    _create_cutover_tables()
    bind = op.get_bind()
    bind.execute(sa.text("PRAGMA foreign_keys=OFF"))
    _drop_chat_events_fts(bind)
    _rebuild_without_carrier_fks(bind)
    _install_partial_legacy_unique(bind)
    _create_chat_events_fts(bind)
    bind.execute(sa.text("PRAGMA foreign_keys=ON"))
    violations = bind.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
    if violations:
        raise RuntimeError("0048 foreign_key_check failed")


def _table_names(bind: sa.Connection) -> set[str]:
    return {
        str(name)
        for name in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        ).scalars()
    }


def _require_pre_apply_schema_downgrade(bind: sa.Connection) -> None:
    names = _table_names(bind)
    if "identity_runtime_state" not in names:
        raise RuntimeError("0048 downgrade blocked: identity_runtime_state missing")
    rows = bind.execute(sa.text("SELECT state FROM identity_runtime_state")).fetchall()
    if len(rows) != 1 or str(rows[0][0]) != "v1":
        raise RuntimeError("0048 downgrade blocked: identity_runtime_state is not v1")
    if "identity_cutover_runs" not in names:
        raise RuntimeError("0048 downgrade blocked: identity_cutover_runs missing")
    succeeded = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM identity_cutover_runs "
            "WHERE mode = 'apply' AND status = 'succeeded'"
        )
    ).scalar()
    if int(succeeded or 0) > 0:
        raise RuntimeError("0048 downgrade blocked: succeeded identity cutover apply exists")


def _validate_legacy_chat_event_unique(bind: sa.Connection, existing: set[str]) -> None:
    if "chat_events" not in existing:
        return
    duplicate = bind.execute(
        sa.text(
            "SELECT 1 FROM ("
            "SELECT bot_user_id, platform_message_id FROM chat_events "
            "GROUP BY bot_user_id, platform_message_id HAVING COUNT(*) > 1"
            ") LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "0048 downgrade blocked: duplicate chat_events (bot_user_id, platform_message_id)"
        )


def _validate_carrier_rows(bind: sa.Connection, existing: set[str]) -> None:
    for table, local, parent, remote, _on_update, _on_delete, _clause in _CARRIER_FOREIGN_KEYS:
        if table not in existing:
            continue
        if parent not in existing:
            raise RuntimeError(
                f"0048 downgrade blocked: missing parent {parent} for {table}.{local}"
            )
        orphan = bind.execute(
            sa.text(
                f'SELECT 1 FROM "{table}" WHERE "{local}" IS NOT NULL AND NOT EXISTS ('
                f'SELECT 1 FROM "{parent}" WHERE "{parent}"."{remote}" = "{table}"."{local}"'
                f") LIMIT 1"
            )
        ).first()
        if orphan is not None:
            raise RuntimeError(f"0048 downgrade blocked: missing {parent} row for {table}.{local}")


def _inject_legacy_carrier_sql(table_name: str, create_sql: str) -> str:
    extras: list[str] = []
    if table_name == "chat_events" and "uq_chat_events_bot_platform_message" not in create_sql:
        extras.append(_LEGACY_CHAT_EVENTS_UNIQUE)
    for (
        item_table,
        _local,
        _parent,
        _remote,
        _on_update,
        _on_delete,
        clause,
    ) in _CARRIER_FOREIGN_KEYS:
        if item_table == table_name and clause not in create_sql:
            extras.append(clause)
    if not extras:
        return create_sql
    stripped = create_sql.rstrip()
    if not stripped.endswith(")"):
        raise RuntimeError(f"0048 downgrade blocked: invalid CREATE TABLE for {table_name}")
    body = stripped[:-1].rstrip()
    if body.endswith(","):
        body = body[:-1].rstrip()
    return f"{body},\n    {',\n    '.join(extras)}\n)"


def _rebuild_with_carrier_fks(bind: sa.Connection) -> None:
    existing = set(inspect(bind).get_table_names())
    for table_name in _REBUILD_TABLES:
        if table_name not in existing:
            continue
        create_sql = _scalar_sql(
            bind,
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name",
            {"name": table_name},
        )
        if not create_sql:
            raise RuntimeError(f"0048 downgrade blocked: missing CREATE SQL for {table_name}")
        indexes = _list_sql(
            bind,
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=:name "
            "AND sql IS NOT NULL AND name != 'uq_chat_events_bot_platform_message'",
            {"name": table_name},
        )
        triggers = _list_sql(
            bind,
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=:name "
            "AND sql IS NOT NULL AND name NOT LIKE 'chat_events_fts_%'",
            {"name": table_name},
        )
        rebuilt = create_sql.replace(
            f'CREATE TABLE "{table_name}"',
            f'CREATE TABLE "{table_name}__c48d"',
            1,
        )
        rebuilt = rebuilt.replace(
            f"CREATE TABLE {table_name}",
            f"CREATE TABLE {table_name}__c48d",
            1,
        )
        rebuilt = _inject_legacy_carrier_sql(table_name, rebuilt)
        bind.execute(sa.text(rebuilt))
        bind.execute(sa.text(f'INSERT INTO "{table_name}__c48d" SELECT * FROM "{table_name}"'))
        bind.execute(sa.text(f'DROP TABLE "{table_name}"'))
        bind.execute(sa.text(f'ALTER TABLE "{table_name}__c48d" RENAME TO "{table_name}"'))
        for sql in indexes:
            bind.execute(sa.text(sql))
        for sql in triggers:
            bind.execute(sa.text(sql))


def _restore_chat_events_fts_0047(bind: sa.Connection) -> None:
    existing = _table_names(bind)
    if "chat_events" not in existing:
        return
    if "chat_events_fts" not in existing:
        bind.exec_driver_sql(_CHAT_EVENTS_FTS_0047)
        bind.exec_driver_sql("INSERT INTO chat_events_fts(chat_events_fts) VALUES('rebuild')")
    for statement in _CHAT_EVENTS_FTS_TRIGGERS_0047:
        bind.exec_driver_sql(statement)


def _drop_0048_owned_objects() -> None:
    op.drop_table("canonical_conversation_rollup_emergency_overlays")
    op.drop_table("conversation_rollup_emergency_overlays")
    op.drop_index(
        "ix_canonical_conversation_rollup_jobs_claim",
        table_name="canonical_conversation_rollup_jobs",
    )
    op.drop_table("canonical_conversation_rollup_jobs")
    op.drop_table("canonical_conversation_rollups")
    op.drop_index("ix_identity_cutover_runs_status_created", table_name="identity_cutover_runs")
    op.drop_table("identity_cutover_runs")
    op.drop_table("identity_cutover_manifests")


def downgrade() -> None:
    """Schema-only reverse before apply. Not a post-cutover data rollback."""

    bind = op.get_bind()
    _require_pre_apply_schema_downgrade(bind)
    existing = set(inspect(bind).get_table_names())
    _validate_legacy_chat_event_unique(bind, existing)
    _validate_carrier_rows(bind, existing)
    bind.execute(sa.text("PRAGMA foreign_keys=OFF"))
    _drop_chat_events_fts(bind)
    _rebuild_with_carrier_fks(bind)
    _restore_chat_events_fts_0047(bind)
    _drop_0048_owned_objects()
    bind.execute(sa.text("PRAGMA foreign_keys=ON"))
    violations = bind.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
    if violations:
        raise RuntimeError("0048 downgrade foreign_key_check failed")
