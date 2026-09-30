"""Index receipt references used by bounded SelfReflection cleanup."""

import sqlalchemy as sa
from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_memory_evidence_tool_receipt"
PREDICATE = "tool_receipt_id IS NOT NULL"


def upgrade() -> None:
    bind = op.get_bind()
    existing = bind.execute(
        sa.text("SELECT tbl_name, sql FROM sqlite_master WHERE type='index' AND name=:name"),
        {"name": INDEX_NAME},
    ).first()
    if existing is not None:
        # Historical fixture databases built from current metadata may carry
        # this index already; an incompatible retained shape is an error.
        normalized = " ".join(str(existing[1]).lower().split())
        columns = [row[2] for row in bind.exec_driver_sql(f"PRAGMA index_info('{INDEX_NAME}')")]
        if (
            existing[0] != "memory_evidence"
            or columns != ["tool_receipt_id"]
            or normalized.split(" where ", 1)[-1] != PREDICATE.lower()
            or " unique index " in normalized
        ):
            raise RuntimeError(f"memory receipt reference index shape mismatch: {INDEX_NAME}")
        return
    op.create_index(
        INDEX_NAME, "memory_evidence", ["tool_receipt_id"], sqlite_where=sa.text(PREDICATE)
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="memory_evidence")
