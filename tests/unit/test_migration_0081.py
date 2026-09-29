"""Claude cache writes stay unknown in historical invocation rows."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine


def test_claude_cache_write_migration_preserves_unknown_and_explicit_zero(monkeypatch):
    migration = importlib.import_module("migrations.versions.0081_claude_cache_write_usage")
    assert migration.down_revision == "0080"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE model_invocations (id TEXT PRIMARY KEY, provider TEXT NOT NULL)"
        )
        connection.exec_driver_sql("INSERT INTO model_invocations VALUES ('old', 'anthropic')")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        fields = (
            "cache_creation_input_tokens, cache_creation_5m_input_tokens, "
            "cache_creation_1h_input_tokens"
        )
        assert connection.exec_driver_sql(
            f"SELECT {fields} FROM model_invocations WHERE id='old'"
        ).one() == (None, None, None)
        connection.exec_driver_sql(
            "INSERT INTO model_invocations VALUES ('new', 'anthropic', 0, 0, 0)"
        )
        migration.downgrade()
        migration.upgrade()
        assert connection.exec_driver_sql(
            f"SELECT id, {fields} FROM model_invocations ORDER BY id"
        ).all() == [("new", 0, 0, 0), ("old", None, None, None)]
    engine.dispose()
