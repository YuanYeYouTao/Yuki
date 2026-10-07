"""Retire only MCP lifecycle metadata and tools-list cache.

Downgrade recreates empty derived tables; deleted server/cache data is not restored.
Shared tool artifacts, invocation diagnostics and Memory evidence are unchanged.
"""

import re

import sqlalchemy as sa
from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None

# Static frozen 0048 shapes. Do not import current ORM or protocol modules.
_RESTORE_STATEMENTS = (
    """CREATE TABLE mcp_server_states (
	server_id VARCHAR(64) NOT NULL,
	transport VARCHAR(32) NOT NULL,
	config_hash VARCHAR(64) NOT NULL,
	enabled BOOLEAN NOT NULL,
	lifecycle VARCHAR(32) NOT NULL,
	status VARCHAR(32) NOT NULL,
	protocol_version VARCHAR(64) NOT NULL,
	server_name VARCHAR(255) NOT NULL,
	server_version VARCHAR(128) NOT NULL,
	server_instructions TEXT NOT NULL,
	last_connected_at DATETIME,
	last_refreshed_at DATETIME,
	last_error_category VARCHAR(128),
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (server_id)
)""",
    """CREATE TABLE mcp_tool_cache (
	id INTEGER NOT NULL,
	server_id VARCHAR(64) NOT NULL,
	remote_tool_name VARCHAR(255) NOT NULL,
	model_name VARCHAR(128) NOT NULL,
	description TEXT NOT NULL,
	compact_description VARCHAR(500) NOT NULL,
	input_schema_json TEXT NOT NULL,
	output_schema_json TEXT NOT NULL,
	annotations_json TEXT NOT NULL,
	metadata_hash VARCHAR(64) NOT NULL,
	refreshed_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (model_name),
	CONSTRAINT uq_mcp_tool_cache_server_tool UNIQUE (server_id, remote_tool_name)
)""",
    """CREATE INDEX ix_mcp_tool_cache_server ON mcp_tool_cache (server_id)""",
)


# Frozen columns: (name, declared type, NOT NULL, PK position).
_COLUMNS: dict[str, tuple[tuple[str, str, int, int], ...]] = {
    "mcp_server_states": (
        ("server_id", "VARCHAR(64)", 1, 1),
        ("transport", "VARCHAR(32)", 1, 0),
        ("config_hash", "VARCHAR(64)", 1, 0),
        ("enabled", "BOOLEAN", 1, 0),
        ("lifecycle", "VARCHAR(32)", 1, 0),
        ("status", "VARCHAR(32)", 1, 0),
        ("protocol_version", "VARCHAR(64)", 1, 0),
        ("server_name", "VARCHAR(255)", 1, 0),
        ("server_version", "VARCHAR(128)", 1, 0),
        ("server_instructions", "TEXT", 1, 0),
        ("last_connected_at", "DATETIME", 0, 0),
        ("last_refreshed_at", "DATETIME", 0, 0),
        ("last_error_category", "VARCHAR(128)", 0, 0),
        ("updated_at", "DATETIME", 1, 0),
    ),
    "mcp_tool_cache": (
        ("id", "INTEGER", 1, 1),
        ("server_id", "VARCHAR(64)", 1, 0),
        ("remote_tool_name", "VARCHAR(255)", 1, 0),
        ("model_name", "VARCHAR(128)", 1, 0),
        ("description", "TEXT", 1, 0),
        ("compact_description", "VARCHAR(500)", 1, 0),
        ("input_schema_json", "TEXT", 1, 0),
        ("output_schema_json", "TEXT", 1, 0),
        ("annotations_json", "TEXT", 1, 0),
        ("metadata_hash", "VARCHAR(64)", 1, 0),
        ("refreshed_at", "DATETIME", 1, 0),
    ),
}
_INDEXES: dict[str, set[tuple[int, str, tuple[str, ...]]]] = {
    "mcp_server_states": {(1, "pk", ("server_id",))},
    "mcp_tool_cache": {
        (1, "u", ("model_name",)),
        (1, "u", ("server_id", "remote_tool_name")),
        (0, "c", ("server_id",)),
    },
}


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _type(name: str) -> str:
    # Keep widths and INTEGER rowid semantics. Only equivalent spellings unify.
    value = re.sub(r"\s+", "", name.upper())
    if value == "BOOL":
        return "BOOLEAN"
    return value.replace("CHARACTERVARYING(", "VARCHAR(")


def _preflight() -> bool:
    bind = op.get_bind()
    objects = bind.execute(sa.text("SELECT type,name,tbl_name,sql FROM sqlite_schema")).all()
    present = {row[1] for row in objects if row[0] == "table"} & set(_COLUMNS)
    if not present:
        # Upstream 0096 already retired both tables. The experiment's published
        # 0096 instead installed invocation indexes; both readers converge here.
        return False
    if present != set(_COLUMNS):
        raise RuntimeError("MCP retirement partial table shape mismatch")
    for table, expected in _COLUMNS.items():
        definition = next((row for row in objects if row[1] == table), None)
        if definition is None or definition[0] != "table" or definition[3] is None:
            raise RuntimeError("MCP retirement table shape mismatch")
        columns = bind.exec_driver_sql(f"PRAGMA table_xinfo({_quoted(table)})").all()
        actual = tuple((row[1], _type(row[2]), row[3], row[5]) for row in columns)
        if actual != expected or any(row[4] is not None or row[6] != 0 for row in columns):
            raise RuntimeError("MCP retirement table shape mismatch")
        if re.search(r"\b(CHECK|STRICT|WITHOUT)\b", definition[3], re.IGNORECASE):
            raise RuntimeError("MCP retirement table shape mismatch")
        indexes = bind.exec_driver_sql(f"PRAGMA index_list({_quoted(table)})").all()
        shapes = []
        for index in indexes:
            keys = bind.exec_driver_sql(f"PRAGMA index_xinfo({_quoted(index[1])})").all()
            selected = [row for row in keys if row[5] == 1]
            if index[4] or any(row[2] is None or row[3] or row[4] != "BINARY" for row in selected):
                raise RuntimeError("MCP retirement index shape mismatch")
            if index[3] == "c" and index[1] != "ix_mcp_tool_cache_server":
                raise RuntimeError("MCP retirement index shape mismatch")
            shapes.append((index[2], index[3], tuple(row[2] for row in selected)))
        if set(shapes) != _INDEXES[table] or len(shapes) != len(_INDEXES[table]):
            raise RuntimeError("MCP retirement index shape mismatch")
    # Metadata only: reject foreign ownership/dependencies before either DROP.
    # Conservative identifier matching also rejects ambiguous literal/comment references.
    references = re.compile(r"\b(?:mcp_server_states|mcp_tool_cache)\b", re.IGNORECASE)
    for kind, name, owner, sql in objects:
        if kind == "table":
            foreign_keys = bind.exec_driver_sql(f"PRAGMA foreign_key_list({_quoted(name)})")
            if any(str(row[2]).lower() in _COLUMNS or name in _COLUMNS for row in foreign_keys):
                raise RuntimeError("MCP retirement foreign key dependency")
        elif kind in {"view", "trigger"} and (owner in _COLUMNS or references.search(sql or "")):
            raise RuntimeError("MCP retirement schema dependency")
    return True


INDEXES = {
    "ix_runtime_effects_parent": "CREATE INDEX IF NOT EXISTS ix_runtime_effects_parent ON runtime_work_effects (work_id, json_extract(receipt_json, '$.invocation.parent_effect_key'), effect_key) WHERE json_extract(receipt_json, '$.invocation.version') = 1",
    "ux_runtime_effects_child_ordinal": "CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_effects_child_ordinal ON runtime_work_effects (work_id, json_extract(receipt_json, '$.invocation.parent_effect_key'), json_extract(receipt_json, '$.invocation.child_ordinal')) WHERE json_extract(receipt_json, '$.invocation.version') = 1 AND json_type(receipt_json, '$.invocation.parent_effect_key') = 'text'",
    "ux_runtime_effects_engine_call": "CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_effects_engine_call ON runtime_work_effects (work_id, json_extract(receipt_json, '$.invocation.parent_effect_key'), json_extract(receipt_json, '$.invocation.feed_index'), json_extract(receipt_json, '$.invocation.engine_call_id')) WHERE json_extract(receipt_json, '$.invocation.version') = 1 AND json_type(receipt_json, '$.invocation.parent_effect_key') = 'text'",
}


_SQL_ASCII_CASE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


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
    retire = _preflight()
    present = {name: _validate_invocation(name, required=False) for name in INDEXES}
    for name, statement in INDEXES.items():
        if not present[name]:
            op.execute(statement)
    if retire:
        op.drop_table("mcp_tool_cache")
        op.drop_table("mcp_server_states")


def downgrade() -> None:
    # A compatible experimental 0096 reader requires these indexes. Keep them,
    # including original invocation facts; its own downgrade retains its guard.
    for name in INDEXES:
        _validate_invocation(name, required=True)
    for statement in _RESTORE_STATEMENTS:
        op.execute(statement)
