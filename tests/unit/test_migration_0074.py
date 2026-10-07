"""Diagnostics migrate independently from existing business state and receipts."""

import sqlite3

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from qq_ai_bot.execution_trace.db_models import ExecutionTraceEntryModel, ExecutionTraceStateModel
from qq_ai_bot.persistence.metadata import Base


def test_upgrade_preserves_business_rows_and_matches_metadata(tmp_path, monkeypatch):
    path = tmp_path / "trace-migration.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    command.upgrade(config, "0073")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO admin_operation_events (actor_user_id, trigger_message_id, "
            "conversation_key, capability, operation, target_type, target_id, "
            "before_json, after_json, success, duration_seconds, created_at) "
            "VALUES ('fixture', '', '', 'control.config.read', 'read', 'config', "
            "'', '{}', '{}', 1, 0, '2026-09-27')"
        )
        previous = db.execute("SELECT * FROM admin_operation_events").fetchall()
    # Current trace metadata includes later indexes; stop before the destructive
    # 0097 retirement so this diagnostic-only downgrade remains a real round trip.
    command.upgrade(config, "0096")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0096",)
        assert db.execute("SELECT * FROM admin_operation_events").fetchall() == previous
        assert db.execute("SELECT count(*) FROM execution_trace_entries").fetchone() == (0,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    migrated = create_engine(f"sqlite:///{path.as_posix()}")
    runtime = create_engine("sqlite://")
    try:
        Base.metadata.create_all(runtime)
        for model in (ExecutionTraceEntryModel, ExecutionTraceStateModel):
            name = model.__tablename__

            def schema(engine, name=name):
                inspector = inspect(engine)
                return (
                    [
                        (c["name"], str(c["type"]), c["nullable"])
                        for c in inspector.get_columns(name)
                    ],
                    sorted(
                        (i["name"], tuple(i["column_names"])) for i in inspector.get_indexes(name)
                    ),
                    sorted(
                        inspector.get_foreign_keys(name),
                        key=lambda fk: fk["constrained_columns"],
                    ),
                )

            assert schema(migrated) == schema(runtime)
    finally:
        migrated.dispose()
        runtime.dispose()
    command.downgrade(config, "0073")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM admin_operation_events").fetchall() == previous
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'execution_trace_%'"
            ).fetchall()
            == []
        )
