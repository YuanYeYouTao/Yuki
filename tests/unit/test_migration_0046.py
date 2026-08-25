"""Person/space ownership shadows and 0046 cutover-descendant proofs."""

from __future__ import annotations

import ast
import sqlite3
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any
from uuid import uuid1, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event
from tests.unit.test_migration_0043 import (
    _downgrade as _alembic_downgrade,
)
from tests.unit.test_migration_0043 import (
    _normalized_schema,
    _schema_dump,
    _tables,
    _upgrade,
)
from tests.unit.test_migration_0044 import _connect, _seed_identity
from tests.unit.test_migration_0045 import (
    CHAT_EVENT_CANONICAL_SHADOW_COLUMNS,
    _column_names,
    _index_names,
)

from qq_ai_bot.conversation.canonical_event_schema import C4_TRIGGER_NAMES
from qq_ai_bot.identity.canonical_ownership_schema import (
    C5_EXCLUDED_OWNERSHIP,
    C5_OWNERSHIP_COLUMNS,
    C5_OWNERSHIP_FOREIGN_KEYS,
    C5_OWNERSHIP_INDEXES,
    C5_OWNERSHIP_INVENTORY,
    C5_OWNERSHIP_TABLES,
    C5_TRIGGER_NAMES,
    C5_TRIGGER_SQL,
)
from qq_ai_bot.identity.db_models import _install_c5_triggers_after_metadata_create
from qq_ai_bot.persistence.metadata import Base

_MIGRATION_PATH = Path("migrations/versions/0046_canonical_ownership_shadows.py")
_KNOWN_WRITERS = (
    Path("src/qq_ai_bot/persistence/repository_helpers.py"),
    Path("src/qq_ai_bot/persistence/people_repository.py"),
    Path("src/qq_ai_bot/persistence/relationship_repository.py"),
    Path("src/qq_ai_bot/memory/repository.py"),
    Path("src/qq_ai_bot/memory/quality/performance.py"),
    Path("src/qq_ai_bot/time/service.py"),
    Path("src/qq_ai_bot/speech/preference_repository.py"),
    Path("src/qq_ai_bot/automation/repository.py"),
)
_SHADOW_NAMES = {column for columns in C5_OWNERSHIP_COLUMNS.values() for column in columns}
_EXPECTED_FKS = tuple(
    (table, parent, column, parent_column, "RESTRICT", "RESTRICT", "NONE")
    for table, column, parent, parent_column in C5_OWNERSHIP_FOREIGN_KEYS
)
_CREATING_REVISIONS = ("0005", "0007", "0012", "0017", "0020", "0027", "0045")
_EXPLICIT_CREATE_SOURCES = (
    Path("migrations/versions/0007_add_relationship_system.py"),
    Path("migrations/versions/0012_add_automation_runtime.py"),
    Path("migrations/versions/0017_planner_voice_governance.py"),
    Path("migrations/versions/0020_memory_v2_cutover.py"),
    Path("migrations/versions/0027_yuki_self_memory.py"),
)


def _enable_sqlite_fk(dbapi_connection: object, _record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _sqlite_trigger_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name NOT LIKE 'sqlite_%'"
            )
        }


def _create_orm_c5_schema(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    event.listen(engine, "connect", _enable_sqlite_fk)
    Base.metadata.create_all(engine)
    engine.dispose()


def _normalized_c5_schema(path: Path) -> dict[str, Any]:
    full = _normalized_schema(path)
    tables = [name for name in full["tables"] if name in C5_OWNERSHIP_TABLES]
    return {
        "tables": tables,
        "columns": {
            name: [
                column
                for column in full["columns"][name]
                if column[0] in C5_OWNERSHIP_COLUMNS[name]
            ]
            for name in tables
        },
        "indexes": {
            name: [item for item in full["indexes"][name] if item[0] in C5_OWNERSHIP_INDEXES]
            for name in tables
        },
        "foreign_keys": {
            name: [
                item for item in full["foreign_keys"][name] if item[1] in C5_OWNERSHIP_COLUMNS[name]
            ]
            for name in tables
        },
        "triggers": {
            name: sql for name, sql in full["triggers"].items() if name in C5_TRIGGER_NAMES
        },
    }


def _c5_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return set(_column_names(connection, table)) & set(C5_OWNERSHIP_COLUMNS[table])


def _c5_present(connection: sqlite3.Connection) -> bool:
    tables = _tables(connection)
    return any(table in tables and _c5_columns(connection, table) for table in C5_OWNERSHIP_TABLES)


def _seed_legacy_people_and_groups(connection: sqlite3.Connection, now: str) -> None:
    for user_id, is_bot in (("bot-1", 1), ("peer-1", 0), ("peer-2", 0), ("1001", 0)):
        connection.execute(
            "INSERT INTO people(user_id, nickname, enabled, is_bot, first_seen_at, last_seen_at) "
            "VALUES (?, '', 1, ?, ?, ?)",
            (user_id, is_bot, now, now),
        )
    for group_id in ("2001", "2002"):
        connection.execute(
            "INSERT INTO groups(group_id, name, enabled, require_mention, autonomous_enabled, "
            "first_seen_at, last_seen_at, updated_at) VALUES (?, '', 1, 1, 1, ?, ?, ?)",
            (group_id, now, now, now),
        )


def _insert_memory_fact(
    connection: sqlite3.Connection,
    *,
    now: str,
    scope_type: str,
    memory_key: str,
    subject_user_id: str | None = None,
    group_id: str | None = None,
    visibility_type: str | None = None,
    visibility_user_id: str | None = None,
    visibility_group_id: str | None = None,
    **shadows: object,
) -> None:
    columns = [
        "scope_type",
        "subject_user_id",
        "group_id",
        "visibility_type",
        "visibility_user_id",
        "visibility_group_id",
        "kind",
        "memory_key",
        "category",
        "content",
        "normalized_content",
        "importance",
        "confidence",
        "source_type",
        "authority",
        "status",
        "conflict_state",
        "created_at",
        "updated_at",
        "last_confirmed_at",
    ]
    values: list[object] = [
        scope_type,
        subject_user_id,
        group_id,
        visibility_type,
        visibility_user_id,
        visibility_group_id,
        "fact" if scope_type != "self" else "preference",
        memory_key,
        "test",
        "hello",
        "hello",
        3,
        1.0,
        "explicit",
        "self_report" if scope_type != "self" else "agent_reflection",
        "active",
        "clear",
        now,
        now,
        now,
    ]
    for name, value in shadows.items():
        columns.append(name)
        values.append(value)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO memory_facts({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )


@pytest.fixture(params=["alembic", "orm"])
def c5_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    path = tmp_path / f"{request.param}.db"
    if request.param == "alembic":
        _upgrade(path, monkeypatch, "head")
    else:
        _create_orm_c5_schema(path)
    return path


def test_c5_inventory_covers_required_ownership_and_excludes_origin() -> None:
    tables = {item["table"] for item in C5_OWNERSHIP_INVENTORY}
    assert tables == {
        "people",
        "groups",
        "person_aliases",
        "memberships",
        "person_relationships",
        "relationship_events",
        "relationship_jobs",
        "person_time_settings",
        "person_speech_preferences",
        "memory_facts",
    }
    assert C5_OWNERSHIP_COLUMNS["memory_facts"] == (
        "canonical_subject_person_id",
        "canonical_subject_space_id",
        "canonical_visibility_person_id",
        "canonical_visibility_space_id",
    )
    reasons = " ".join(item["reason"] for item in C5_OWNERSHIP_INVENTORY)
    assert "not UNIQUE" in reasons
    assert "SELF" in reasons
    excluded = " ".join(reason for _name, reason in C5_EXCLUDED_OWNERSHIP)
    assert "origin/audit" in excluded
    assert "C21" in excluded
    assert "C6" in excluded
    assert not any(index.startswith("uq_") for index in C5_OWNERSHIP_INDEXES)


def test_creating_revisions_and_0045_do_not_create_c5_shadows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for revision in _CREATING_REVISIONS:
        path = tmp_path / f"at-{revision}.db"
        _upgrade(path, monkeypatch, revision)
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                revision,
            )
            assert not _c5_present(connection)
            people_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='people'"
                ).fetchone()[0]
            )
            groups_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='groups'"
                ).fetchone()[0]
            )
            assert "REFERENCES persons" not in people_sql
            assert "REFERENCES spaces" not in groups_sql
            if revision == "0045":
                assert set(CHAT_EVENT_CANONICAL_SHADOW_COLUMNS) <= set(
                    _column_names(connection, "chat_events")
                )
                trigger_names = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                }
                assert set(C4_TRIGGER_NAMES) <= trigger_names
                assert not (set(C5_TRIGGER_NAMES) & trigger_names)


def test_fresh_upgrade_head_creates_only_inventory_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fresh-head.db"
    _upgrade(path, monkeypatch, "head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0048",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, columns in C5_OWNERSHIP_COLUMNS.items():
            assert set(columns) <= set(_column_names(connection, table))
            for column in columns:
                info = {
                    str(row[1]): row for row in connection.execute(f'PRAGMA table_info("{table}")')
                }
                assert info[column][3] == 0
                assert info[column][4] is None
        assert set(C5_OWNERSHIP_INDEXES) <= _index_names(connection, "people") | set().union(
            *(_index_names(connection, table) for table in C5_OWNERSHIP_TABLES)
        )
        for table in C5_OWNERSHIP_TABLES:
            flags = {
                str(row[1]): int(row[2])
                for row in connection.execute(f'PRAGMA index_list("{table}")')
            }
            for item in set(C5_OWNERSHIP_INDEXES) & set(flags):
                assert flags[item] == 0
        shadow_fks = [
            (table, row[2], row[3], row[4], row[5], row[6], row[7])
            for table in C5_OWNERSHIP_TABLES
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
            if str(row[3]) in C5_OWNERSHIP_COLUMNS[table]
        ]
        assert set(shadow_fks) == set(_EXPECTED_FKS)
        trigger_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert set(C5_TRIGGER_NAMES) <= trigger_names
        assert set(C4_TRIGGER_NAMES) <= trigger_names


def test_empty_fresh_and_0045_to_0046_schemas_are_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = tmp_path / "fresh.db"
    upgraded = tmp_path / "from-0045.db"
    _upgrade(fresh, monkeypatch, "0046")
    _upgrade(upgraded, monkeypatch, "0045")
    before = _schema_dump(upgraded)
    _upgrade(upgraded, monkeypatch, "0046")
    assert _normalized_schema(fresh) == _normalized_schema(upgraded)
    after = _schema_dump(upgraded)
    preserved = {"alembic_version", *C5_OWNERSHIP_TABLES}
    assert all(after[key] == sql for key, sql in before.items() if key[1] not in preserved)


def test_orm_metadata_matches_0046_c5_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrated = tmp_path / "migrated.db"
    orm = tmp_path / "orm.db"
    _upgrade(migrated, monkeypatch, "head")
    _create_orm_c5_schema(orm)
    assert _normalized_c5_schema(migrated) == _normalized_c5_schema(orm)


def test_metadata_create_all_installs_c5_triggers_without_private_helper(
    tmp_path: Path,
) -> None:
    assert event.contains(Base.metadata, "after_create", _install_c5_triggers_after_metadata_create)
    path = tmp_path / "full-create-all.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    event.listen(engine, "connect", _enable_sqlite_fk)
    Base.metadata.create_all(engine)
    names = _sqlite_trigger_names(path)
    assert set(C5_TRIGGER_NAMES) <= names
    assert len(set(C5_TRIGGER_NAMES) & names) == len(C5_TRIGGER_NAMES)
    Base.metadata.create_all(engine)
    assert _sqlite_trigger_names(path) == names
    engine.dispose()


def test_create_all_waits_until_all_c5_hosts_exist(tmp_path: Path) -> None:
    path = tmp_path / "partial-create-all.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    event.listen(engine, "connect", _enable_sqlite_fk)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["persons"],
            Base.metadata.tables["spaces"],
            Base.metadata.tables["people"],
            Base.metadata.tables["groups"],
        ],
    )
    assert not (set(C5_TRIGGER_NAMES) & _sqlite_trigger_names(path))
    Base.metadata.create_all(engine)
    assert set(C5_TRIGGER_NAMES) <= _sqlite_trigger_names(path)
    engine.dispose()


def test_c5_metadata_hook_skips_non_sqlite_dialect() -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeConnection:
        dialect = FakeDialect()

        def execute(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("sqlite trigger SQL must not run")

    _install_c5_triggers_after_metadata_create(Base.metadata, FakeConnection())  # type: ignore[arg-type]


def test_alembic_heads_is_exactly_0046() -> None:
    config = Config("alembic.ini")
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["0048"]


def test_0046_is_self_contained_alembic() -> None:
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "qq_ai_bot" not in source
    assert "Base.metadata" not in source
    assert "use_alter" not in source
    assert "foreign_keys=OFF" not in source
    assert "autocommit_block" not in source
    assert "batch_alter_table" not in source
    assert "not enforced" not in source
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert {name.split(".")[0] for name in imported} <= {
        "__future__",
        "collections",
        "sqlalchemy",
        "alembic",
    }
    loaded = SourceFileLoader("revision_0046", str(_MIGRATION_PATH)).load_module()
    assert loaded._C5_TRIGGER_NAMES == C5_TRIGGER_NAMES
    assert loaded._OWNERSHIP_INDEXES == C5_OWNERSHIP_INDEXES
    legacy_alias = next(
        item
        for item in loaded._C5_TRIGGER_SQL
        if "trg_person_aliases_ownership_shadow_update" in item
    )
    current_alias = next(
        item for item in C5_TRIGGER_SQL if "trg_person_aliases_ownership_shadow_update" in item
    )
    assert "UPDATE OF canonical_person_id, canonical_space_id ON person_aliases" in legacy_alias
    assert "group_scope" not in legacy_alias.split("BEGIN", 1)[0]
    assert "group_scope" in current_alias.split("BEGIN", 1)[0]


def test_0005_lists_c5_people_group_shadows() -> None:
    source = Path("migrations/versions/0005_person_centric_v1.py").read_text(encoding="utf-8")
    assert '"canonical_person_id"' in source
    assert '"canonical_space_id"' in source
    assert "ix_people_canonical_person_id" in source
    assert "uq_chat_events_canonical_event_keeper" in source
    assert "_table_without_future_shadows" in source
    assert "_C4_CHAT_EVENT_SHADOW_COLUMNS" in source


def test_explicit_create_revisions_do_not_mention_c5_columns() -> None:
    for path in _EXPLICIT_CREATE_SOURCES:
        source = path.read_text(encoding="utf-8")
        assert "canonical_person_id" not in source
        assert "canonical_space_id" not in source
        assert "canonical_subject_" not in source
        assert "canonical_visibility_" not in source


def test_populated_downgrade_0046_to_0045_preserves_legacy_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / "expected-0045.db"
    path = tmp_path / "populated-downgrade.db"
    _upgrade(expected, monkeypatch, "0045")
    _upgrade(path, monkeypatch, "0046")
    now = "2026-08-24T00:00:00+00:00"
    with _connect(path) as connection:
        ids = _seed_identity(connection, now)
        _seed_legacy_people_and_groups(connection, now)
        connection.execute(
            "INSERT INTO person_aliases(user_id, group_scope, alias, alias_type, "
            "first_seen_at, last_seen_at) VALUES ('peer-1', '', 'Ada', 'nickname', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO memberships(user_id, group_id, group_card, first_seen_at, last_seen_at) "
            "VALUES ('peer-1', '2001', 'card', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO person_relationships(user_id, affection_score, trust_score, "
            "created_at, updated_at) VALUES ('peer-1', 50, 50, ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO relationship_events("
            "user_id, actor_user_id, change_type, affection_before, affection_delta, "
            "affection_after, trust_before, trust_delta, trust_after, reason_code, created_at"
            ") VALUES ('peer-1', '1001', 'manual', 50, 1, 51, 50, 0, 50, 'manual_set', ?)",
            (now,),
        )
        connection.execute(
            "INSERT INTO person_time_settings(user_id, timezone, created_at, updated_at) "
            "VALUES ('peer-1', 'Asia/Shanghai', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO person_speech_preferences(user_id, mode, source_message_id, "
            "created_at, updated_at) VALUES ('peer-1', 'auto', '', ?, ?)",
            (now, now),
        )
        _insert_memory_fact(
            connection,
            now=now,
            scope_type="person",
            memory_key="legacy:person",
            subject_user_id="peer-1",
        )
        connection.execute(
            "UPDATE people SET canonical_person_id=? WHERE user_id IN ('peer-1', 'peer-2')",
            (ids["person_a"],),
        )
        connection.execute(
            "UPDATE groups SET canonical_space_id=? WHERE group_id IN ('2001', '2002')",
            (ids["space_a"],),
        )
        connection.commit()
        people_before = connection.execute(
            "SELECT user_id, nickname, enabled, is_bot FROM people ORDER BY user_id"
        ).fetchall()
        groups_before = connection.execute(
            "SELECT group_id, name, enabled FROM groups ORDER BY group_id"
        ).fetchall()
        facts_before = connection.execute(
            "SELECT scope_type, subject_user_id, group_id, memory_key, visibility_type "
            "FROM memory_facts ORDER BY memory_key"
        ).fetchall()
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    _alembic_downgrade(path, monkeypatch, "0045")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0045",)
        assert not _c5_present(connection)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT user_id, nickname, enabled, is_bot FROM people ORDER BY user_id"
            ).fetchall()
            == people_before
        )
        assert (
            connection.execute(
                "SELECT group_id, name, enabled FROM groups ORDER BY group_id"
            ).fetchall()
            == groups_before
        )
        assert (
            connection.execute(
                "SELECT scope_type, subject_user_id, group_id, memory_key, visibility_type "
                "FROM memory_facts ORDER BY memory_key"
            ).fetchall()
            == facts_before
        )
    assert _normalized_schema(path) == _normalized_schema(expected)


def test_legacy_writers_leave_shadows_null(c5_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c5_db) as connection:
        _seed_legacy_people_and_groups(connection, now)
        connection.execute(
            "INSERT INTO person_aliases(user_id, group_scope, alias, alias_type, "
            "first_seen_at, last_seen_at) VALUES ('peer-1', '2001', 'Ada', 'group_card', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO memberships(user_id, group_id, group_card, first_seen_at, last_seen_at) "
            "VALUES ('peer-1', '2001', '', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO person_relationships(user_id, affection_score, trust_score, "
            "created_at, updated_at) VALUES ('peer-1', 40, 60, ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO relationship_events("
            "user_id, actor_user_id, change_type, affection_before, affection_delta, "
            "affection_after, trust_before, trust_delta, trust_after, reason_code, created_at"
            ") VALUES ('peer-1', '1001', 'manual', 40, 0, 40, 60, 0, 60, 'manual_set', ?)",
            (now,),
        )
        connection.execute(
            "INSERT INTO person_time_settings(user_id, timezone, created_at, updated_at) "
            "VALUES ('peer-1', 'UTC', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO person_speech_preferences(user_id, mode, source_message_id, "
            "created_at, updated_at) VALUES ('peer-1', 'text_only', '', ?, ?)",
            (now, now),
        )
        _insert_memory_fact(
            connection,
            now=now,
            scope_type="person",
            memory_key="null-shadow",
            subject_user_id="peer-1",
        )
        connection.commit()
        assert connection.execute(
            "SELECT canonical_person_id FROM people WHERE user_id='peer-1'"
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT canonical_space_id FROM groups WHERE group_id='2001'"
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT canonical_person_id, canonical_space_id FROM memberships"
        ).fetchone() == (None, None)
        assert connection.execute(
            "SELECT canonical_subject_person_id, canonical_visibility_person_id "
            "FROM memory_facts WHERE memory_key='null-shadow'"
        ).fetchone() == (None, None)


def test_many_legacy_rows_can_share_one_canonical_owner(c5_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c5_db) as connection:
        ids = _seed_identity(connection, now)
        _seed_legacy_people_and_groups(connection, now)
        connection.execute(
            "UPDATE people SET canonical_person_id=? WHERE user_id IN ('peer-1', 'peer-2')",
            (ids["person_a"],),
        )
        connection.execute(
            "UPDATE groups SET canonical_space_id=? WHERE group_id IN ('2001', '2002')",
            (ids["space_a"],),
        )
        connection.execute(
            "INSERT INTO person_aliases(user_id, group_scope, alias, alias_type, "
            "first_seen_at, last_seen_at, canonical_person_id, canonical_space_id) "
            "VALUES ('peer-1', '2001', 'Ada', 'group_card', ?, ?, ?, ?)",
            (now, now, ids["person_a"], ids["space_a"]),
        )
        connection.execute(
            "INSERT INTO person_aliases(user_id, group_scope, alias, alias_type, "
            "first_seen_at, last_seen_at, canonical_person_id, canonical_space_id) "
            "VALUES ('peer-2', '2002', 'Bea', 'group_card', ?, ?, ?, ?)",
            (now, now, ids["person_a"], ids["space_a"]),
        )
        connection.execute(
            "INSERT INTO memberships(user_id, group_id, group_card, first_seen_at, "
            "last_seen_at, canonical_person_id, canonical_space_id) "
            "VALUES ('peer-1', '2001', '', ?, ?, ?, ?)",
            (now, now, ids["person_a"], ids["space_a"]),
        )
        connection.execute(
            "INSERT INTO memberships(user_id, group_id, group_card, first_seen_at, "
            "last_seen_at, canonical_person_id, canonical_space_id) "
            "VALUES ('peer-2', '2002', '', ?, ?, ?, ?)",
            (now, now, ids["person_a"], ids["space_a"]),
        )
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM people WHERE canonical_person_id=?",
            (ids["person_a"],),
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM groups WHERE canonical_space_id=?",
            (ids["space_a"],),
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM memberships WHERE canonical_person_id=? AND canonical_space_id=?",
            (ids["person_a"], ids["space_a"]),
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT canonical_person_id FROM people WHERE user_id='bot-1'"
        ).fetchone() == (None,)
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO person_aliases(user_id, group_scope, alias, alias_type, "
                "first_seen_at, last_seen_at, canonical_person_id, canonical_space_id) "
                "VALUES ('peer-1', '', 'NoScope', 'nickname', ?, ?, ?, ?)",
                (now, now, ids["person_a"], ids["space_a"]),
            )
        connection.rollback()


def test_memory_scope_and_visibility_shadow_contract(c5_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c5_db) as connection:
        ids = _seed_identity(connection, now)
        _seed_legacy_people_and_groups(connection, now)
        connection.commit()
        _insert_memory_fact(
            connection,
            now=now,
            scope_type="person",
            memory_key="scope:person",
            subject_user_id="peer-1",
            canonical_subject_person_id=ids["person_a"],
        )
        _insert_memory_fact(
            connection,
            now=now,
            scope_type="person_group",
            memory_key="scope:person-group",
            subject_user_id="peer-1",
            group_id="2001",
            canonical_subject_person_id=ids["person_a"],
            canonical_subject_space_id=ids["space_a"],
        )
        _insert_memory_fact(
            connection,
            now=now,
            scope_type="group",
            memory_key="scope:group",
            group_id="2001",
            canonical_subject_space_id=ids["space_a"],
        )
        _insert_memory_fact(
            connection,
            now=now,
            scope_type="self",
            memory_key="scope:self-global",
            visibility_type="global",
        )
        _insert_memory_fact(
            connection,
            now=now,
            scope_type="self",
            memory_key="scope:self-private",
            visibility_type="private",
            visibility_user_id="peer-1",
            canonical_visibility_person_id=ids["person_a"],
        )
        _insert_memory_fact(
            connection,
            now=now,
            scope_type="self",
            memory_key="scope:self-group",
            visibility_type="group",
            visibility_group_id="2001",
            canonical_visibility_space_id=ids["space_a"],
        )
        connection.commit()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_memory_fact(
                connection,
                now=now,
                scope_type="self",
                memory_key="self-subject-person",
                visibility_type="global",
                canonical_subject_person_id=ids["person_a"],
            )
        connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_memory_fact(
                connection,
                now=now,
                scope_type="person",
                memory_key="person-space",
                subject_user_id="peer-1",
                canonical_subject_space_id=ids["space_a"],
            )
        connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_memory_fact(
                connection,
                now=now,
                scope_type="person",
                memory_key="uuid1",
                subject_user_id="peer-1",
                canonical_subject_person_id=str(uuid1()),
            )
        connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_memory_fact(
                connection,
                now=now,
                scope_type="person",
                memory_key="upper",
                subject_user_id="peer-1",
                canonical_subject_person_id=ids["person_a"].upper(),
            )
        connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_memory_fact(
                connection,
                now=now,
                scope_type="group",
                memory_key="group-person",
                group_id="2001",
                canonical_subject_person_id=ids["person_a"],
            )
        connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_memory_fact(
                connection,
                now=now,
                scope_type="person",
                memory_key="person-visibility",
                subject_user_id="peer-1",
                canonical_visibility_person_id=ids["person_a"],
            )
        connection.rollback()
        missing = str(uuid4())
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_memory_fact(
                connection,
                now=now,
                scope_type="person",
                memory_key="dangling",
                subject_user_id="peer-1",
                canonical_subject_person_id=missing,
            )
        connection.rollback()


def test_old_uniques_and_hashes_still_hold(c5_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c5_db) as connection:
        _seed_legacy_people_and_groups(connection, now)
        _insert_memory_fact(
            connection,
            now=now,
            scope_type="person",
            memory_key="dup-key",
            subject_user_id="peer-1",
        )
        connection.commit()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_memory_fact(
                connection,
                now=now,
                scope_type="person",
                memory_key="dup-key",
                subject_user_id="peer-1",
            )
        connection.rollback()
        connection.execute(
            "INSERT INTO person_aliases(user_id, group_scope, alias, alias_type, "
            "first_seen_at, last_seen_at) VALUES ('peer-1', '', 'Ada', 'nickname', ?, ?)",
            (now, now),
        )
        connection.commit()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO person_aliases(user_id, group_scope, alias, alias_type, "
                "first_seen_at, last_seen_at) VALUES ('peer-1', '', 'Ada', 'nickname', ?, ?)",
                (now, now),
            )
        connection.rollback()
        connection.execute(
            "INSERT INTO memberships(user_id, group_id, group_card, first_seen_at, last_seen_at) "
            "VALUES ('peer-1', '2001', '', ?, ?)",
            (now, now),
        )
        connection.commit()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO memberships(user_id, group_id, group_card, first_seen_at, "
                "last_seen_at) VALUES ('peer-1', '2001', '', ?, ?)",
                (now, now),
            )
        connection.rollback()
        connection.execute(
            "INSERT INTO person_relationships(user_id, affection_score, trust_score, "
            "created_at, updated_at) VALUES ('peer-1', 10, 10, ?, ?)",
            (now, now),
        )
        connection.commit()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO person_relationships(user_id, affection_score, trust_score, "
                "created_at, updated_at) VALUES ('peer-1', 20, 20, ?, ?)",
                (now, now),
            )
        connection.rollback()
        _insert_memory_fact(
            connection,
            now=now,
            scope_type="self",
            memory_key="self-dup",
            visibility_type="global",
        )
        connection.commit()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_memory_fact(
                connection,
                now=now,
                scope_type="self",
                memory_key="self-dup",
                visibility_type="global",
            )
        connection.rollback()


def test_ownership_foreign_keys_reject_dangling_and_parent_mutation(c5_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c5_db) as connection:
        ids = _seed_identity(connection, now)
        _seed_legacy_people_and_groups(connection, now)
        isolated_person = str(uuid4())
        isolated_space = str(uuid4())
        connection.execute(
            "INSERT INTO persons(id, enabled, revision, created_at, updated_at) "
            "VALUES (?, 1, 1, ?, ?)",
            (isolated_person, now, now),
        )
        connection.execute(
            "INSERT INTO spaces(id, name, enabled, autonomous_enabled, require_mention, "
            "revision, created_at, updated_at) VALUES (?, '', 1, 1, 1, 1, ?, ?)",
            (isolated_space, now, now),
        )
        connection.execute(
            "UPDATE people SET canonical_person_id=? WHERE user_id='peer-1'",
            (isolated_person,),
        )
        connection.execute(
            "UPDATE groups SET canonical_space_id=? WHERE group_id='2001'",
            (isolated_space,),
        )
        connection.commit()

        def snapshot() -> tuple[object, ...]:
            return (
                connection.execute(
                    "SELECT id, revision FROM persons WHERE id=?",
                    (isolated_person,),
                ).fetchone(),
                connection.execute(
                    "SELECT id, revision FROM spaces WHERE id=?",
                    (isolated_space,),
                ).fetchone(),
                connection.execute(
                    "SELECT user_id, canonical_person_id FROM people WHERE user_id='peer-1'"
                ).fetchone(),
                connection.execute(
                    "SELECT group_id, canonical_space_id FROM groups WHERE group_id='2001'"
                ).fetchone(),
            )

        before = snapshot()
        missing = str(uuid4())
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE people SET canonical_person_id=? WHERE user_id='peer-2'",
                (missing,),
            )
        connection.rollback()
        parent_mutations = (
            ("DELETE FROM persons WHERE id=?", (isolated_person,)),
            ("UPDATE persons SET id=? WHERE id=?", (missing, isolated_person)),
            ("DELETE FROM spaces WHERE id=?", (isolated_space,)),
            ("UPDATE spaces SET id=? WHERE id=?", (missing, isolated_space)),
        )
        for statement, params in parent_mutations:
            connection.execute("BEGIN")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, params)
            connection.rollback()
        assert snapshot() == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.execute(
            "UPDATE persons SET revision=2, updated_at=? WHERE id=?",
            (now, isolated_person),
        )
        connection.execute(
            "UPDATE spaces SET revision=2, updated_at=? WHERE id=?",
            (now, isolated_space),
        )
        connection.commit()
        assert connection.execute(
            "SELECT revision FROM persons WHERE id=?",
            (isolated_person,),
        ).fetchone() == (2,)
        assert ids["person_a"] != isolated_person


def test_old_writer_inventory_does_not_pass_shadow_columns() -> None:
    found_writers = 0
    found_create_fact = 0
    memory_fact_shadows = set(C5_OWNERSHIP_COLUMNS["memory_facts"])
    writer_names = {
        "PersonModel",
        "GroupModel",
        "PersonAliasModel",
        "MembershipModel",
        "PersonRelationshipModel",
        "RelationshipEventModel",
        "RelationshipJobModel",
        "PersonTimeSettingModel",
        "PersonSpeechPreferenceModel",
        "MemoryFactModel",
    }
    for path in _KNOWN_WRITERS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in writer_names:
                        found_writers += 1
                        keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
                        if node.func.id == "MemoryFactModel" and fn.name == "create_fact":
                            found_create_fact += 1
                            assert keywords & _SHADOW_NAMES == memory_fact_shadows
                        else:
                            assert keywords.isdisjoint(_SHADOW_NAMES)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "INSERT INTO " in node.value:
                    for table in C5_OWNERSHIP_TABLES:
                        if f"INSERT INTO {table}" in node.value:
                            found_writers += 1
                            for column in _SHADOW_NAMES:
                                assert column not in node.value
    assert found_writers >= 8
    assert found_create_fact == 1
