"""Index bounded media expiry and global web-search retention discovery."""

import sqlalchemy as sa
from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_media_analyses_expires_at", "media_analyses", "expires_at"),
    ("ix_web_search_runs_created_at", "web_search_runs", "created_at"),
)


def _retained_index(name: str, table: str, column: str) -> bool:
    bind = op.get_bind()
    existing = bind.execute(
        sa.text("SELECT tbl_name, sql FROM sqlite_master WHERE type='index' AND name=:name"),
        {"name": name},
    ).first()
    if existing is None:
        return False
    index = next(
        (row for row in bind.exec_driver_sql(f'PRAGMA index_list("{table}")') if row[1] == name),
        None,
    )
    keys = tuple(
        (row[2], row[3], row[4])
        for row in bind.exec_driver_sql(f'PRAGMA index_xinfo("{name}")')
        if row[5] == 1
    )
    if (
        existing[0] != table
        or existing[1] is None
        or index is None
        or index[2] != 0
        or index[3] != "c"
        or index[4] != 0
        or keys != ((column, 0, "BINARY"),)
    ):
        raise RuntimeError(f"cache cleanup index shape mismatch: {name}")
    return True


def upgrade() -> None:
    # Current-metadata historical fixtures may already contain these indexes.
    # Validate every retained shape before creating any missing index.
    present = {name for name, table, column in INDEXES if _retained_index(name, table, column)}
    for name, table, column in INDEXES:
        if name not in present:
            op.create_index(name, table, [column])


def downgrade() -> None:
    for name, table, column in INDEXES:
        if not _retained_index(name, table, column):
            raise RuntimeError(f"cache cleanup index is missing: {name}")
    for name, table, _column in reversed(INDEXES):
        op.drop_index(name, table_name=table)
