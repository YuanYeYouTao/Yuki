"""The compatibility migration rejects drift before changing any index."""

import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
@pytest.mark.parametrize(
    "drift",
    [
        "json_path_case",
        "unique",
        "predicate",
        "column",
        "column_unicode_case",
        "column_unicode_space",
        "main_columns",
    ],
)
async def test_compatibility_index_drift_keeps_original_schema(
    database, monkeypatch, action, drift
):
    migration = importlib.import_module("migrations.versions.0096_invocation_effect_indexes")

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        if drift == "main_columns":
            name, table, _columns = migration.LEGACY_MAIN_INDEXES[0]
            connection.exec_driver_sql(f"DROP INDEX {name}")
            connection.exec_driver_sql(f"CREATE INDEX {name} ON {table}(created_at)")
        else:
            name = "ix_runtime_effects_parent"
            connection.exec_driver_sql(f"DROP INDEX {name}")
            sql = migration.INDEXES[name]
            if drift == "json_path_case":
                sql = sql.replace("$.invocation.version", "$.Invocation.version")
            elif drift == "unique":
                sql = sql.replace("CREATE INDEX", "CREATE UNIQUE INDEX")
            elif drift.startswith("column"):
                # SQLite distinguishes these identifiers. Python Unicode
                # lower()/whitespace normalization must not merge them.
                column = {
                    "column": "work_idifnotexists",
                    "column_unicode_case": "wor\u212a_id",
                    "column_unicode_space": "wor\u00a0k_id",
                }[drift]
                connection.exec_driver_sql(
                    f"ALTER TABLE runtime_work_effects ADD COLUMN {column} TEXT"
                )
                sql = sql.replace("(work_id,", f"({column},")
            else:
                sql = sql.replace(" = 1", " = 2")
            connection.exec_driver_sql(sql)
        before = connection.exec_driver_sql(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).all()
        if drift == "main_columns" and action == "downgrade":
            # 0096 owns invocation indexes, not main's 0092 indexes. The latter
            # are retained and their shape is checked by main's own downgrade.
            migration.downgrade()
            assert (
                connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE name=?", (name,)
                ).scalar()
                == f"CREATE INDEX {name} ON {table}(created_at)"
            )
        else:
            with pytest.raises(RuntimeError, match="index shape mismatch"):
                getattr(migration, action)()
            assert (
                connection.exec_driver_sql(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
                ).all()
                == before
            )

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)
