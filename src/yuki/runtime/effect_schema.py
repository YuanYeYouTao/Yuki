"""Current indexes without mutating the frozen 0056 table declarations."""

from sqlalchemy import Connection

INVOCATION_INDEXES = {
    "ix_runtime_effects_domain_ref": "CREATE INDEX IF NOT EXISTS ix_runtime_effects_domain_ref "
    "ON runtime_work_effects (json_extract(receipt_json, '$.invocation.original_domain_ref'), "
    "work_id)",
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
