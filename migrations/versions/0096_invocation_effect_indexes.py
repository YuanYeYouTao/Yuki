"""Index versioned original invocation identities; preserve legacy receipts."""

import re

import sqlalchemy as sa
from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None

# Frozen migration SQL; never import future runtime declarations.
INDEXES = {
    "ix_runtime_effects_parent": "CREATE INDEX IF NOT EXISTS ix_runtime_effects_parent ON runtime_work_effects (work_id, json_extract(receipt_json, '$.invocation.parent_effect_key'), effect_key) WHERE json_extract(receipt_json, '$.invocation.version') = 1",
    "ux_runtime_effects_child_ordinal": "CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_effects_child_ordinal ON runtime_work_effects (work_id, json_extract(receipt_json, '$.invocation.parent_effect_key'), json_extract(receipt_json, '$.invocation.child_ordinal')) WHERE json_extract(receipt_json, '$.invocation.version') = 1 AND json_type(receipt_json, '$.invocation.parent_effect_key') = 'text'",
    "ux_runtime_effects_engine_call": "CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_effects_engine_call ON runtime_work_effects (work_id, json_extract(receipt_json, '$.invocation.parent_effect_key'), json_extract(receipt_json, '$.invocation.feed_index'), json_extract(receipt_json, '$.invocation.engine_call_id')) WHERE json_extract(receipt_json, '$.invocation.version') = 1 AND json_type(receipt_json, '$.invocation.parent_effect_key') = 'text'",
}


# 0092 existed on both branches with different additive index sets. Never stamp
# or rewrite old business facts: ensure the upstream shapes while upgrading
# either a real main database or the original experiment's 0092 database.
LEGACY_MAIN_INDEXES = (
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


_SQL_ASCII_CASE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _validate_legacy_main(
    name: str, table: str, columns: tuple[str, ...], *, required: bool
) -> bool:
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


def _normalized(sql: str) -> str:
    # Keyword/formatting differences are harmless; JSON paths are case sensitive.
    # SQLite omits the declaration's IF NOT EXISTS. Never remove that substring
    # from identifiers: a changed column must remain a shape mismatch.
    sql = re.sub(
        r"\A(\s*create(?:\s+unique)?\s+index)\s+if\s+not\s+exists\b",
        r"\1",
        sql,
        flags=re.IGNORECASE | re.ASCII,
    )
    parts = re.split(r"('(?:''|[^'])*')", sql)
    return "".join(
        part if index % 2 else re.sub(r"\s+", "", part, flags=re.ASCII).translate(_SQL_ASCII_CASE)
        for index, part in enumerate(parts)
    )


def _validate_invocation(name: str, *, required: bool) -> bool:
    existing = (
        op.get_bind()
        .execute(
            sa.text("SELECT type,tbl_name,sql FROM sqlite_master WHERE name=:name"), {"name": name}
        )
        .first()
    )
    if existing is None:
        if required:
            raise RuntimeError(f"invocation index missing: {name}")
        return False
    if (
        existing[0] != "index"
        or existing[1] != "runtime_work_effects"
        or _normalized(existing[2] or "") != _normalized(INDEXES[name])
    ):
        raise RuntimeError(f"invocation index shape mismatch: {name}")
    return True


def upgrade() -> None:
    legacy = tuple(_validate_legacy_main(*spec, required=False) for spec in LEGACY_MAIN_INDEXES)
    present = {name: _validate_invocation(name, required=False) for name in INDEXES}
    for (name, table, columns), exists in zip(LEGACY_MAIN_INDEXES, legacy, strict=True):
        if not exists:
            op.create_index(name, table, list(columns))
    for name, statement in INDEXES.items():
        if not present[name]:
            op.execute(statement)


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM runtime_work_effects WHERE json_extract(receipt_json, '$.invocation.version') = 1 OR json_extract(receipt_json, '$.composition.version') = 1 LIMIT 1"
            )
        )
        .first()
    ):
        raise RuntimeError("versioned invocation facts exist; use a compatible reader")
    for name in INDEXES:
        _validate_invocation(name, required=True)
    for name in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
