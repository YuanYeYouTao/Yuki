"""Bound live execution and event-to-turn reads by their actual predicates."""

import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None

INDEXES = (
    (
        "ix_execution_trace_roots",
        ["conversation_id", "id"],
        "kind IN ('chat_processing_start', 'turn_start')",
    ),
    (
        "ix_execution_trace_source_event",
        ["source_event_id", "id"],
        "source_event_id IS NOT NULL",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    for name, columns, predicate in INDEXES:
        existing = bind.scalar(
            sa.text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"),
            {"name": name},
        )
        if existing is not None:
            # Metadata-created test databases can already carry model indexes.
            normalized = " ".join(str(existing).lower().split())
            actual_columns = [
                row[2] for row in bind.exec_driver_sql(f"PRAGMA index_info('{name}')")
            ]
            if (
                f"where {predicate.lower()}" not in normalized
                or actual_columns != columns
                or " unique index " in normalized
            ):
                raise RuntimeError(f"execution trace index shape mismatch: {name}")
            continue
        op.create_index(
            name,
            "execution_trace_entries",
            columns,
            sqlite_where=sa.text(predicate),
        )


def downgrade() -> None:
    for name, _, _ in reversed(INDEXES):
        op.drop_index(name, table_name="execution_trace_entries")
