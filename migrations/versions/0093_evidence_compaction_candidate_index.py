"""Index exact fact/count/terminal evidence compaction receipt checks."""

import sqlalchemy as sa
from alembic import op

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None

# Frozen ownership: never import the current ORM into a historical migration.
INDEXES = (
    (
        "ix_evidence_compaction_items_fact_before_status",
        "memory_evidence_compaction_items",
        ("fact_id", "evidence_before", "status"),
    ),
)


def _validate(name: str, table: str, columns: tuple[str, ...], *, required: bool) -> bool:
    bind = op.get_bind()
    target = bind.execute(
        sa.text("SELECT type FROM sqlite_master WHERE name=:name"), {"name": table}
    ).scalar()
    available = {row[1] for row in bind.exec_driver_sql(f'PRAGMA table_info("{table}")')}
    if target != "table" or not set(columns) <= available:
        raise RuntimeError(f"evidence compaction index table shape mismatch: {table}")
    existing = bind.execute(
        sa.text("SELECT type,tbl_name FROM sqlite_master WHERE name=:name"),
        {"name": name},
    ).first()
    if existing is None:
        if required:
            raise RuntimeError(f"evidence compaction index missing: {name}")
        return False
    if existing[0] != "index" or existing[1] != table:
        raise RuntimeError(f"evidence compaction index shape mismatch: {name}")
    shape = next(
        (row for row in bind.exec_driver_sql(f'PRAGMA index_list("{table}")') if row[1] == name),
        None,
    )
    keys = tuple(
        (row[2], row[3], row[4])
        for row in bind.exec_driver_sql(f'PRAGMA index_xinfo("{name}")')
        if row[5] == 1
    )
    if (
        shape is None
        or shape[2] != 0
        or shape[3] != "c"
        or shape[4] != 0
        or keys != tuple((column, 0, "BINARY") for column in columns)
    ):
        raise RuntimeError(f"evidence compaction index shape mismatch: {name}")
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
