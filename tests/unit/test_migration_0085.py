"""Work directory indexes preserve facts and reject retained index drift."""

import asyncio
import importlib

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from tests.support.social_identity_cases import social_env

from qq_ai_bot.persistence.schema_guard import (
    CanonicalSchemaError,
    canonical_schema_revision,
    require_canonical_schema,
)
from qq_ai_bot.runtime.work_query_schema import query_index_sql
from qq_ai_bot.runtime.work_repository import WorkRepository


async def test_work_query_migration_round_trip_preserves_persisted_work(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease is not None
    original = await repository.accept(
        lease, source_key="migration-fact", source={"actor_user_id": "10001"}, goal="keep original"
    )
    migration = importlib.import_module("migrations.versions.0085_work_query_indexes")

    def round_trip(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.downgrade()
        migration.upgrade()
        return dict(
            connection.exec_driver_sql(
                "SELECT name,sql FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'ix_runtime_work_query_%'"
            ).fetchall()
        )

    async with database.engine.begin() as connection:
        actual = await connection.run_sync(round_trip)

    def normalize(value):
        return "".join(value.lower().replace('"', "").split())

    assert {name: normalize(sql) for name, sql in actual.items()} == {
        name: normalize(sql) for name, sql in query_index_sql().items()
    }
    assert await repository.get(original["id"]) == original
    path = tmp_path / "fresh-migration.sqlite3"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    await require_canonical_schema(url)


@pytest.mark.parametrize(
    "shape", ["missing", "expression", "path_case", "partial", "unique", "order"]
)
async def test_work_query_index_drift_rejected_by_migration_and_startup(
    database, monkeypatch, shape
):
    migration = importlib.import_module("migrations.versions.0085_work_query_indexes")
    name = "ix_runtime_work_query_scope_updated"

    def mutate(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        connection.exec_driver_sql(f"DROP INDEX {name}")
        if shape != "missing":
            sql = migration.INDEX_SQL[name]
            if shape == "expression":
                sql = sql.replace("$.actor_user_id", "$.trigger_event_id")
            elif shape == "path_case":
                sql = sql.replace("$.actor_user_id", "$.ACTOR_USER_ID")
            elif shape == "partial":
                sql += " WHERE state='running'"
            elif shape == "unique":
                sql = sql.replace("CREATE INDEX", "CREATE UNIQUE INDEX")
            else:
                sql = sql.replace("updated DESC", "updated ASC")
            connection.exec_driver_sql(sql)
            with pytest.raises(RuntimeError, match="shape mismatch"):
                migration.upgrade()
        with pytest.raises(RuntimeError, match=r"(shape mismatch|is missing)"):
            migration.downgrade()
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.exec_driver_sql(
            "INSERT INTO alembic_version VALUES (?)", (canonical_schema_revision(),)
        )

    async with database.engine.begin() as connection:
        await connection.run_sync(mutate)
    with pytest.raises(CanonicalSchemaError, match="Work query index"):
        await require_canonical_schema(database.url)
