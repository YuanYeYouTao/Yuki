"""Reply migration only records future authoritative ingress references."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from tests.unit.test_migration_0066 import _schema


def test_reply_reference_migration_preserves_history_and_enforces_fk(monkeypatch):
    migration = importlib.import_module("migrations.versions.0068_canonical_reply_reference")
    assert migration.down_revision == "0067"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql(
            "CREATE TABLE chat_events (id INTEGER PRIMARY KEY, reply_to_message_id TEXT)"
        )
        connection.exec_driver_sql("INSERT INTO chat_events VALUES (1, NULL), (2, '1')")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert connection.exec_driver_sql("SELECT reply_to_event_id FROM chat_events").all() == [
            (None,),
            (None,),
        ]
        connection.exec_driver_sql("UPDATE chat_events SET reply_to_event_id=1 WHERE id=2")
        before_schema = _schema(connection, "chat_events")
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        fk = connection.exec_driver_sql("PRAGMA foreign_key_list(chat_events)").one()
        assert fk[2:7] == ("chat_events", "reply_to_event_id", "id", "RESTRICT", "RESTRICT")
        migration.downgrade()
        migration.upgrade()
        migration.upgrade()
        assert _schema(connection, "chat_events") == before_schema
        assert (
            connection.exec_driver_sql(
                "SELECT reply_to_event_id FROM chat_events WHERE id=2"
            ).scalar()
            == 1
        )
    engine.dispose()


def test_reply_reference_upgrade_restores_missing_index_without_changing_reference(monkeypatch):
    migration = importlib.import_module("migrations.versions.0068_canonical_reply_reference")
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE chat_events (id INTEGER PRIMARY KEY, reply_to_event_id INTEGER "
            "REFERENCES chat_events(id) ON UPDATE RESTRICT ON DELETE RESTRICT)"
        )
        connection.exec_driver_sql("INSERT INTO chat_events VALUES (1, NULL), (2, 1)")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert _schema(connection, "chat_events")["indexes"] == [
            ("ix_chat_events_reply_to_event_id", ("reply_to_event_id",), 0, "")
        ]
        assert connection.exec_driver_sql(
            "SELECT reply_to_event_id FROM chat_events ORDER BY id"
        ).all() == [
            (None,),
            (1,),
        ]
    engine.dispose()
