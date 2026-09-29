"""Split-send plans are prospective; old hashes cannot reveal their chunk count."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine


def test_sequence_plan_migration_keeps_old_parent_unknown(monkeypatch):
    migration = importlib.import_module("migrations.versions.0080_social_sequence_plan")
    assert migration.down_revision == "0079"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE social_operation_receipts (id TEXT PRIMARY KEY, "
            "action TEXT NOT NULL, payload_hash TEXT NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO social_operation_receipts VALUES "
            "('historical', 'send_message_sequence', 'irreversible-hash')"
        )
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert connection.exec_driver_sql(
            "SELECT planned_parts FROM social_operation_receipts WHERE id='historical'"
        ).scalar() is None
        connection.exec_driver_sql(
            "INSERT INTO social_operation_receipts VALUES "
            "('new', 'send_message_sequence', 'new-hash', 2)"
        )
        migration.downgrade()
        migration.upgrade()
        assert connection.exec_driver_sql(
            "SELECT id, planned_parts FROM social_operation_receipts ORDER BY id"
        ).all() == [("historical", None), ("new", 2)]
    engine.dispose()
