"""Index global Work directory and local recent-state reads without changing facts."""

import sqlalchemy as sa
from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None

# Frozen SQL: migrations must not import mutable runtime tables or projections.
SOURCE_FIELDS = (
    "actor_user_id",
    "actor_person_id",
    "origin",
    "plugin_id",
    "delegation_id",
    "execution_boundary",
    "principal_kind",
    "initiative_run_id",
)
INDEX_SQL = {
    "ix_runtime_work_query_scope_updated": (
        "CREATE INDEX ix_runtime_work_query_scope_updated ON runtime_work "
        "(conversation_id, generation, "
        + ", ".join(f"json_extract(source_json, '$.{key}')" for key in SOURCE_FIELDS)
        + ", updated DESC, id DESC)"
    ),
    "ix_runtime_work_query_updated": (
        "CREATE INDEX ix_runtime_work_query_updated ON runtime_work (updated DESC, id DESC)"
    ),
    "ix_runtime_work_query_state_updated": (
        "CREATE INDEX ix_runtime_work_query_state_updated "
        "ON runtime_work (state, updated DESC, id DESC)"
    ),
}


def _normalized(sql: str) -> str:
    # JSON paths are case-sensitive; never fold string literals with SQL tokens.
    return "".join(sql.replace('"', "").split())


def _retained(name: str, expected: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT tbl_name, sql FROM sqlite_master WHERE type='index' AND name=:name"),
        {"name": name},
    ).first()
    if row is None:
        return False
    if row[0] != "runtime_work" or row[1] is None or _normalized(row[1]) != _normalized(expected):
        raise RuntimeError(f"Work query index shape mismatch: {name}")
    return True


def upgrade() -> None:
    present = {name for name, sql in INDEX_SQL.items() if _retained(name, sql)}
    for name, sql in INDEX_SQL.items():
        if name not in present:
            op.execute(sa.text(sql))


def downgrade() -> None:
    for name, sql in INDEX_SQL.items():
        if not _retained(name, sql):
            raise RuntimeError(f"Work query index is missing: {name}")
    for name in reversed(INDEX_SQL):
        op.drop_index(name, table_name="runtime_work")
