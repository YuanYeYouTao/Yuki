"""Frozen Speech retirement is atomic and rejects unknown ownership or live facts."""

import asyncio
import importlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command, op
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from tests.support.social_identity_cases import social_env

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.work_repository import WorkRepository

RETIRED = {
    "speech_generations",
    "speech_voice_references",
    "speech_voice_profiles",
    "person_speech_preferences",
}
MIGRATION = importlib.import_module("migrations.versions.0097_retire_speech_output")


def config(path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    return Config(str(Path(__file__).parents[2] / "alembic.ini"))


def facts(db, *, shared=False):
    objects = tuple(
        db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type,name"
        )
    )
    if shared:
        objects = tuple(row for row in objects if row[2] not in RETIRED | {"alembic_version"})
    rows = {}
    for (name,) in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"):
        if name.startswith("sqlite_") or (shared and name in RETIRED | {"alembic_version"}):
            continue
        values = tuple(sorted(db.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr))
        if shared and name == "runtime_config_overrides":
            key_index = [
                row[1] for row in db.execute('PRAGMA table_info("runtime_config_overrides")')
            ].index("config_key")
            values = tuple(row for row in values if row[key_index] not in MIGRATION._CONFIG_KEYS)
        rows[name] = values
    return objects, rows


def insert(db, table, values):
    db.execute(
        f'INSERT INTO "{table}" ({",".join(values)}) VALUES ({",".join("?" for _ in values)})',
        tuple(values.values()),
    )


def seed_config(db):
    for index, key in enumerate(
        (*MIGRATION._CONFIG_KEYS, "speech.custom.keep", "asr.enabled", "identity.bot_name")
    ):
        insert(
            db,
            "runtime_config_overrides",
            {
                "id": index + 1,
                "config_key": key,
                "scope_type": "global",
                "value_json": "true",
                "value_type": "boolean",
                "apply_mode": "hot",
                "version": 1,
                "created_at": "2026-10-07",
                "updated_at": "2026-10-07",
                "updated_by": "fixture",
            },
        )


async def seed_support(path, tmp_path):
    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        env = await social_env(database, tmp_path)
        repo = WorkRepository(database)
        lease = await repo.acquire(env.context.conversation_id, 1)
        work = await repo.accept(lease, source_key="original-execution", source={}, goal="keep")
        await repo.checkpoint(lease, work["id"], None, models=3, tools=2)
        return env.person, env.context.conversation_id
    finally:
        await database.close()


def seed_speech(db, *, status="sent", person=None, conversation=None):
    insert(
        db,
        "speech_voice_profiles",
        {
            "profile_id": "old-profile",
            "display_name": "old",
            "provider": "genie",
            "engine_model_version": "v2",
            "language": "zh",
            "model_relative_path": "voice/model",
            "model_checksum": "original-model-hash",
            "default_style": "neutral",
            "source": "user",
            "manifest_hash": "original-manifest",
            "created_at": "2026-10-07",
            "updated_at": "2026-10-07",
            "is_default": 1,
        },
    )
    insert(
        db,
        "speech_voice_references",
        {
            "id": 1,
            "profile_id": "old-profile",
            "reference_key": "neutral",
            "style": "neutral",
            "audio_relative_path": "voice/reference.wav",
            "audio_checksum": "original-reference-hash",
            "transcript": "original reference",
            "language": "zh",
            "created_at": "2026-10-07",
            "updated_at": "2026-10-07",
        },
    )
    insert(
        db,
        "speech_generations",
        {
            "id": 1,
            "request_id": "original-request",
            "conversation_key_hash": "original-scope",
            "trigger_event_id": 1 if conversation else None,
            "profile_id": "old-profile",
            "reference_id": 1,
            "engine_version": "v2",
            "text_hash": "original-text",
            "normalized_text_hash": "original-normalized",
            "character_count": 1,
            "cache_key": "original-cache",
            "status": status,
            "output_relative_path": "cache/original.wav",
            "created_at": "2026-10-07",
            "canonical_conversation_id": conversation,
        },
    )
    if person:
        insert(
            db,
            "person_speech_preferences",
            {
                "canonical_person_id": person,
                "mode": "prefer_voice",
                "source_message_id": "original-source",
                "created_at": "2026-10-07",
                "updated_at": "2026-10-07",
            },
        )


@pytest.mark.parametrize("populated", [False, True])
def test_frozen_retirement_preserves_shared_facts_and_only_exact_config_keys(
    tmp_path, monkeypatch, populated
):
    path = tmp_path / "old.db"
    cfg = config(path, monkeypatch)
    command.upgrade(cfg, "0096")
    person, conversation = asyncio.run(seed_support(path, tmp_path))
    with closing(sqlite3.connect(path)) as db:
        seed_config(db)
        if populated:
            seed_speech(db, person=person, conversation=conversation)
        db.commit()
        assert (
            sum(
                len(db.execute(f'PRAGMA foreign_key_list("{table}")').fetchall())
                for table in RETIRED
            )
            == 6
        )
        before = facts(db, shared=True)
    command.upgrade(cfg, "0097")
    with closing(sqlite3.connect(path)) as db:
        assert facts(db, shared=True) == before
        assert not RETIRED.intersection(
            row[0] for row in db.execute("SELECT name FROM sqlite_schema")
        )
        assert set(
            row[0] for row in db.execute("SELECT config_key FROM runtime_config_overrides")
        ) == {
            "speech.custom.keep",
            "asr.enabled",
            "identity.bot_name",
        }
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0097",)


def test_fresh_chain_and_downgrade_refusal_do_not_reintroduce_output(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    cfg = config(path, monkeypatch)
    command.upgrade(cfg, "0097")
    with closing(sqlite3.connect(path)) as db:
        before = facts(db)
    with pytest.raises(RuntimeError, match="cannot restore deleted facts"):
        command.downgrade(cfg, "0096")
    with closing(sqlite3.connect(path)) as db:
        assert facts(db) == before


@pytest.mark.parametrize("status", ["queued", "generating"])
def test_pending_generation_rejects_without_reclassifying_or_deleting_config(
    tmp_path, monkeypatch, status
):
    path = tmp_path / "pending.db"
    cfg = config(path, monkeypatch)
    command.upgrade(cfg, "0096")
    with closing(sqlite3.connect(path)) as db:
        seed_speech(db, status=status)
        seed_config(db)
        db.commit()
        before = facts(db)
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        command.upgrade(cfg, "0097")
    with closing(sqlite3.connect(path)) as db:
        assert facts(db) == before


def replace_table(db, name, transform):
    sql = db.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (name,)
    ).fetchone()[0]
    indexes = [
        row[0]
        for row in db.execute(
            "SELECT sql FROM sqlite_schema WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (name,),
        )
    ]
    db.execute(f'DROP TABLE "{name}"')
    db.execute(transform(sql))
    for statement in indexes:
        db.execute(statement)


@pytest.mark.parametrize(
    "drift",
    [
        "column",
        "default",
        "check",
        "foreign_action",
        "external_fk",
        "view",
        "owned_trigger",
        "external_trigger",
        "partial_index",
        "extra_index",
        "unique",
    ],
)
def test_owned_drift_and_external_dependencies_reject_before_any_dml_ddl(
    tmp_path, monkeypatch, drift
):
    path = tmp_path / "drift.db"
    cfg = config(path, monkeypatch)
    command.upgrade(cfg, "0096")
    with closing(sqlite3.connect(path)) as db:
        seed_config(db)
        if drift == "column":
            db.execute("ALTER TABLE speech_generations ADD COLUMN foreign_data TEXT")
        elif drift in {"default", "check", "foreign_action", "unique"}:
            table = (
                "speech_voice_profiles"
                if drift in {"default", "check"}
                else "speech_voice_references"
            )
            old, new = {
                "default": ("DEFAULT '[]'", "DEFAULT '[ ]'"),
                "check": ("provider = 'genie'", "provider != 'other'"),
                "foreign_action": ("ON DELETE CASCADE", "ON DELETE RESTRICT"),
                "unique": ("UNIQUE (profile_id, reference_key)", "UNIQUE (reference_key)"),
            }[drift]
            replace_table(db, table, lambda sql: sql.replace(old, new))
        elif drift == "external_fk":
            db.execute(
                "CREATE TABLE external_reference(id INTEGER, "
                "voice TEXT REFERENCES speech_voice_profiles(profile_id))"
            )
        elif drift == "view":
            db.execute("CREATE VIEW external_voice AS SELECT request_id FROM speech_generations")
        elif drift == "owned_trigger":
            db.execute(
                "CREATE TRIGGER user_hook AFTER UPDATE ON speech_generations BEGIN SELECT 1; END"
            )
        elif drift == "external_trigger":
            db.execute("CREATE TABLE unrelated(id INTEGER)")
            db.execute(
                "CREATE TRIGGER user_hook AFTER INSERT ON unrelated "
                "BEGIN SELECT profile_id FROM speech_voice_profiles; END"
            )
        elif drift == "partial_index":
            db.execute("DROP INDEX uq_speech_profiles_one_default")
            db.execute(
                "CREATE UNIQUE INDEX uq_speech_profiles_one_default "
                "ON speech_voice_profiles(is_default) WHERE is_default = 1"
            )
        else:
            db.execute("CREATE INDEX external_cache ON speech_generations(text_hash)")
        db.commit()
        before = facts(db)
    statements = []

    def capture(_connection, _cursor, sql, *_args):
        statements.append(sql.lstrip().upper())

    event.listen(Engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(RuntimeError, match="Speech retirement"):
            command.upgrade(cfg, "0097")
    finally:
        event.remove(Engine, "before_cursor_execute", capture)
    assert not any(
        sql.startswith(("CREATE", "DROP", "ALTER", "DELETE", "INSERT", "UPDATE"))
        for sql in statements
    )
    with closing(sqlite3.connect(path)) as db:
        assert facts(db) == before


def test_failure_after_config_delete_and_first_drop_rolls_everything_back(tmp_path, monkeypatch):
    path = tmp_path / "rollback.db"
    cfg = config(path, monkeypatch)
    command.upgrade(cfg, "0096")
    with closing(sqlite3.connect(path)) as db:
        seed_config(db)
        seed_speech(db)
        db.commit()
        before = facts(db)
    original = op.drop_table

    def fail_second(name, **kwargs):
        if name == "speech_voice_references":
            raise RuntimeError("injected failure")
        return original(name, **kwargs)

    monkeypatch.setattr(op, "drop_table", fail_second)
    with pytest.raises(RuntimeError, match="injected failure"):
        command.upgrade(cfg, "0097")
    with closing(sqlite3.connect(path)) as db:
        assert facts(db) == before


def test_unrelated_view_and_trigger_remain_valid(tmp_path, monkeypatch):
    path = tmp_path / "unrelated.db"
    cfg = config(path, monkeypatch)
    command.upgrade(cfg, "0096")
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE user_data(id INTEGER)")
        db.execute("CREATE VIEW user_view AS SELECT id FROM user_data")
        db.execute("CREATE TRIGGER user_hook AFTER INSERT ON user_data BEGIN SELECT 1; END")
        db.commit()
        before = facts(db, shared=True)
    command.upgrade(cfg, "0097")
    with closing(sqlite3.connect(path)) as db:
        assert facts(db, shared=True) == before


def test_sqlite_writer_contention_leaves_original_upgrade_retryable(tmp_path, monkeypatch):
    path = tmp_path / "locked.db"
    cfg = config(path, monkeypatch)
    command.upgrade(cfg, "0096")
    with closing(sqlite3.connect(path)) as db:
        seed_config(db)
        seed_speech(db)
        db.commit()
        before = facts(db)
        shared = facts(db, shared=True)
        db.execute("BEGIN IMMEDIATE")
        # A real second SQLite writer must fail before any retirement mutation.
        with pytest.raises(OperationalError, match="database is locked"):
            command.upgrade(cfg, "0097")
        assert facts(db) == before
        db.rollback()
    command.upgrade(cfg, "0097")
    with closing(sqlite3.connect(path)) as db:
        assert facts(db, shared=True) == shared
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0097",)
