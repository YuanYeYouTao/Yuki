"""Destructively rebuild storage around people and a permanent event ledger.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-25
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import CheckConstraint, Index, MetaData, Table, UniqueConstraint, inspect
from sqlalchemy.schema import ForeignKeyConstraint, PrimaryKeyConstraint

from qq_ai_bot.persistence.metadata import Base

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_C4_CHAT_EVENT_SHADOW_COLUMNS: tuple[str, ...] = (
    "ingress_gateway_instance_id",
    "ingress_provider",
    "suppression_status",
    "utterance_fingerprint",
    "ingress_presence_id",
    "author_presence_id",
    "author_person_id",
    "author_kind",
    "canonical_conversation_id",
    "canonical_event_id",
)
_C4_CHAT_EVENT_SHADOW_INDEXES: tuple[str, ...] = (
    "ix_chat_events_canonical_event_id",
    "ix_chat_events_canonical_conversation_id",
    "uq_chat_events_canonical_event_keeper",
    "uq_chat_events_bot_platform_message",
)
_C27_0005_RESTORED_CARRIER_FKS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "person_aliases": (("user_id", "people", "user_id", "CASCADE"),),
    "memberships": (
        ("user_id", "people", "user_id", "CASCADE"),
        ("group_id", "groups", "group_id", "CASCADE"),
    ),
    "chat_events": (
        ("sender_user_id", "people", "user_id", "CASCADE"),
        ("private_peer_user_id", "people", "user_id", "CASCADE"),
        ("group_id", "groups", "group_id", "CASCADE"),
    ),
}
_C5_OWNERSHIP_SHADOW_COLUMNS: dict[str, tuple[str, ...]] = {
    "people": ("canonical_person_id",),
    "groups": ("canonical_space_id",),
    "person_aliases": ("canonical_person_id", "canonical_space_id"),
    "memberships": ("canonical_person_id", "canonical_space_id"),
}
_C5_OWNERSHIP_SHADOW_INDEXES: tuple[str, ...] = (
    "ix_people_canonical_person_id",
    "ix_groups_canonical_space_id",
    "ix_person_aliases_canonical_person_id",
    "ix_person_aliases_canonical_space_id",
    "ix_memberships_canonical_person_id",
    "ix_memberships_canonical_space_id",
)
_STRIPPED_AT_0005: tuple[str, ...] = (
    "people",
    "groups",
    "person_aliases",
    "memberships",
    "chat_events",
)
_STRIPPED_PARENTS: dict[str, tuple[str, ...]] = {
    "people": (),
    "groups": (),
    "person_aliases": ("people",),
    "memberships": ("people", "groups"),
    "chat_events": ("people", "groups", "automations", "automation_runs"),
}


def _excluded_future_columns(table_name: str) -> set[str]:
    excluded = set(_C4_CHAT_EVENT_SHADOW_COLUMNS if table_name == "chat_events" else ())
    excluded.update(_C5_OWNERSHIP_SHADOW_COLUMNS.get(table_name, ()))
    return excluded


def _copy_table_without_future_shadows(table_name: str, side: MetaData) -> Table:
    """Copy one ORM table onto side metadata without later C4/C5 shadows."""

    if table_name in side.tables:
        return side.tables[table_name]
    source = Base.metadata.tables[table_name]
    excluded = _excluded_future_columns(table_name)
    table = Table(
        source.name,
        side,
        *[column._copy() for column in source.columns if column.name not in excluded],
    )
    for constraint in list(source.constraints):
        if isinstance(constraint, PrimaryKeyConstraint):
            continue
        if isinstance(constraint, ForeignKeyConstraint):
            local_names = [element.parent.name for element in constraint.elements]
            if set(local_names) & excluded:
                continue
            table.append_constraint(
                ForeignKeyConstraint(
                    local_names,
                    [element.target_fullname for element in constraint.elements],
                    name=constraint.name,
                    ondelete=constraint.ondelete,
                    onupdate=constraint.onupdate,
                )
            )
            continue
        if isinstance(constraint, UniqueConstraint):
            names = [column.name for column in constraint.columns]
            if set(names) & excluded:
                continue
            table.append_constraint(UniqueConstraint(*names, name=constraint.name))
            continue
        if isinstance(constraint, CheckConstraint):
            table.append_constraint(CheckConstraint(constraint.sqltext, name=constraint.name))
    for index in source.indexes:
        if index.name == "uq_chat_events_bot_platform_message":
            continue
        names = [column.name for column in index.columns]
        if set(names) & excluded:
            continue
        kwargs: dict[str, object] = {"unique": index.unique}
        sqlite_opts = index.dialect_options.get("sqlite", {})
        if "where" in sqlite_opts:
            continue
        Index(index.name, *[table.c[name] for name in names], **kwargs)
    if table_name == "chat_events":
        table.append_constraint(
            UniqueConstraint(
                "bot_user_id",
                "platform_message_id",
                name="uq_chat_events_bot_platform_message",
            )
        )
    for local, parent, remote, ondelete in _C27_0005_RESTORED_CARRIER_FKS.get(table_name, ()):
        table.append_constraint(
            ForeignKeyConstraint(
                [local],
                [f"{parent}.{remote}"],
                ondelete=ondelete,
            )
        )
    return table


def _table_without_future_shadows(table_name: str) -> Table:
    """Create an empty 0005 table without later C4/C5 shadow columns or FKs.

    SQLite DROP COLUMN does not remove FOREIGN KEY clauses that were part of
    the original CREATE TABLE, so current ORM create_all would make later
    shadow columns undeletable. Parent copies are stripped too so a later
    people.canonical_person_id → persons FK cannot leak into 0005.
    """

    side = MetaData()
    for parent in _STRIPPED_PARENTS[table_name]:
        _copy_table_without_future_shadows(parent, side)
    return _copy_table_without_future_shadows(table_name, side)


def upgrade() -> None:
    """Discard pre-1.0 business data and create the person-centric schema."""

    bind = op.get_bind()
    op.execute("DROP TRIGGER IF EXISTS chat_events_fts_ai")
    op.execute("DROP TRIGGER IF EXISTS chat_events_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS chat_events_fts_au")
    op.execute("DROP TABLE IF EXISTS chat_events_fts")

    existing = set(inspect(bind).get_table_names())
    for table_name in (
        "user_group_profiles",
        "private_user_settings",
        "messages",
        "conversations",
        "group_memories",
        "group_settings",
        "processed_events",
        "user_profiles",
    ):
        if table_name in existing:
            op.drop_table(table_name)

    # Later revisions add tables to the shared ORM metadata. Keep this historical
    # migration deterministic so a fresh install does not create future tables early.
    v1_tables = [
        table
        for table in Base.metadata.tables.values()
        if table.name
        not in {
            "web_search_runs",
            "web_search_sources",
            "person_relationships",
            "relationship_events",
            "relationship_jobs",
            "runtime_config_overrides",
            "admin_operation_events",
            "media_analyses",
            "emoji_descriptions",
            "person_time_settings",
            "automations",
            "automation_versions",
            "automation_runs",
            "automation_step_runs",
            "planner_runs",
            "plugin_installations",
            "plugin_config_values",
            "plugin_state",
            "plugin_audit_events",
            "plugin_agent_sessions",
            "plugin_agent_messages",
            "emoji_assets",
            "emoji_scope_states",
            "emoji_jobs",
            "emoji_usage_events",
            "speech_voice_profiles",
            "speech_voice_references",
            "speech_generations",
            "person_speech_preferences",
            "model_invocations",
            "memory_facts",
            "memory_evidence",
            "memory_jobs",
            "mcp_server_states",
            "mcp_tool_cache",
            "tool_artifacts",
            "tool_invocations",
            "memory_embedding_profiles",
            "memory_embeddings",
            "memory_embedding_jobs",
            "memory_fact_relations",
            "memory_fact_state_events",
            "memory_rebuild_runs",
            "memory_rebuild_items",
            "memory_rebuild_proposals",
            "memory_mutation_receipts",
            "memory_reflection_jobs",
            "memory_claim_candidates",
            "memory_claim_candidate_evidence",
            "memory_tool_receipts",
            "memory_self_reflection_runtime",
            "memory_self_reflection_states",
            "memory_self_reflection_runs",
            "memory_self_reflection_results",
            "memory_dream_runtime",
            "memory_dream_runs",
            "memory_dream_clusters",
            "memory_dream_operations",
            "memory_dream_operation_sources",
            "memory_dream_operation_results",
            "memory_dream_fact_checkpoints",
            "memory_dream_cluster_previews",
            "memory_evidence_compaction_runs",
            "memory_evidence_compaction_items",
            "memory_activation_states",
            "memory_recall_receipts",
            "memory_recall_items",
            "plugin_background_target_grants",
            "plugin_media_artifacts",
            "plugin_notification_outbox",
            "plugin_background_turn_jobs",
            "runtime_turn_observations",
            "reply_effect_events",
            "conversation_scopes",
            "conversation_rollups",
            "conversation_rollup_jobs",
            "persons",
            "identity_bindings",
            "spaces",
            "space_bindings",
            "presences",
            "identity_runtime_state",
            "identity_backfill_runs",
            "identity_conflicts",
            "canonical_conversations",
            "conversation_legacy_aliases",
            "person_active_routes",
            "space_binding_ingest_routes",
            "space_active_routes",
            "control_command_receipts",
            "canonical_event_receipts",
            "identity_cutover_manifests",
            "identity_cutover_runs",
            "canonical_conversation_rollups",
            "canonical_conversation_rollup_jobs",
            "conversation_rollup_emergency_overlays",
            "canonical_conversation_rollup_emergency_overlays",
        }
    ]
    create_tables = [table for table in v1_tables if table.name not in _STRIPPED_AT_0005]
    _table_without_future_shadows("people").create(bind, checkfirst=True)
    _table_without_future_shadows("groups").create(bind, checkfirst=True)
    Base.metadata.create_all(bind=bind, tables=create_tables, checkfirst=True)
    _table_without_future_shadows("person_aliases").create(bind, checkfirst=True)
    _table_without_future_shadows("memberships").create(bind, checkfirst=True)
    _table_without_future_shadows("chat_events").create(bind, checkfirst=True)
    for index_name in (*_C4_CHAT_EVENT_SHADOW_INDEXES, *_C5_OWNERSHIP_SHADOW_INDEXES):
        bind.exec_driver_sql(f"DROP INDEX IF EXISTS {index_name}")
    chat_event_columns = {
        str(row[1]) for row in bind.exec_driver_sql("PRAGMA table_info(chat_events)")
    }
    for column_name in _C4_CHAT_EVENT_SHADOW_COLUMNS:
        if column_name in chat_event_columns:
            bind.exec_driver_sql(f"ALTER TABLE chat_events DROP COLUMN {column_name}")
    op.execute(
        """
        CREATE VIRTUAL TABLE chat_events_fts USING fts5(
            content,
            content='chat_events',
            content_rowid='id',
            tokenize='trigram'
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER chat_events_fts_ai AFTER INSERT ON chat_events BEGIN
            INSERT INTO chat_events_fts(rowid, content) VALUES (new.id, new.content);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER chat_events_fts_ad AFTER DELETE ON chat_events BEGIN
            INSERT INTO chat_events_fts(chat_events_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER chat_events_fts_au AFTER UPDATE OF content ON chat_events BEGIN
            INSERT INTO chat_events_fts(chat_events_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
            INSERT INTO chat_events_fts(rowid, content) VALUES (new.id, new.content);
        END
        """
    )


def downgrade() -> None:
    """Refuse a lossy downgrade after the intentional 1.0 reset."""

    raise RuntimeError("revision 0005 is an irreversible destructive data reset")
