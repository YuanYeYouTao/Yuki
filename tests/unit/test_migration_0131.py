"""Search retirement removes active settings, preserving original execution data."""

import sqlite3
from uuid import uuid4

from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118


def test_full_empty_upgrade_reaches_0131(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    command.upgrade(Config("alembic.ini"), "0131")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0131",)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_0130_upgrade_retires_all_scopes_without_rewriting_history(tmp_path, monkeypatch):
    path = tmp_path / "retirement.db"
    _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0130")
    now = "2026-10-17 00:00:00"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        person = db.execute("SELECT id FROM persons LIMIT 1").fetchone()[0]
        space = str(uuid4())
        db.execute(
            "INSERT INTO spaces(id,name,enabled,autonomous_enabled,require_mention,revision,"
            "created_at,updated_at) VALUES(?,'original',1,1,1,1,?,?)",
            (space, now, now),
        )
        for key in (
            "web.extract_max_results",
            "web.source_max_runs_per_conversation",
            "web.search_max_results",
        ):
            for scope, owner, shared in (
                ("global", None, None),
                ("user", person, None),
                ("group", None, space),
            ):
                db.execute(
                    "INSERT INTO runtime_config_overrides(config_key,scope_type,value_json,"
                    "value_type,apply_mode,version,created_at,updated_at,updated_by,"
                    "canonical_person_id,canonical_space_id) "
                    "VALUES(?,?,'3','integer','hot',7,?,?,'original-operator',?,?)",
                    (key, scope, now, now, owner, shared),
                )
        db.execute(
            "INSERT INTO admin_operation_events(actor_user_id,trigger_message_id,conversation_key,"
            "capability,operation,target_type,target_id,before_json,after_json,success,"
            "duration_seconds,created_at) VALUES('original-operator','','','config.set','set',"
            "'config','web.extract_max_results','1','3',1,0,?)",
            (now,),
        )
        conversation = db.execute("SELECT id FROM canonical_conversations LIMIT 1").fetchone()[0]
        db.execute(
            "INSERT INTO web_search_runs(id,conversation_key,trigger_message_id,execution_id,"
            "query,provider,created_at,partial_failure,canonical_conversation_id) "
            "VALUES(69,'original-conversation','original-platform-message','original-execution',"
            "'original query','original-provider',?,0,?)",
            (now, conversation),
        )
        db.execute(
            "INSERT INTO web_search_sources(id,run_id,ordinal,title,url,domain,snippet,created_at) "
            "VALUES(70,69,1,'Original source','https://example.com/original','example.com',"
            "'original retained source',?)",
            (now,),
        )
        before = {
            table: db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in (
                "chat_events",
                "runtime_work",
                "admin_operation_events",
                "web_search_runs",
                "web_search_sources",
            )
        }
        live = db.execute(
            "SELECT * FROM runtime_config_overrides WHERE config_key='web.search_max_results' "
            "ORDER BY id"
        ).fetchall()
    command.upgrade(config, "0131")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0131",)
        assert db.execute("SELECT * FROM runtime_config_overrides ORDER BY id").fetchall() == live
        for table, rows in before.items():
            assert db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == rows
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
