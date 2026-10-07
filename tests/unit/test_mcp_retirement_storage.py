"""MCP retirement preserves shared result storage, evidence and frozen SQLite data."""

import asyncio
import importlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from tests.support.social_identity_cases import social_env

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.diagnostic_writer import DiagnosticWriter
from qq_ai_bot.persistence.models import (
    Base,
    ChatEventModel,
    MemoryToolReceiptModel,
    ToolInvocationModel,
)
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.tool_results.access import ArtifactAccess
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository
from qq_ai_bot.tool_results.recorder import ToolInvocationRepository

RETIRED = {"mcp_server_states", "mcp_tool_cache"}


def _config(path):
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path.as_posix()}")
    return config


def _snapshot(connection):
    # Only isolated synthetic SQLite fixtures; never used against production.
    objects = tuple(
        connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND tbl_name NOT IN "
            "('mcp_server_states','mcp_tool_cache','alembic_version') ORDER BY type,name"
        )
    )
    rows = {
        name: tuple(sorted(connection.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr))
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT IN "
            "('mcp_server_states','mcp_tool_cache','alembic_version') ORDER BY name"
        )
    }
    return objects, rows


async def _seed_original_work(path, tmp_path):
    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        env = await social_env(database, tmp_path)
        repository = WorkRepository(database)
        lease = await repository.acquire(env.context.conversation_id, 1)
        assert lease is not None
        original = await repository.accept(
            lease, source_key="original-work", source={}, goal="retain original goal"
        )
        await repository.checkpoint(lease, original["id"], None, models=3, tools=2)
        await repository.enqueue(
            env.context.conversation_id,
            1,
            "original-input",
            kind="message",
            work_id=original["id"],
            ready=False,
        )
    finally:
        await database.close()


@pytest.mark.parametrize("cache_count", [0, 3])
def test_frozen_old_head_upgrade_removes_only_owned_derived_tables(
    tmp_path, monkeypatch, cache_count
):
    path = tmp_path / "old.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = _config(path)
    command.upgrade(config, "0095")
    asyncio.run(_seed_original_work(path, tmp_path))
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as db:
        old_shapes = tuple(
            db.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN "
                "('mcp_server_states','mcp_tool_cache') ORDER BY type,name"
            )
        )
        for number in range(cache_count):
            db.execute(
                "INSERT INTO mcp_server_states VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"old-{number}",
                    "stdio",
                    "hash",
                    1,
                    "keepalive",
                    "ready",
                    "",
                    "",
                    "",
                    "",
                    None,
                    None,
                    None,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO mcp_tool_cache VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    number + 1,
                    f"old-{number}",
                    "tool",
                    f"old-tool-{number}",
                    "old description",
                    "short",
                    "{}",
                    "{}",
                    "{}",
                    "hash",
                    now,
                ),
            )
        db.execute(
            "INSERT INTO tool_artifacts(handle_id,provider_id,tool_name,relative_path,"
            "media_type,byte_size,created_at,expires_at) "
            "VALUES('original-handle','mcp-old','old-tool','original-handle.json','application/json',2,?,?)",
            (now, now),
        )
        db.execute(
            "INSERT INTO tool_invocations(conversation_key_hash,provider_id,tool_name,success,"
            "latency_seconds,result_size,artifact_created,created_at) "
            "VALUES('original-hash','mcp-old','old-tool',1,0.1,2,1,?)",
            (now,),
        )
        db.commit()
        before = _snapshot(db)
        assert not any(
            row[2] in RETIRED
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            for row in db.execute(f'PRAGMA foreign_key_list("{name}")')
        )
    command.upgrade(config, "0096")
    with sqlite3.connect(path) as db:
        assert not RETIRED.intersection(
            name for (name,) in db.execute("SELECT name FROM sqlite_master")
        )
        assert _snapshot(db) == before
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0096",)
    command.downgrade(config, "0095")
    with sqlite3.connect(path) as db:
        assert _snapshot(db) == before
        assert (
            tuple(
                db.execute(
                    "SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN "
                    "('mcp_server_states','mcp_tool_cache') ORDER BY type,name"
                )
            )
            == old_shapes
        )
        assert all(
            db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone() == (0,) for name in RETIRED
        )
    # This round trip covers MCP's frozen 0095 -> 0096 boundary. Later Speech
    # retirement owns other tables and deliberately has no factual downgrade.
    command.upgrade(config, "0096")
    with sqlite3.connect(path) as db:
        assert _snapshot(db) == before
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0096",)


def test_current_metadata_excludes_retired_tables_but_keeps_shared_tables():
    assert not RETIRED.intersection(Base.metadata.tables)
    assert {
        "tool_artifacts",
        "tool_artifact_refs",
        "tool_invocations",
        "memory_tool_receipts",
    }.issubset(Base.metadata.tables)


async def test_neutral_store_and_recorder_preserve_original_owner_and_private_evidence(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    access = ArtifactAccess(env.context.conversation_id, 1, env.person)
    store = ToolArtifactRepository(database, tmp_path / "tool_artifacts", retention_seconds=60)
    assert database.work_result_store is store
    handle = await store.write_artifact(
        provider_id="core",
        tool_name="read",
        content='{"answer":"original"}',
        media_type="application/json",
        access=access,
    )
    assert await store.read(handle, access=access) is not None
    denied = await store.read(
        handle, access=ArtifactAccess(env.context.conversation_id, 1, "foreign-person")
    )
    assert denied is not None and denied["error_code"] == "artifact_not_authorized"
    writer = DiagnosticWriter()
    await writer.start()
    recorder = ToolInvocationRepository(
        database, writer=writer, reflection_excerpt_characters=128, reflection_retention_days=3
    )
    try:
        async with database.sessions() as session:
            event = await session.scalar(select(ChatEventModel))
        assert event is not None
        await recorder.record_invocation(
            conversation_key="shared-tool-evidence",
            provider_id="core",
            tool_name="read",
            success=True,
            latency_seconds=0.1,
            result_size=32,
            artifact_created=True,
            error_category=None,
            trigger_event_id=event.id,
            canonical_conversation_id=env.context.conversation_id,
            ingress_presence_id=env.presence,
            bot_user_id=env.bot.self_id,
            tool_call_id="original-call",
            execution_id=f"event:{event.id}",
            result_excerpt=json.dumps({"answer": "original", "api_key": "must-not-persist"}),
        )
    finally:
        await writer.close()
    async with database.sessions() as session:
        invocation = (await session.scalars(select(ToolInvocationModel))).one()
        receipt = (await session.scalars(select(MemoryToolReceiptModel))).one()
        assert invocation.provider_id == receipt.provider_id == "core"
        assert receipt.trigger_event_id == event.id
        assert receipt.tool_call_id == "original-call"
        assert receipt.canonical_space_id == env.space
        assert "must-not-persist" not in receipt.result_excerpt
        assert "[redacted]" in receipt.result_excerpt
        assert len(receipt.result_excerpt) <= 128
    assert writer.committed == 1 and writer.failures == 0


def test_partial_retirement_ddl_failure_rolls_back_first_drop(tmp_path, monkeypatch):
    path = tmp_path / "invalid-old.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = _config(path)
    command.upgrade(config, "0095")
    with sqlite3.connect(path) as db:
        before = _all_facts(db)
    from alembic import op

    original = op.drop_table

    def fail_second_drop(name, **kwargs):
        if name == "mcp_server_states":
            raise OperationalError("DROP TABLE", None, sqlite3.OperationalError("fixture failure"))
        return original(name, **kwargs)

    monkeypatch.setattr(op, "drop_table", fail_second_drop)
    with pytest.raises(OperationalError, match="fixture failure"):
        command.upgrade(config, "0096")
    with sqlite3.connect(path) as db:
        assert _all_facts(db) == before
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0095",)


def _all_facts(db):
    objects = tuple(
        db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")
    )
    facts = {
        name: tuple(sorted(db.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr))
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return objects, facts


def _replace_owned_table(db, table, replacement):
    columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
    records = db.execute(f'SELECT * FROM "{table}"').fetchall()
    db.execute(f'DROP TABLE "{table}"')
    db.execute(replacement)
    names = ",".join(f'"{name}"' for name in columns)
    placeholders = ",".join("?" for _ in columns)
    db.executemany(f'INSERT INTO "{table}"({names}) VALUES({placeholders})', records)
    if table == "mcp_tool_cache":
        db.execute("CREATE INDEX ix_mcp_tool_cache_server ON mcp_tool_cache(server_id)")


@pytest.mark.parametrize(
    "drift",
    [
        "missing",
        "view",
        "extra_column",
        "type",
        "nullable",
        "primary_key",
        "default",
        "unique",
        "index_missing",
        "index_column",
        "index_expression",
        "index_collation",
        "index_partial",
        "index_extra",
        "external_fk",
        "owned_fk",
        "view_dependency",
        "owned_trigger",
        "external_trigger",
    ],
)
def test_owned_schema_drift_rejected_before_first_ddl_and_preserves_facts(
    tmp_path, monkeypatch, drift
):
    path = tmp_path / "drift.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = _config(path)
    command.upgrade(config, "0095")
    frozen = importlib.import_module("migrations.versions.0096_retire_mcp_metadata")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO mcp_server_states VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "original",
                "stdio",
                "hash",
                1,
                "keepalive",
                "ready",
                "",
                "",
                "",
                "",
                None,
                None,
                None,
                "2026-10-07",
            ),
        )
        db.execute(
            "INSERT INTO mcp_tool_cache VALUES(1,'original','tool','original-tool','','',"
            "'{}','{}','{}','hash','2026-10-07')"
        )
        state = frozen._RESTORE_STATEMENTS[0]
        cache = frozen._RESTORE_STATEMENTS[1]
        if drift in {"missing", "view"}:
            db.execute("DROP TABLE mcp_server_states")
            if drift == "view":
                db.execute("CREATE VIEW mcp_server_states AS SELECT 'business' AS body")
        elif drift == "extra_column":
            db.execute("ALTER TABLE mcp_server_states ADD COLUMN business_body TEXT")
        elif drift in {"type", "nullable", "primary_key", "default", "owned_fk"}:
            state = {
                "type": state.replace("transport VARCHAR(32)", "transport TEXT"),
                "nullable": state.replace(
                    "transport VARCHAR(32) NOT NULL", "transport VARCHAR(32)"
                ),
                "primary_key": state.replace(
                    "PRIMARY KEY (server_id)", "PRIMARY KEY (server_id, transport)"
                ),
                "default": state.replace(
                    "server_name VARCHAR(255) NOT NULL",
                    "server_name VARCHAR(255) NOT NULL DEFAULT 'legacy'",
                ),
                "owned_fk": state.replace(
                    "PRIMARY KEY (server_id)",
                    "PRIMARY KEY (server_id), FOREIGN KEY (server_id) "
                    "REFERENCES mcp_tool_cache(model_name)",
                ),
            }[drift]
            _replace_owned_table(db, "mcp_server_states", state)
        elif drift == "unique":
            cache = cache.replace(
                ",\n\tCONSTRAINT uq_mcp_tool_cache_server_tool "
                "UNIQUE (server_id, remote_tool_name)",
                "",
            )
            _replace_owned_table(db, "mcp_tool_cache", cache)
        elif drift.startswith("index_"):
            if drift == "index_extra":
                db.execute("CREATE INDEX business_cache_index ON mcp_tool_cache(description)")
            else:
                db.execute("DROP INDEX ix_mcp_tool_cache_server")
                shape = {
                    "index_column": "model_name",
                    "index_expression": "server_id||''",
                    "index_collation": "server_id COLLATE NOCASE",
                    "index_partial": "server_id",
                }.get(drift)
                if shape:
                    predicate = " WHERE id > 0" if drift == "index_partial" else ""
                    db.execute(
                        f"CREATE INDEX ix_mcp_tool_cache_server "
                        f"ON mcp_tool_cache({shape}){predicate}"
                    )
        elif drift == "external_fk":
            db.execute(
                "CREATE TABLE business_refs(id TEXT PRIMARY KEY, "
                "server TEXT REFERENCES mcp_server_states(server_id))"
            )
            db.execute("INSERT INTO business_refs VALUES('keep','original')")
        elif drift == "view_dependency":
            db.execute("CREATE VIEW business_cache AS SELECT id FROM mcp_tool_cache")
        elif drift == "owned_trigger":
            db.execute(
                "CREATE TRIGGER business_hook AFTER UPDATE ON mcp_tool_cache BEGIN SELECT 1; END"
            )
        elif drift == "external_trigger":
            db.execute("CREATE TABLE business_notifications(id TEXT)")
            db.execute(
                "CREATE TRIGGER business_hook AFTER INSERT ON business_notifications "
                "BEGIN SELECT id FROM mcp_tool_cache; END"
            )
        db.commit()
        before = _all_facts(db)
    statements = []

    def capture(_connection, _cursor, sql, *_args):
        statements.append(sql.lstrip().upper())

    event.listen(Engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(RuntimeError, match="MCP retirement"):
            command.upgrade(config, "0096")
    finally:
        event.remove(Engine, "before_cursor_execute", capture)
    assert not any(sql.startswith(("CREATE", "DROP", "ALTER")) for sql in statements)
    with sqlite3.connect(path) as db:
        assert _all_facts(db) == before
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0095",)


def test_equivalent_declared_types_pass_owned_preflight(tmp_path, monkeypatch):
    path = tmp_path / "synonyms.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = _config(path)
    command.upgrade(config, "0095")
    frozen = importlib.import_module("migrations.versions.0096_retire_mcp_metadata")
    with sqlite3.connect(path) as db:
        state = (
            frozen._RESTORE_STATEMENTS[0]
            .replace("BOOLEAN", "BOOL")
            .replace("VARCHAR(", "CHARACTER VARYING(")
        )
        _replace_owned_table(db, "mcp_server_states", state)
        db.commit()
    command.upgrade(config, "0096")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0096",)
