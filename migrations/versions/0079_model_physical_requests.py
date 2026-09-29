"""Separate logical model calls from dispatched provider HTTP requests."""

import sqlalchemy as sa
from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical rows stay NULL: their retry and native-search request counts
    # cannot be reconstructed from one logical invocation row.
    op.add_column("model_invocations", sa.Column("physical_request_count", sa.Integer()))
    op.add_column("model_invocations", sa.Column("unknown_usage_request_count", sa.Integer()))
    op.add_column("model_invocations", sa.Column("native_search_requested", sa.Boolean()))


def downgrade() -> None:
    with op.batch_alter_table("model_invocations") as batch:
        batch.drop_column("native_search_requested")
        batch.drop_column("unknown_usage_request_count")
        batch.drop_column("physical_request_count")
