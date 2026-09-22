"""Internal Social event anchors are additive and never inferred from old platform IDs."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from tests.unit.test_migration_0066 import _schema

from qq_ai_bot.social.db_models import SocialOperationModel


def test_social_event_reference_preserves_history_and_survives_event_deletion(monkeypatch):
    migration = importlib.import_module("migrations.versions.0069_social_receipt_event_reference")
    assert migration.down_revision == "0068"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("CREATE TABLE chat_events (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE social_operation_receipts (id TEXT PRIMARY KEY, "
            "status TEXT, platform_reference TEXT)"
        )
        connection.exec_driver_sql("INSERT INTO chat_events VALUES (1)")
        connection.exec_driver_sql(
            "INSERT INTO social_operation_receipts VALUES ('old', 'succeeded', '1')"
        )
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert (
            connection.exec_driver_sql("SELECT event_id FROM social_operation_receipts").scalar()
            is None
        )
        connection.exec_driver_sql("UPDATE social_operation_receipts SET event_id=1 WHERE id='old'")
        before = _schema(connection, "social_operation_receipts")
        migration.downgrade()
        migration.upgrade()
        migration.upgrade()
        assert _schema(connection, "social_operation_receipts") == before
        assert (
            connection.exec_driver_sql("SELECT event_id FROM social_operation_receipts").scalar()
            == 1
        )
        connection.exec_driver_sql("DELETE FROM chat_events WHERE id=1")
        assert connection.exec_driver_sql(
            "SELECT status, platform_reference, event_id FROM social_operation_receipts"
        ).one() == ("succeeded", "1", None)
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_social_event_migration_matches_runtime_metadata(monkeypatch):
    original = importlib.import_module("migrations.versions.0052_social_operation_receipts")
    migration = importlib.import_module("migrations.versions.0069_social_receipt_event_reference")
    deployed, runtime = create_engine("sqlite:///:memory:"), create_engine("sqlite:///:memory:")
    with deployed.begin() as migrated, runtime.begin() as metadata:
        for connection in (migrated, metadata):
            connection.exec_driver_sql("CREATE TABLE chat_events (id INTEGER PRIMARY KEY)")
            for table in ("canonical_conversations", "presences"):
                connection.exec_driver_sql(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
        for module in (original, migration):
            monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(migrated)))
            module.upgrade()
        SocialOperationModel.__table__.create(metadata)
        schemas = []
        for connection in (migrated, metadata):
            schema = _schema(connection, "social_operation_receipts")
            # SQLAlchemy's CREATE TABLE parser omits actions on SQLite's inline
            # ALTER-added FK. PRAGMA reports the constraints SQLite enforces.
            schema["foreign_keys"] = sorted(
                tuple(row[2:7])
                for row in connection.exec_driver_sql(
                    "PRAGMA foreign_key_list(social_operation_receipts)"
                )
            )
            schemas.append(schema)
        assert schemas[0] == schemas[1]
    deployed.dispose()
    runtime.dispose()
