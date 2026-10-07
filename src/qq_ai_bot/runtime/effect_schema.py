"""Current indexes without mutating the frozen 0056 table declarations."""

from sqlalchemy import Connection

INVOCATION_INDEXES = {
    "ix_runtime_effects_parent": "CREATE INDEX IF NOT EXISTS ix_runtime_effects_parent "
    "ON runtime_work_effects (work_id, "
    "json_extract(receipt_json, '$.invocation.parent_effect_key'), effect_key) "
    "WHERE json_extract(receipt_json, '$.invocation.version') = 1",
    "ux_runtime_effects_child_ordinal": "CREATE UNIQUE INDEX IF NOT EXISTS "
    "ux_runtime_effects_child_ordinal ON runtime_work_effects (work_id, "
    "json_extract(receipt_json, '$.invocation.parent_effect_key'), "
    "json_extract(receipt_json, '$.invocation.child_ordinal')) "
    "WHERE json_extract(receipt_json, '$.invocation.version') = 1 "
    "AND json_type(receipt_json, '$.invocation.parent_effect_key') = 'text'",
    "ux_runtime_effects_engine_call": "CREATE UNIQUE INDEX IF NOT EXISTS "
    "ux_runtime_effects_engine_call ON runtime_work_effects (work_id, "
    "json_extract(receipt_json, '$.invocation.parent_effect_key'), "
    "json_extract(receipt_json, '$.invocation.feed_index'), "
    "json_extract(receipt_json, '$.invocation.engine_call_id')) "
    "WHERE json_extract(receipt_json, '$.invocation.version') = 1 "
    "AND json_type(receipt_json, '$.invocation.parent_effect_key') = 'text'",
}


def install_indexes(connection: Connection) -> None:
    definitions = {
        "work_key": "work_id, effect_key",
        "work_updated": "work_id, updated DESC, effect_key DESC",
        "work_state": "work_id, state, effect_key",
        **{
            f"work_{field}": f"work_id, json_extract(receipt_json, '$.outcome.{field}'), effect_key"
            for field in ("pending", "uncertain", "run_id")
        },
    }
    for suffix, columns in definitions.items():
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_runtime_effects_{suffix} "
            f"ON runtime_work_effects ({columns})"
        )
    for statement in INVOCATION_INDEXES.values():
        connection.exec_driver_sql(statement)
