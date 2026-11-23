"""Partial index for SELF concerns in the resident segment (#541 P2).

A concern is a SELF fact with ``category = 'concern'`` (no new kind, no table rebuild).
Every Main turn lists the concerns the latest finished Dream run had seen; without
this index that is a scan over every SELF fact. The index holds only concern rows;
its key starts with the two equality columns so the planner prefers it over
``ix_memory_facts_scope_status_updated`` without table statistics.

Downgrade drops the index. Frozen: does not import mutable ORM models.
"""

import sqlalchemy as sa
from alembic import op

revision = "0168"
down_revision = "0167"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_memory_facts_self_concern",
        "memory_facts",
        ["scope_type", "category", "id"],
        sqlite_where=sa.text("category = 'concern'"),
    )


def downgrade() -> None:
    op.drop_index("ix_memory_facts_self_concern", table_name="memory_facts")
