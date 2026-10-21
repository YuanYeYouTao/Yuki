"""Prepare the final history search structure before offline text conversion."""

import sqlalchemy as sa
from alembic import op

revision = "0144"
down_revision = "0143"
branch_labels = None
depends_on = None


def _preserve_event_ids() -> None:
    """Make SQLite's own rowid allocator retain every proven historical high-water mark."""
    connection = op.get_bind()
    highest = connection.exec_driver_sql(
        "SELECT COALESCE(MAX(value),0) FROM ("
        "SELECT MAX(id) AS value FROM chat_events UNION ALL "
        "SELECT MAX(last_event_id) FROM canonical_conversations UNION ALL "
        "SELECT MAX(starts_after_event_id) FROM canonical_conversations UNION ALL "
        "SELECT MAX(last_generation_change_event_id) FROM canonical_conversations UNION ALL "
        "SELECT MAX(event_id) FROM social_operation_receipts UNION ALL "
        "SELECT MAX(original_event_id) FROM social_operation_receipts UNION ALL "
        "SELECT MAX(seq) FROM sqlite_sequence WHERE name='chat_events')"
    ).scalar_one()
    triggers = (
        connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='chat_events' "
            "ORDER BY name"
        )
        .scalars()
        .all()
    )
    legacy = connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar_one()
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table(
            "chat_events", recreate="always", table_kwargs={"sqlite_autoincrement": True}
        ):
            pass
    finally:
        connection.exec_driver_sql(f"PRAGMA legacy_alter_table={legacy}")
    for statement in triggers:
        connection.exec_driver_sql(statement)
    connection.exec_driver_sql(
        "UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='chat_events'", (highest,)
    )
    connection.exec_driver_sql(
        "INSERT INTO sqlite_sequence(name,seq) SELECT 'chat_events',? "
        "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='chat_events')",
        (highest,),
    )


def upgrade() -> None:
    op.add_column(
        "social_operation_receipts", sa.Column("original_event_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "social_operation_receipts",
        sa.Column("original_conversation_id", sa.String(36), nullable=True),
    )
    _preserve_event_ids()
    op.execute(
        "UPDATE social_operation_receipts SET original_event_id=event_id, original_conversation_id=(SELECT canonical_conversation_id FROM chat_events WHERE chat_events.id=social_operation_receipts.event_id) WHERE event_id IS NOT NULL"
    )
    op.add_column(
        "memory_tool_receipts",
        sa.Column("source_event_ids_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.execute(
        "CREATE VIRTUAL TABLE chat_history_fts USING fts5(tokens, content='', contentless_delete=1, detail=none, tokenize='ascii')"
    )
    op.create_index(
        "ix_chat_events_conversation_author",
        "chat_events",
        ["canonical_conversation_id", "author_person_id"],
    )
    op.create_index(
        "ix_chat_events_conversation_occurred",
        "chat_events",
        ["canonical_conversation_id", "occurred_at"],
    )


def downgrade() -> None:
    raise RuntimeError("0144 prepares the irreversible history search replacement")
