"""Reply migration only records future authoritative ingress references."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine


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
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        fk = connection.exec_driver_sql("PRAGMA foreign_key_list(chat_events)").one()
        assert fk[2:7] == ("chat_events", "reply_to_event_id", "id", "RESTRICT", "RESTRICT")
        migration.downgrade()
        assert (
            connection.exec_driver_sql(
                "SELECT reply_to_event_id FROM chat_events WHERE id=2"
            ).scalar()
            == 1
        )
    engine.dispose()
