"""Source revision closure adds/removes only triggers; no receipt or source rows change."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from tests.unit.test_semantic_participation_host import _event_and_route

from qq_ai_bot.conversation.projection_revision_schema import PROJECTION_ADDITIONS_0082
from qq_ai_bot.persistence.event_repository import EventLedgerRepository


async def test_revision_closure_upgrade_downgrade_preserves_source_and_receipts(
    database, monkeypatch
):
    source = await _event_and_route(database, EventLedgerRepository(database))
    migration = importlib.import_module("migrations.versions.0082_prompt_source_revision_closure")
    assert migration.down_revision == "0081"

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        before = connection.exec_driver_sql(
            "SELECT * FROM chat_events WHERE id=?", (source.id,)
        ).one()
        for _ in range(2):
            migration.downgrade()
            names = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).scalars()
            )
            assert not names.intersection(PROJECTION_ADDITIONS_0082)
            migration.upgrade()
            names = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).scalars()
            )
            assert names.issuperset(PROJECTION_ADDITIONS_0082)
            assert (
                connection.exec_driver_sql(
                    "SELECT * FROM chat_events WHERE id=?", (source.id,)
                ).one()
                == before
            )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)
