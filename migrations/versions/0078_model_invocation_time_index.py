"""Index the bounded model usage windows shown in the control panel."""

import sqlalchemy as sa
from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    name = "ix_model_invocations_created"
    existing = bind.scalar(
        sa.text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"),
        {"name": name},
    )
    if existing is not None:
        columns = [row[2] for row in bind.exec_driver_sql(f"PRAGMA index_info('{name}')")]
        if columns != ["created_at"] or " unique index " in " ".join(str(existing).lower().split()):
            raise RuntimeError("model invocation time index shape mismatch")
        return
    op.create_index(name, "model_invocations", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_model_invocations_created", table_name="model_invocations")
