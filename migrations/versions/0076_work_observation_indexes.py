"""Index bounded operator reads without changing Work identity or state."""

from alembic import op
from sqlalchemy import inspect

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_work_inputs_work_id", "runtime_work_inputs", ["work_id", "id"]),
    ("ix_work_effects_work_updated", "runtime_work_effects", ["work_id", "updated", "effect_key"]),
    ("ix_work_children_root", "runtime_subagents", ["root_id", "work_id"]),
    ("ix_work_waits_work_created", "runtime_work_waits", ["work_id", "created", "id"]),
)


def upgrade() -> None:
    for name, table, columns in INDEXES:
        existing = next(
            (index for index in inspect(op.get_bind()).get_indexes(table) if index["name"] == name),
            None,
        )
        # Metadata-created test databases and the frozen Table-based migration
        # chain can already have the declared indexes. Never accept a wrong shape.
        if existing is not None:
            if existing["column_names"] != columns or existing["unique"]:
                raise RuntimeError("work observation index shape mismatch")
        else:
            op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
