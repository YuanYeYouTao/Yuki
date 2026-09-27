"""Control intents preserve historical receipts and match runtime constraints."""

import sqlite3
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel


def test_upgrade_keeps_receipts_and_accepts_only_complete_intents(tmp_path, monkeypatch):
    path = tmp_path / "migration.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    command.upgrade(config, "0072")
    principal, request = str(uuid4()), str(uuid4())
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO admin_operation_events (id, actor_user_id, trigger_message_id, "
            "conversation_key, capability, operation, target_type, target_id, before_json, "
            "after_json, success, duration_seconds, created_at) VALUES (1, ?, ?, '', "
            "'control.mcp.mutate', 'control.mcp.mutate', 'mcp', 'probe', '{}', '{}', "
            "0, 0, '2026-09-27')",
            (principal, request),
        )
        db.execute(
            "INSERT INTO control_command_receipts (principal_id, request_id, payload_hash, "
            "status, problem_code, audit_id, created_at, updated_at) VALUES (?, ?, ?, 'failed', "
            "'validation_error', 1, '2026-09-27', '2026-09-27')",
            (principal, request, "0" * 64),
        )
        previous = db.execute("SELECT * FROM control_command_receipts").fetchall()
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0073",)
        assert db.execute("SELECT * FROM control_command_receipts").fetchall() == previous
        assert db.execute(
            "SELECT actor_principal_id, control_request_id, trigger_message_id "
            "FROM admin_operation_events WHERE id=1"
        ).fetchone() == (principal, request, "")
        for status, problem in (("running", None), ("unknown", "process_restart")):
            new_request = str(uuid4())
            db.execute(
                "INSERT INTO control_command_receipts (principal_id, request_id, payload_hash, "
                "status, problem_code, audit_id, operation_kind, operation_ref, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, 'control', ?, '2026-09-27', '2026-09-27')",
                (
                    principal,
                    new_request,
                    "0" * 64,
                    status,
                    problem,
                    f"control:{principal}:{new_request}",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO control_command_receipts (principal_id, request_id, payload_hash, "
                "status, created_at, updated_at) VALUES (?, ?, ?, 'running', "
                "'2026-09-27', '2026-09-27')",
                (principal, str(uuid4()), "0" * 64),
            )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    migrated, runtime = create_engine(f"sqlite:///{path.as_posix()}"), create_engine("sqlite://")
    with runtime.begin() as connection:
        ControlCommandReceiptModel.__table__.create(connection)

    def checks(engine):
        return {
            row["name"]: "".join(row["sqltext"].split())
            for row in inspect(engine).get_check_constraints("control_command_receipts")
        }

    assert checks(migrated) == checks(runtime)
    migrated.dispose()
    runtime.dispose()
