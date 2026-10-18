"""Persist Person authority and original chat management request association."""

import sqlalchemy as sa
from alembic import op

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Qualification is assigned explicitly to a Person after schema upgrade.
    # Never read the old platform account environment or reconstruct ownership.
    op.add_column(
        "persons",
        sa.Column("is_superuser", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("control_command_receipts", sa.Column("source_event_id", sa.Integer()))
    op.add_column("control_command_receipts", sa.Column("expected_revision", sa.Integer()))
    op.create_index(
        "uq_control_command_receipts_source_event",
        "control_command_receipts",
        ["source_event_id"],
        unique=True,
        sqlite_where=sa.text("source_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_control_command_receipts_source_event", table_name="control_command_receipts")
    op.drop_column("control_command_receipts", "expected_revision")
    op.drop_column("control_command_receipts", "source_event_id")
    op.drop_column("persons", "is_superuser")
