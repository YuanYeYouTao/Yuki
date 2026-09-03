"""Distinguish evaluated recall from missing attribution (content-free only)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE memory_recall_receipts ADD COLUMN consumer "
        "VARCHAR(24) NOT NULL DEFAULT 'unknown'"
    )
    op.execute(
        "ALTER TABLE memory_recall_receipts ADD COLUMN attribution_status "
        "VARCHAR(16) NOT NULL DEFAULT 'unknown' CHECK "
        "(attribution_status IN ('unknown','pending','succeeded','failed','skipped'))"
    )
    op.execute("ALTER TABLE memory_recall_receipts ADD COLUMN attribution_reason VARCHAR(32)")
    op.execute("ALTER TABLE memory_recall_receipts ADD COLUMN attribution_completed_at DATETIME")
    for outcome in (
        "success",
        "empty",
        "ambiguous",
        "permission_denied",
        "duplicate",
        "infrastructure_failure",
    ):
        op.execute(
            "ALTER TABLE memory_recall_receipts ADD COLUMN "
            f"tool_read_{outcome}_count INTEGER NOT NULL DEFAULT 0"
        )
    op.execute(
        "ALTER TABLE memory_recall_items ADD COLUMN attribution_evaluated "
        "BOOLEAN NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.drop_column("memory_recall_items", "attribution_evaluated")
    for column in (
        "tool_read_infrastructure_failure_count",
        "tool_read_duplicate_count",
        "tool_read_permission_denied_count",
        "tool_read_ambiguous_count",
        "tool_read_empty_count",
        "tool_read_success_count",
        "attribution_completed_at",
        "attribution_reason",
        "attribution_status",
        "consumer",
    ):
        op.drop_column("memory_recall_receipts", column)
