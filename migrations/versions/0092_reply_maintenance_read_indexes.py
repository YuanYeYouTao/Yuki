"""Index ordinary feedback scope reads and bounded protocol GC cursor pages."""

import sqlalchemy as sa
from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None

# Frozen shapes: historical migrations must not import mutable ORM metadata.
INDEXES = (
    (
        "ix_social_operation_scope_updated",
        "social_operation_receipts",
        ("source_conversation_id", "updated_at"),
    ),
    (
        "ix_protocol_objects_gc_cursor",
        "runtime_protocol_objects",
        ("deleting", "prepared_at", "sha256"),
    ),
)


def _validate(name: str, table: str, columns: tuple[str, ...], *, required: bool) -> bool:
    bind = op.get_bind()
    existing = bind.execute(
        sa.text("SELECT type,tbl_name FROM sqlite_master WHERE name=:name"),
        {"name": name},
    ).first()
    if existing is None:
        if required:
            raise RuntimeError(f"reply maintenance index missing: {name}")
        return False
    if existing[0] != "index":
        raise RuntimeError(f"reply maintenance index shape mismatch: {name}")
    actual_columns = tuple(row[2] for row in bind.exec_driver_sql(f"PRAGMA index_info('{name}')"))
    keys = tuple(row for row in bind.exec_driver_sql(f"PRAGMA index_xinfo('{name}')") if row[5])
    shape = next(
        (row for row in bind.exec_driver_sql(f"PRAGMA index_list('{table}')") if row[1] == name),
        None,
    )
    if (
        existing[1] != table
        or actual_columns != columns
        or shape is None
        or shape[2]
        or shape[3] != "c"
        or shape[4]
        or any(row[3] or row[4] != "BINARY" for row in keys)
    ):
        raise RuntimeError(f"reply maintenance index shape mismatch: {name}")
    return True


def upgrade() -> None:
    present = tuple(_validate(*spec, required=False) for spec in INDEXES)
    for (name, table, columns), exists in zip(INDEXES, present, strict=True):
        if not exists:
            op.create_index(name, table, list(columns))


def downgrade() -> None:
    for spec in INDEXES:
        _validate(*spec, required=True)
    for name, table, _columns in reversed(INDEXES):
        op.drop_index(name, table_name=table)
