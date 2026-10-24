"""Restore device tables without rewriting any original command or receipt."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def test_new_device_schema_and_existing_original_rows_survive_upgrade(monkeypatch):
    migration = importlib.import_module("migrations.versions.0152_desktop_local_devices")
    with create_engine("sqlite://").begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.execute(text("CREATE TABLE persons(id TEXT PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE runtime_work(id TEXT PRIMARY KEY, source_json TEXT)"))
        connection.execute(text("INSERT INTO persons VALUES('original-person')"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        connection.execute(
            text("""INSERT INTO client_executors
            (id,person_id,token_hash,label,platform,revision,paused,created_at)
            VALUES('original-device','original-person','original-token','original-label',
                   'windows',7,0,'2026-10-24')""")
        )
        connection.execute(
            text("""INSERT INTO executor_grants
            VALUES('original-device','folder.write','original-folder','original-digest',4)""")
        )
        connection.execute(
            text("""INSERT INTO executor_operations
            (op_id,executor_id,person_id,work_id,tool_call_key,capability,params_json,
             state,result_json,receipt_hash)
            VALUES('original-operation','original-device','original-person','original-work',
                   'original-call','folder.write','{ "original": true }','dispatched',
                   '{ "receipt": "unknown" }','original-hash')""")
        )
        tables = ("client_executors", "executor_grants", "executor_operations")
        original = {
            table: connection.execute(text(f"SELECT * FROM {table}")).all() for table in tables
        }
        # Older feature deployments may already hold these exact domain tables.
        migration.upgrade()
        assert {
            table: connection.execute(text(f"SELECT * FROM {table}")).all() for table in tables
        } == original
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert {
            row[1] for row in connection.execute(text("PRAGMA index_list(executor_operations)"))
        } >= {"ix_executor_operations_history"}
