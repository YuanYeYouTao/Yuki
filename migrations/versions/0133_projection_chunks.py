"""Store the current projection as immutable append batches."""

import sqlalchemy as sa
from alembic import op

from yuki.conversation.projection_chunks_schema import PROJECTION_TRIGGERS_0133

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    for name in PROJECTION_TRIGGERS_0133:
        connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
    op.add_column(
        "prompt_projections",
        sa.Column("tail_ordinal", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "prompt_projections",
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "prompt_projection_chunks",
        sa.Column(
            "view_key",
            sa.String(64),
            sa.ForeignKey("prompt_projections.view_key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("epoch_id", sa.String(36), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("ordinal >= 1", name="ck_prompt_projection_chunk_ordinal"),
    )
    # Copy the actual current bytes, not historical first-selection provenance.
    connection.exec_driver_sql("""
        INSERT INTO prompt_projection_chunks(view_key, epoch_id, ordinal, payload_json)
        SELECT view_key, epoch_id, 1, payload_json FROM prompt_projections
        WHERE invalidated_reason IS NULL AND json_array_length(payload_json)>0
    """)
    connection.exec_driver_sql("""
        UPDATE prompt_projections SET tail_ordinal=1, item_count=json_array_length(payload_json)
        WHERE invalidated_reason IS NULL AND json_array_length(payload_json)>0
    """)
    op.drop_column("prompt_projections", "payload_json")
    op.add_column(
        "model_context_selections",
        sa.Column("min_event_id", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "model_context_selections",
        sa.Column("max_event_id", sa.Integer(), nullable=False, server_default="0"),
    )
    connection.exec_driver_sql("""
        UPDATE model_context_selections SET
        min_event_id=coalesce((SELECT min(value) FROM json_each(event_ids_json)),0),
        max_event_id=coalesce((SELECT max(value) FROM json_each(event_ids_json)),0)
    """)
    op.create_index(
        "ix_context_selection_range",
        "model_context_selections",
        ["view_key", "generation", "max_event_id", "id"],
    )
    for statement in PROJECTION_TRIGGERS_0133.values():
        connection.exec_driver_sql(statement)


def downgrade() -> None:
    connection = op.get_bind()
    for name in PROJECTION_TRIGGERS_0133:
        connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
    op.add_column(
        "prompt_projections",
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="[]"),
    )
    import json

    for view, epoch in connection.exec_driver_sql(
        "SELECT view_key, epoch_id FROM prompt_projections"
    ):
        items = []
        for (payload,) in connection.exec_driver_sql(
            "SELECT payload_json FROM prompt_projection_chunks WHERE view_key=? AND epoch_id=? ORDER BY ordinal",
            (view, epoch),
        ):
            items.extend(json.loads(payload))
        connection.exec_driver_sql(
            "UPDATE prompt_projections SET payload_json=? WHERE view_key=?",
            (json.dumps(items, ensure_ascii=False, separators=(",", ":")), view),
        )
    op.drop_table("prompt_projection_chunks")
    op.drop_column("prompt_projections", "tail_ordinal")
    op.drop_column("prompt_projections", "item_count")
    op.drop_index("ix_context_selection_range", table_name="model_context_selections")
    op.drop_column("model_context_selections", "min_event_id")
    op.drop_column("model_context_selections", "max_event_id")
    from yuki.conversation.projection_compact_schema import PROJECTION_TRIGGERS_0119

    for statement in PROJECTION_TRIGGERS_0119.values():
        connection.exec_driver_sql(statement)
