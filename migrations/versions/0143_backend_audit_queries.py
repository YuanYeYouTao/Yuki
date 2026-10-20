"""Index original artifact references and maintenance candidates; retire unused SDK state.

Definitions are frozen here. The caller owns the offline SQLite transaction.
"""

import sqlalchemy as sa
from alembic import op

revision = "0143"
down_revision = "0142"
branch_labels = None
depends_on = None

_REFERENCES = (
    "SELECT {event_id}, json_extract(part.value, '$.data.artifact_id'), "
    "json_extract(part.value, '$.data.version') FROM json_each({segments}) AS part "
    "WHERE part.type='object' AND json_type(part.value, '$.data')='object' "
    "AND json_type(part.value, '$.data.artifact_id')='text' "
    "AND json_type(part.value, '$.data.version')='text'"
)
_INSERT = (
    "INSERT OR IGNORE INTO chat_event_artifact_refs(event_id,artifact_id,version) "
    + _REFERENCES.format(event_id="NEW.id", segments="NEW.segments_json")
    + ";"
)
_TRIGGERS = (
    "CREATE TRIGGER chat_events_artifact_refs_ai AFTER INSERT ON chat_events "
    "BEGIN " + _INSERT + " END",
    "CREATE TRIGGER chat_events_artifact_refs_au AFTER UPDATE OF segments_json ON chat_events "
    "WHEN NEW.segments_json IS NOT OLD.segments_json "
    "BEGIN DELETE FROM chat_event_artifact_refs WHERE event_id=OLD.id; " + _INSERT + " END",
)
_INDEXES = (
    (
        "ix_plugin_background_turn_unbound_due",
        "plugin_background_turn_jobs",
        ("status", "work_id", "next_attempt_at"),
    ),
    (
        "ix_plugin_background_turn_expired_lease",
        "plugin_background_turn_jobs",
        ("status", "lease_until", "next_attempt_at"),
    ),
    ("ix_memory_facts_status_valid_until_id", "memory_facts", ("status", "valid_until", "id")),
    (
        "ix_memory_self_reflection_cycles_status_created",
        "memory_self_reflection_cycles",
        ("status", "created_at"),
    ),
    (
        "ix_memory_self_reflection_runs_retry_next",
        "memory_self_reflection_runs",
        ("retry_state", "next_attempt_at"),
    ),
    (
        "ix_memory_self_reflection_cycles_delivery",
        "memory_self_reflection_cycles",
        ("trigger", "delivery_state", "status", "created_at"),
    ),
)


def upgrade() -> None:
    op.create_table(
        "chat_event_artifact_refs",
        sa.Column(
            "event_id",
            sa.Integer(),
            sa.ForeignKey("chat_events.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("artifact_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.Text(), primary_key=True),
    )
    op.create_index(
        "ix_chat_event_artifact_refs_artifact_version",
        "chat_event_artifact_refs",
        ["artifact_id", "version", "event_id"],
    )
    # A single offline pass projects only the original event's exact string pairs.
    # No owner, current account or current delivery eligibility is copied.
    op.execute(
        "INSERT OR IGNORE INTO chat_event_artifact_refs(event_id,artifact_id,version) "
        "SELECT event.id, json_extract(part.value, '$.data.artifact_id'), "
        "json_extract(part.value, '$.data.version') "
        "FROM chat_events AS event, json_each(event.segments_json) AS part "
        "WHERE part.type='object' AND json_type(part.value, '$.data')='object' "
        "AND json_type(part.value, '$.data.artifact_id')='text' "
        "AND json_type(part.value, '$.data.version')='text'"
    )
    for statement in _TRIGGERS:
        op.execute(statement)
    op.execute(
        "CREATE INDEX ix_runtime_effects_domain_ref ON runtime_work_effects "
        "(json_extract(receipt_json, '$.invocation.original_domain_ref'), work_id)"
    )
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns)
    op.create_index(
        "ix_prompt_projections_active_updated",
        "prompt_projections",
        ["updated_at", "view_key", "byte_size"],
        sqlite_where=sa.text("invalidated_reason IS NULL"),
    )
    op.create_index(
        "ix_runtime_work_ready_scope",
        "runtime_work",
        [sa.text("(state IN ('queued', 'running'))"), "conversation_id", "updated", "id"],
        sqlite_where=sa.text("(state IN ('queued', 'running')) = 1"),
    )
    op.create_index(
        "ix_runtime_subagents_unarchived",
        "runtime_subagents",
        ["lease_until", "work_id"],
        sqlite_where=sa.text("archived_at IS NULL"),
    )
    # This column never limited execution and is no longer part of Plugin API 7.
    op.drop_column("plugin_agent_sessions", "allowed_capabilities_json")


def downgrade() -> None:
    raise RuntimeError("0143 retires the unused plugin session capability declaration")
