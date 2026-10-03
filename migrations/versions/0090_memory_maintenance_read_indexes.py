"""Index bounded expired governance and processing Dream candidate reads."""

import sqlalchemy as sa
from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None

# Frozen migration shapes; do not derive historical DDL from current ORM metadata.
INDEXES = (
    (
        "ix_memory_reflection_jobs_status_claimed",
        "memory_reflection_jobs",
        ("status", "claimed_at", "id"),
    ),
    ("ix_memory_dream_clusters_status_id", "memory_dream_clusters", ("status", "id")),
)


def _validate(name: str, table: str, columns: tuple[str, ...], *, required: bool) -> bool:
    bind = op.get_bind()
    existing = bind.execute(
        sa.text("SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=:name"),
        {"name": name},
    ).scalar()
    if existing is None:
        if required:
            raise RuntimeError(f"maintenance index missing: {name}")
        return False
    actual_columns = tuple(row[2] for row in bind.exec_driver_sql(f"PRAGMA index_info('{name}')"))
    keys = tuple(row for row in bind.exec_driver_sql(f"PRAGMA index_xinfo('{name}')") if row[5])
    shape = next(
        (row for row in bind.exec_driver_sql(f"PRAGMA index_list('{table}')") if row[1] == name),
        None,
    )
    if (
        existing != table
        or actual_columns != columns
        or shape is None
        or shape[2]
        or shape[4]
        or any(row[3] or row[4] != "BINARY" for row in keys)
    ):
        raise RuntimeError(f"maintenance index shape mismatch: {name}")
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
