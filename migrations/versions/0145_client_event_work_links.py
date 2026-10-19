"""Locate original root Work for a bounded page of private ledger events."""

from alembic import op

revision = "0145"
down_revision = "0144"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_runtime_work_client_trigger ON runtime_work "
        "(conversation_id, json_extract(source_json, '$.trigger_event_id'), created, id) "
        "WHERE parent_work_id IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_runtime_work_client_trigger", table_name="runtime_work")
