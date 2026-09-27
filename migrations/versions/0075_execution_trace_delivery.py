"""Link confirmed outgoing ledger events to their actual diagnostic turn."""

import sqlalchemy as sa
from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    previous_sequence = _sequence()
    with op.batch_alter_table(
        "execution_trace_entries", table_kwargs={"sqlite_autoincrement": True}
    ) as batch:
        batch.add_column(sa.Column("delivered_event_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_execution_trace_delivered_event",
            "chat_events",
            ["delivered_event_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_execution_trace_delivered_event", ["delivered_event_id", "id"])
    _preserve_sequence(previous_sequence)


def downgrade() -> None:
    previous_sequence = _sequence()
    with op.batch_alter_table(
        "execution_trace_entries", table_kwargs={"sqlite_autoincrement": True}
    ) as batch:
        batch.drop_index("ix_execution_trace_delivered_event")
        batch.drop_constraint("fk_execution_trace_delivered_event", type_="foreignkey")
        batch.drop_column("delivered_event_id")
    _preserve_sequence(previous_sequence)


def _sequence() -> int:
    if op.get_bind().dialect.name != "sqlite":
        return 0
    return (
        op.get_bind().scalar(
            sa.text("SELECT seq FROM sqlite_sequence WHERE name='execution_trace_entries'")
        )
        or 0
    )


def _preserve_sequence(previous: int) -> None:
    # A table copy retains max(id), but must also retain IDs erased by privacy
    # deletion or expiry. Never let an old diagnostic URL identify a new record.
    if previous > _sequence():
        op.get_bind().execute(
            sa.text("DELETE FROM sqlite_sequence WHERE name='execution_trace_entries'")
        )
        op.get_bind().execute(
            sa.text(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('execution_trace_entries', :seq)"
            ),
            {"seq": previous},
        )
