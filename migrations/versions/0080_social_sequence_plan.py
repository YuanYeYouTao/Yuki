"""Keep prospective split-send plan size without inferring historical payloads."""

import sqlalchemy as sa
from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing parents have only a payload hash. Leave their plan unknowable.
    if "planned_parts" not in {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("social_operation_receipts")
    }:
        op.add_column("social_operation_receipts", sa.Column("planned_parts", sa.Integer()))


def downgrade() -> None:
    # Preserve receipts when rolling back code; a later upgrade can still audit plans.
    pass
