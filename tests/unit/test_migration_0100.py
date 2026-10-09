"""0100 links Jobs only by one trustworthy original-Work match, else fails closed."""

import asyncio
import importlib
import json
import sqlite3
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, update
from tests.support.background_authority import approve_background_plugin
from tests.support.social_identity_cases import social_env

from qq_ai_bot.persistence.schema_guard import canonical_schema_revision
from qq_ai_bot.plugin_host.db_models import PluginBackgroundTurnJobModel
from qq_ai_bot.runtime.work_schema_v1 import work
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

MIGRATION = "migrations.versions.0100_plugin_turn_work_link"
PLUGIN = "test.backfill"


def _source(conversation_id: str, event_id: int, *, owner="plugin_background", generation=1):
    return json.dumps(
        {
            "owner": owner,
            "plugin_id": PLUGIN,
            "trigger_event_id": event_id,
            "conversation_id": conversation_id,
            "generation": generation,
        }
    )


async def _seed_jobs(database, tmp_path, count):
    env = await social_env(database, tmp_path)
    repository = await approve_background_plugin(
        database, plugin_id=PLUGIN, bot_user_id="80001", group_id="20001", creator_user_id="10001"
    )
    for number in range(count):
        await repository.publish(
            plugin_id=PLUGIN,
            request=PublishNotificationRequest(
                event_key=f"event-{number}",
                event_type="fixture",
                external_source="offline",
                target=NotificationTarget(target_type="group", target_id="20001"),
                occurred_at=datetime.now(UTC),
                summary=f"update {number}",
                ask_agent=True,
                agent_intent="ack",
            ),
        )
    async with database.sessions() as session:
        jobs = list(
            await session.scalars(
                select(PluginBackgroundTurnJobModel).order_by(PluginBackgroundTurnJobModel.id)
            )
        )
    return env, [(job.id, job.source_event_id) for job in jobs]


async def _add_work(database, identity, conversation_id, source_json, *, state="suspended"):
    async with database.immediate_session() as session:
        await session.execute(
            work.insert().values(
                id=identity,
                conversation_id=conversation_id,
                generation=1,
                source_key=f"invocation:{identity}",
                source_json=source_json,
                goal="original goal",
                state=state,
                created=1,
                updated=1,
            )
        )


async def _restore_0099_shape(connection):
    """Rebuild the Job table as 0099 had it; ORM create_all already adds the link."""
    rows = await connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name="
        "'plugin_background_turn_jobs' AND sql IS NOT NULL AND name != "
        "'ux_plugin_background_turn_work'"
    )
    indexes = [row[0] for row in rows]
    columns = [
        row[1]
        for row in await connection.exec_driver_sql(
            "PRAGMA table_info(plugin_background_turn_jobs)"
        )
        if row[1] != "work_id"
    ]
    table_sql = (
        await connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name='plugin_background_turn_jobs'"
        )
    ).scalar()
    lines = [line for line in table_sql.split("\n") if "work_id" not in line]
    rebuilt = "\n".join(lines).replace("plugin_background_turn_jobs", "jobs_0099", 1)
    rebuilt = rebuilt.rstrip().rstrip(")").rstrip().rstrip(",") + "\n)"
    await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    await connection.exec_driver_sql(rebuilt)
    names = ",".join(columns)
    await connection.exec_driver_sql(
        f"INSERT INTO jobs_0099({names}) SELECT {names} FROM plugin_background_turn_jobs"
    )
    await connection.exec_driver_sql("DROP TABLE plugin_background_turn_jobs")
    await connection.exec_driver_sql("ALTER TABLE jobs_0099 RENAME TO plugin_background_turn_jobs")
    for sql in indexes:
        await connection.exec_driver_sql(sql)


def _run(connection, migration, monkeypatch, action):
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    getattr(migration, action)()


async def test_backfill_links_unique_and_fails_closed_otherwise(database, tmp_path, monkeypatch):
    migration = importlib.import_module(MIGRATION)
    assert (migration.revision, migration.down_revision) == ("0100", "0099")
    env, jobs = await _seed_jobs(database, tmp_path, 5)
    conversation = env.context.conversation_id
    unique, ambiguous, missing_claimed, missing_new, foreign = jobs
    await _add_work(
        database,
        "a" * 8 + "-0000-4000-8000-000000000001",
        conversation,
        _source(conversation, unique[1]),
    )
    for number in (2, 3):
        await _add_work(
            database,
            f"bbbbbbbb-0000-4000-8000-00000000000{number}",
            conversation,
            _source(conversation, ambiguous[1]),
        )
    # A Work of a different owner kind never counts as this Job's admission.
    await _add_work(
        database,
        "cccccccc-0000-4000-8000-000000000004",
        conversation,
        _source(conversation, foreign[1], owner="plugin_invocation"),
    )
    # A claimed Job with no Work cannot prove admission never happened.
    async with database.immediate_session() as session:
        await session.execute(
            update(PluginBackgroundTurnJobModel)
            .where(PluginBackgroundTurnJobModel.id.in_((missing_claimed[0], foreign[0])))
            .values(attempts=1)
        )
    async with database.engine.begin() as connection:
        await _restore_0099_shape(connection)
        await connection.run_sync(_run, migration, monkeypatch, "upgrade")
        # Re-running the owned shape is idempotent and keeps prior decisions.
        await connection.run_sync(_run, migration, monkeypatch, "upgrade")
    async with database.sessions() as session:
        rows = {row.id: row for row in await session.scalars(select(PluginBackgroundTurnJobModel))}
        works = {row.id: row.state for row in (await session.execute(select(work))).all()}
    assert rows[unique[0]].work_id == "a" * 8 + "-0000-4000-8000-000000000001"
    assert rows[unique[0]].status == "pending"
    assert (rows[ambiguous[0]].status, rows[ambiguous[0]].work_id) == ("failed", None)
    assert rows[ambiguous[0]].last_error_category == "runtime_work_link_ambiguous"
    assert (rows[missing_claimed[0]].status, rows[missing_claimed[0]].last_error_category) == (
        "failed",
        "runtime_work_link_unproven",
    )
    assert rows[foreign[0]].status == "failed" and rows[foreign[0]].work_id is None
    # A never-claimed Job proves no admission: it is still an ordinary new Job.
    assert (rows[missing_new[0]].status, rows[missing_new[0]].work_id) == ("pending", None)
    # Old Work is neither deleted nor terminated.
    assert len(works) == 4 and set(works.values()) == {"suspended"}


async def test_downgrade_refuses_live_parked_owner(database, tmp_path, monkeypatch):
    migration = importlib.import_module(MIGRATION)
    env, jobs = await _seed_jobs(database, tmp_path, 1)
    identity = "dddddddd-0000-4000-8000-000000000005"
    await _add_work(
        database,
        identity,
        env.context.conversation_id,
        _source(env.context.conversation_id, jobs[0][1]),
    )
    async with database.immediate_session() as session:
        await session.execute(update(PluginBackgroundTurnJobModel).values(work_id=identity))
    async with database.engine.begin() as connection:
        with pytest.raises(RuntimeError, match="live owners"):
            await connection.run_sync(_run, migration, monkeypatch, "downgrade")
    async with database.sessions() as session:
        assert (await session.scalar(select(PluginBackgroundTurnJobModel))).work_id == identity


async def test_drifted_link_shape_is_rejected_before_any_change(database, monkeypatch):
    migration = importlib.import_module(MIGRATION)
    async with database.engine.begin() as connection:
        await connection.exec_driver_sql("DROP INDEX ux_plugin_background_turn_work")
        await connection.exec_driver_sql(
            "CREATE INDEX ux_plugin_background_turn_work ON plugin_background_turn_jobs(work_id)"
        )
        with pytest.raises(RuntimeError, match="index shape mismatch"):
            await connection.run_sync(_run, migration, monkeypatch, "upgrade")


async def test_fresh_alembic_head_matches_current_link_contract(tmp_path, monkeypatch):
    path = tmp_path / "head.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0099")
    await asyncio.to_thread(command.upgrade, config, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            canonical_schema_revision(),
        )
        references = [
            row[2:7]
            for row in db.execute("PRAGMA foreign_key_list(plugin_background_turn_jobs)")
            if row[3] == "work_id"
        ]
        assert references == [("runtime_work", "work_id", "id", "NO ACTION", "RESTRICT")]
        unique = [
            row
            for row in db.execute("PRAGMA index_list(plugin_background_turn_jobs)")
            if row[1] == "ux_plugin_background_turn_work"
        ]
        assert unique and unique[0][2] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    await asyncio.to_thread(command.downgrade, config, "0099")
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(plugin_background_turn_jobs)")}
        assert "work_id" not in columns
