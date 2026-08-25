"""Canonical conversations, routes, receipts, and 0044 cutover-descendant proofs."""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid1, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable
from tests.unit.test_migration_0043 import (
    _downgrade as _alembic_downgrade,
)
from tests.unit.test_migration_0043 import (
    _normalized_schema,
    _schema_dump,
    _tables,
    _upgrade,
)

from qq_ai_bot.conversation.canonical_db_models import (
    CANONICAL_CONVERSATION_CREATE_ORDER,
    CANONICAL_CONVERSATION_TABLES,
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.conversation.canonical_schema import C3_TRIGGER_NAMES, C3_TRIGGER_SQL
from qq_ai_bot.identity.db_models import CANONICAL_IDENTITY_CREATE_ORDER, CANONICAL_IDENTITY_TABLES
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.metadata import Base

_MIGRATION_PATH = Path("migrations/versions/0044_canonical_conversations_and_routes.py")
_FORBIDDEN_TABLES = {
    "yuki",
    "yukis",
    "yuki_self",
    "yukiself",
    "gateway_connections",
    "presence_active_routes",
    "delivery_routes",
}
_SECRET_COLUMN_TOKENS = (
    "secret",
    "token",
    "password",
    "api_key",
    "cookie",
    "authorization",
    "private_key",
    "credential",
)
_VALID_HASH = "a" * 64
_VALID_STATE = '{"enabled":true,"revision":1}'
_REQUIRED_CONSTRAINT_FRAGMENTS = (
    "fk_canonical_conversations_primary_alias",
    "ck_canonical_conversations_owner_xor",
    "ck_canonical_conversations_primary_marker",
    "uq_conversation_legacy_aliases_primary_target",
    "uq_conversation_legacy_aliases_scope_key",
    "uq_control_command_receipts_principal_request",
    "ck_control_command_receipts_payload_hash",
    "ck_control_command_receipts_lifecycle",
    "ck_control_command_receipts_effective_state",
    "fk_control_command_receipts_audit",
)


def _enable_sqlite_fk(dbapi_connection: object, _record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _create_orm_conversation_schema(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    event.listen(engine, "connect", _enable_sqlite_fk)
    tables = [
        Base.metadata.tables[name]
        for name in (
            *CANONICAL_IDENTITY_CREATE_ORDER,
            "admin_operation_events",
            *CANONICAL_CONVERSATION_CREATE_ORDER,
        )
    ]
    Base.metadata.create_all(engine, tables=tables)
    engine.dispose()


def _downgrade(path: Path, monkeypatch: pytest.MonkeyPatch, revision: str) -> None:
    _alembic_downgrade(path, monkeypatch, revision)


def _normalized_conversation_schema(path: Path) -> dict[str, Any]:
    full = _normalized_schema(path)
    tables = [name for name in full["tables"] if name in CANONICAL_CONVERSATION_TABLES]
    return {
        "tables": tables,
        "columns": {name: full["columns"][name] for name in tables},
        "indexes": {name: full["indexes"][name] for name in tables},
        "foreign_keys": {name: full["foreign_keys"][name] for name in tables},
        "constraints": {name: full["constraints"][name] for name in tables},
        "triggers": {
            name: sql for name, sql in full["triggers"].items() if name in C3_TRIGGER_NAMES
        },
    }


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _insert_audit(connection: sqlite3.Connection, now: str) -> int:
    connection.execute(
        "INSERT INTO admin_operation_events("
        "actor_user_id, trigger_message_id, conversation_key, capability, operation, "
        "target_type, target_id, before_json, after_json, success, duration_seconds, "
        "created_at"
        ") VALUES ('audit-actor', '', '', 'identity.mutate', 'test', 'person', '', "
        "'null', 'null', 1, 0, ?)",
        (now,),
    )
    row = connection.execute("SELECT last_insert_rowid()").fetchone()
    assert row is not None
    return int(row[0])


def _parent_and_route_snapshot(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    return (
        connection.execute(
            "SELECT id, person_id, platform, display_name, status, revision "
            "FROM identity_bindings ORDER BY id"
        ).fetchall(),
        connection.execute(
            "SELECT id, space_id, platform, display_name, status, revision "
            "FROM space_bindings ORDER BY id"
        ).fetchall(),
        connection.execute(
            "SELECT id, platform, enabled, ingest_eligible, revision FROM presences ORDER BY id"
        ).fetchall(),
        connection.execute(
            "SELECT person_id, identity_binding_id, presence_id, route_generation, "
            "paused, revision FROM person_active_routes ORDER BY person_id"
        ).fetchall(),
        connection.execute(
            "SELECT space_binding_id, ingest_presence_id, route_generation, paused, "
            "revision FROM space_binding_ingest_routes ORDER BY space_binding_id"
        ).fetchall(),
        connection.execute(
            "SELECT space_id, space_binding_id, presence_id, route_generation, paused, "
            "revision FROM space_active_routes ORDER BY space_id"
        ).fetchall(),
    )


def _seed_identity(connection: sqlite3.Connection, now: str) -> dict[str, str]:
    ids = {
        "person_a": str(uuid4()),
        "person_b": str(uuid4()),
        "space_a": str(uuid4()),
        "space_b": str(uuid4()),
        "binding_a": str(uuid4()),
        "binding_b": str(uuid4()),
        "space_binding_a": str(uuid4()),
        "space_binding_b": str(uuid4()),
        "presence_qq": str(uuid4()),
        "presence_telegram": str(uuid4()),
    }
    for person_id in (ids["person_a"], ids["person_b"]):
        connection.execute(
            "INSERT INTO persons(id, enabled, revision, created_at, updated_at) "
            "VALUES (?, 1, 1, ?, ?)",
            (person_id, now, now),
        )
    for space_id in (ids["space_a"], ids["space_b"]):
        connection.execute(
            "INSERT INTO spaces(id, name, enabled, autonomous_enabled, require_mention, "
            "revision, created_at, updated_at) VALUES (?, '', 1, 1, 1, 1, ?, ?)",
            (space_id, now, now),
        )
    connection.execute(
        "INSERT INTO identity_bindings("
        "id, person_id, platform, external_account_id, display_name, status, "
        "revision, created_at, updated_at"
        ") VALUES (?, ?, 'qq', '1001', '', 'active', 1, ?, ?)",
        (ids["binding_a"], ids["person_a"], now, now),
    )
    connection.execute(
        "INSERT INTO identity_bindings("
        "id, person_id, platform, external_account_id, display_name, status, "
        "revision, created_at, updated_at"
        ") VALUES (?, ?, 'qq', '1002', '', 'active', 1, ?, ?)",
        (ids["binding_b"], ids["person_b"], now, now),
    )
    connection.execute(
        "INSERT INTO space_bindings("
        "id, space_id, platform, external_space_id, display_name, status, "
        "revision, created_at, updated_at"
        ") VALUES (?, ?, 'qq', '2001', '', 'active', 1, ?, ?)",
        (ids["space_binding_a"], ids["space_a"], now, now),
    )
    connection.execute(
        "INSERT INTO space_bindings("
        "id, space_id, platform, external_space_id, display_name, status, "
        "revision, created_at, updated_at"
        ") VALUES (?, ?, 'qq', '2002', '', 'active', 1, ?, ?)",
        (ids["space_binding_b"], ids["space_b"], now, now),
    )
    connection.execute(
        "INSERT INTO presences("
        "id, platform, external_account_id, enabled, ingest_eligible, "
        "revision, created_at, updated_at"
        ") VALUES (?, 'qq', '8000', 1, 1, 1, ?, ?)",
        (ids["presence_qq"], now, now),
    )
    connection.execute(
        "INSERT INTO presences("
        "id, platform, external_account_id, enabled, ingest_eligible, "
        "revision, created_at, updated_at"
        ") VALUES (?, 'telegram', '9000', 1, 1, 1, ?, ?)",
        (ids["presence_telegram"], now, now),
    )
    connection.commit()
    return ids


def _insert_conversation(
    connection: sqlite3.Connection,
    *,
    conversation_id: str,
    alias_id: str,
    kind: str,
    owner_id: str,
    now: str,
    generation: int = 1,
    extra_aliases: tuple[tuple[str, str], ...] = (),
) -> None:
    person_id = owner_id if kind == "private" else None
    space_id = owner_id if kind == "space" else None
    connection.execute("BEGIN")
    connection.execute(
        "INSERT INTO canonical_conversations("
        "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
        "starts_after_event_id, last_event_id, last_generation_change_event_id, "
        "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
        "revision, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, 1, ?, 0, 0, 0, 0, 0, 0, 1, ?, ?)",
        (conversation_id, kind, person_id, space_id, alias_id, generation, now, now),
    )
    connection.execute(
        "INSERT INTO conversation_legacy_aliases("
        "id, conversation_id, scope_key, is_primary, created_at, updated_at"
        ") VALUES (?, ?, ?, 1, ?, ?)",
        (alias_id, conversation_id, f"scope:{alias_id}", now, now),
    )
    for extra_id, scope_key in extra_aliases:
        connection.execute(
            "INSERT INTO conversation_legacy_aliases("
            "id, conversation_id, scope_key, is_primary, created_at, updated_at"
            ") VALUES (?, ?, ?, 0, ?, ?)",
            (extra_id, conversation_id, scope_key, now, now),
        )
    connection.execute("COMMIT")


@pytest.fixture(params=["alembic", "orm"])
def c3_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    path = tmp_path / f"{request.param}.db"
    if request.param == "alembic":
        _upgrade(path, monkeypatch, "head")
    else:
        _create_orm_conversation_schema(path)
    return path


def test_0005_does_not_create_canonical_conversation_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "at-0005.db"
    _upgrade(path, monkeypatch, "0005")
    with sqlite3.connect(path) as connection:
        assert not (set(CANONICAL_CONVERSATION_TABLES) & _tables(connection))


def test_0043_does_not_create_canonical_conversation_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "at-0043.db"
    _upgrade(path, monkeypatch, "0043")
    with sqlite3.connect(path) as connection:
        tables = _tables(connection)
        assert not (set(CANONICAL_CONVERSATION_TABLES) & tables)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0043",)
        trigger_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert not (set(C3_TRIGGER_NAMES) & trigger_names)


def test_fresh_upgrade_head_creates_canonical_conversations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fresh-head.db"
    _upgrade(path, monkeypatch, "head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0048",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = _tables(connection)
        assert set(CANONICAL_CONVERSATION_TABLES) <= tables
        assert set(CANONICAL_IDENTITY_TABLES) <= tables
        assert not (_FORBIDDEN_TABLES & tables)
        schema_sql = "\n".join(
            str(row[0] or "") for row in connection.execute("SELECT sql FROM sqlite_master")
        )
        for fragment in _REQUIRED_CONSTRAINT_FRAGMENTS:
            assert fragment in schema_sql
        trigger_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert set(C3_TRIGGER_NAMES) <= trigger_names


def test_upgrade_from_0043_to_0044(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "from-0043.db"
    _upgrade(path, monkeypatch, "0043")
    before = _schema_dump(path)
    _upgrade(path, monkeypatch, "0044")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0044",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert set(CANONICAL_CONVERSATION_TABLES) <= _tables(connection)
    after = _schema_dump(path)
    assert all(after[key] == sql for key, sql in before.items() if key[1] != "alembic_version")


def test_fresh_head_and_0043_to_head_schemas_are_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = tmp_path / "fresh.db"
    upgraded = tmp_path / "from-0043.db"
    _upgrade(fresh, monkeypatch, "head")
    _upgrade(upgraded, monkeypatch, "0043")
    _upgrade(upgraded, monkeypatch, "head")
    assert _normalized_schema(fresh) == _normalized_schema(upgraded)


def test_downgrade_0044_removes_only_conversation_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / "expected-0043.db"
    path = tmp_path / "downgrade.db"
    _upgrade(expected, monkeypatch, "0043")
    _upgrade(path, monkeypatch, "0044")
    _downgrade(path, monkeypatch, "0043")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0043",)
        assert not (set(CANONICAL_CONVERSATION_TABLES) & _tables(connection))
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _normalized_schema(path) == _normalized_schema(expected)


def test_populated_downgrade_0044_to_0043_with_foreign_keys_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / "expected-0043.db"
    path = tmp_path / "populated-downgrade.db"
    _upgrade(expected, monkeypatch, "0043")
    _upgrade(path, monkeypatch, "0044")
    now = "2026-08-24T00:00:00+00:00"
    with _connect(path) as connection:
        ids = _seed_identity(connection, now)
        conversation_id = str(uuid4())
        alias_id = str(uuid4())
        _insert_conversation(
            connection,
            conversation_id=conversation_id,
            alias_id=alias_id,
            kind="private",
            owner_id=ids["person_a"],
            now=now,
            extra_aliases=((str(uuid4()), "legacy:extra"),),
        )
        connection.execute(
            "INSERT INTO person_active_routes("
            "person_id, identity_binding_id, presence_id, route_generation, paused, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, ?, 1, 0, 1, ?, ?)",
            (ids["person_a"], ids["binding_a"], ids["presence_qq"], now, now),
        )
        connection.execute(
            "INSERT INTO space_binding_ingest_routes("
            "space_binding_id, ingest_presence_id, route_generation, paused, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, 1, 0, 1, ?, ?)",
            (ids["space_binding_a"], ids["presence_qq"], now, now),
        )
        connection.execute(
            "INSERT INTO space_active_routes("
            "space_id, space_binding_id, presence_id, route_generation, paused, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, ?, 1, 0, 1, ?, ?)",
            (ids["space_a"], ids["space_binding_a"], ids["presence_qq"], now, now),
        )
        audit_id = _insert_audit(connection, now)
        connection.execute(
            "INSERT INTO control_command_receipts("
            "principal_id, request_id, payload_hash, status, result_resource_id, "
            "effective_state_json, result_revision, audit_id, created_at, updated_at"
            ") VALUES (?, ?, ?, 'succeeded', 'conversation.rollup.enabled', ?, 1, ?, ?, ?)",
            (str(uuid4()), str(uuid4()), _VALID_HASH, _VALID_STATE, audit_id, now, now),
        )
        connection.commit()
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone() == (1,)
    _downgrade(path, monkeypatch, "0043")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0043",)
        assert not (set(CANONICAL_CONVERSATION_TABLES) & _tables(connection))
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _normalized_schema(path) == _normalized_schema(expected)


def test_orm_metadata_matches_0044_conversation_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrated = tmp_path / "migrated.db"
    orm = tmp_path / "orm.db"
    _upgrade(migrated, monkeypatch, "head")
    _create_orm_conversation_schema(orm)
    assert set(CANONICAL_CONVERSATION_TABLES) <= set(Base.metadata.tables)
    assert _normalized_conversation_schema(migrated) == _normalized_conversation_schema(orm)


def test_alembic_heads_is_exactly_0044() -> None:
    config = Config("alembic.ini")
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["0048"]


def test_0044_is_self_contained_alembic() -> None:
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "qq_ai_bot" not in source
    assert "Base.metadata" not in source
    assert "use_alter" not in source
    assert "foreign_keys=OFF" not in source
    assert "autocommit_block" not in source
    assert "batch_alter_table" not in source
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
    for statement in C3_TRIGGER_SQL:
        assert statement in source


def test_0005_excludes_conversation_tables_as_frozen_literals() -> None:
    source = Path("migrations/versions/0005_person_centric_v1.py").read_text(encoding="utf-8")
    assert "qq_ai_bot.conversation" not in source
    assert "CANONICAL_CONVERSATION" not in source
    for table in CANONICAL_CONVERSATION_TABLES:
        assert f'"{table}"' in source


def test_cyclic_foreign_keys_are_inline_not_use_alter() -> None:
    primary = next(
        constraint
        for constraint in CanonicalConversationModel.__table__.foreign_key_constraints
        if constraint.name == "fk_canonical_conversations_primary_alias"
    )
    back = next(
        constraint
        for constraint in ConversationLegacyAliasModel.__table__.foreign_key_constraints
        if constraint.name == "fk_conversation_legacy_aliases_conversation"
    )
    assert primary.use_alter is False
    assert back.use_alter is False
    assert primary.deferrable is True
    assert primary.initially == "DEFERRED"
    assert back.deferrable is True
    assert back.initially == "DEFERRED"
    assert tuple(primary.column_keys) == ("id", "primary_alias_id", "primary_marker")
    referred = tuple(element.column.name for element in primary.elements)
    assert referred == ("conversation_id", "id", "is_primary")


def test_first_cyclic_table_create_succeeds_with_foreign_keys_on() -> None:
    sql = str(CreateTable(CanonicalConversationModel.__table__).compile(dialect=sqlite.dialect()))
    assert "REFERENCES conversation_legacy_aliases" in sql
    assert "DEFERRABLE" in sql.upper()
    assert "INITIALLY DEFERRED" in sql.upper()
    assert "is_primary" in sql
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        connection.execute(sql)
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "canonical_conversations" in tables
        assert "conversation_legacy_aliases" not in tables
    finally:
        connection.close()


def test_0044_creates_conversations_before_aliases() -> None:
    tree = ast.parse(_MIGRATION_PATH.read_text(encoding="utf-8"))
    created: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "create_table" or not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            created.append(argument.value)
    assert created[0] == "canonical_conversations"
    assert created[1] == "conversation_legacy_aliases"


def test_trigger_installers_are_table_scoped() -> None:
    from qq_ai_bot.conversation import canonical_db_models as models

    pairs = (
        (models.CanonicalConversationModel, models.install_canonical_conversation_triggers),
        (models.ConversationLegacyAliasModel, models.install_conversation_legacy_alias_triggers),
        (models.PersonActiveRouteModel, models.install_person_active_route_triggers),
        (
            models.SpaceBindingIngestRouteModel,
            models.install_space_binding_ingest_route_triggers,
        ),
        (models.SpaceActiveRouteModel, models.install_space_active_route_triggers),
    )
    for model, installer in pairs:
        assert event.contains(model.__table__, "after_create", installer)
        for name, other in Base.metadata.tables.items():
            if name == model.__tablename__:
                continue
            assert not event.contains(other, "after_create", installer)


@pytest.mark.asyncio
async def test_database_create_schema_includes_c3_tables(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'fresh.db').as_posix()}")
    try:
        await database.create_schema()
        async with database.sessions() as session:
            from sqlalchemy import text

            rows = (
                await session.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                )
            ).all()
        names = {str(row[0]) for row in rows}
        assert set(CANONICAL_CONVERSATION_TABLES) <= names
    finally:
        await database.close()


def test_valid_conversation_and_primary_alias_commit(c3_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c3_db) as connection:
        ids = _seed_identity(connection, now)
        conversation_id = str(uuid4())
        alias_id = str(uuid4())
        extra_a = str(uuid4())
        extra_b = str(uuid4())
        _insert_conversation(
            connection,
            conversation_id=conversation_id,
            alias_id=alias_id,
            kind="private",
            owner_id=ids["person_a"],
            now=now,
            extra_aliases=((extra_a, "legacy:a"), (extra_b, "legacy:b")),
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT COUNT(*) FROM conversation_legacy_aliases WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone() == (3,)
        space_conversation = str(uuid4())
        space_alias = str(uuid4())
        _insert_conversation(
            connection,
            conversation_id=space_conversation,
            alias_id=space_alias,
            kind="space",
            owner_id=ids["space_a"],
            now=now,
        )
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone() == (2,)


def test_missing_primary_alias_fails_at_commit(c3_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c3_db) as connection:
        ids = _seed_identity(connection, now)
        connection.execute("BEGIN")
        connection.execute(
            "INSERT INTO canonical_conversations("
            "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
            "starts_after_event_id, last_event_id, last_generation_change_event_id, "
            "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
            "revision, created_at, updated_at"
            ") VALUES (?, 'private', ?, NULL, ?, 1, 1, 0, 0, 0, 0, 0, 0, 1, ?, ?)",
            (str(uuid4()), ids["person_a"], str(uuid4()), now, now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("COMMIT")
        connection.execute("ROLLBACK")


def test_conversation_constraint_negatives(c3_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c3_db) as connection:
        ids = _seed_identity(connection, now)
        conversation_id = str(uuid4())
        alias_id = str(uuid4())
        _insert_conversation(
            connection,
            conversation_id=conversation_id,
            alias_id=alias_id,
            kind="private",
            owner_id=ids["person_a"],
            now=now,
            extra_aliases=((str(uuid4()), "legacy:keep"),),
        )
        dual_id = str(uuid4())
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO conversation_legacy_aliases("
                "id, conversation_id, scope_key, is_primary, created_at, updated_at"
                ") VALUES (?, ?, 'legacy:dual', 1, ?, ?)",
                (dual_id, conversation_id, now, now),
            )
        connection.rollback()

        connection.execute("BEGIN")
        other_conversation = str(uuid4())
        other_alias = str(uuid4())
        connection.execute(
            "INSERT INTO canonical_conversations("
            "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
            "starts_after_event_id, last_event_id, last_generation_change_event_id, "
            "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
            "revision, created_at, updated_at"
            ") VALUES (?, 'private', ?, NULL, ?, 1, 1, 0, 0, 0, 0, 0, 0, 1, ?, ?)",
            (other_conversation, ids["person_b"], alias_id, now, now),
        )
        connection.execute(
            "INSERT INTO conversation_legacy_aliases("
            "id, conversation_id, scope_key, is_primary, created_at, updated_at"
            ") VALUES (?, ?, 'legacy:wrong-primary', 1, ?, ?)",
            (other_alias, other_conversation, now, now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("COMMIT")
        connection.execute("ROLLBACK")

        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE canonical_conversations SET primary_alias_id=? WHERE id=?",
                (str(uuid4()), conversation_id),
            )
        connection.rollback()

        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE conversation_legacy_aliases SET is_primary=0 WHERE id=?",
                (alias_id,),
            )
        connection.rollback()

        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE conversation_legacy_aliases SET scope_key='rewritten' WHERE id=?",
                (alias_id,),
            )
        connection.rollback()

        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM conversation_legacy_aliases WHERE id=?",
                (alias_id,),
            )
        connection.rollback()

        negatives = (
            (
                "INSERT INTO canonical_conversations("
                "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
                "starts_after_event_id, last_event_id, last_generation_change_event_id, "
                "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
                "revision, created_at, updated_at"
                ") VALUES (?, 'private', ?, NULL, ?, 1, 1, 0, 0, 0, 0, 0, 0, 1, ?, ?)",
                (str(uuid4()).upper(), ids["person_b"], str(uuid4()), now, now),
            ),
            (
                "INSERT INTO canonical_conversations("
                "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
                "starts_after_event_id, last_event_id, last_generation_change_event_id, "
                "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
                "revision, created_at, updated_at"
                ") VALUES (?, 'private', ?, NULL, ?, 1, 1, 0, 0, 0, 0, 0, 0, 1, ?, ?)",
                (str(uuid1()), ids["person_b"], str(uuid4()), now, now),
            ),
            (
                "INSERT INTO canonical_conversations("
                "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
                "starts_after_event_id, last_event_id, last_generation_change_event_id, "
                "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
                "revision, created_at, updated_at"
                ") VALUES (?, 'group', ?, NULL, ?, 1, 1, 0, 0, 0, 0, 0, 0, 1, ?, ?)",
                (str(uuid4()), ids["person_b"], str(uuid4()), now, now),
            ),
            (
                "INSERT INTO canonical_conversations("
                "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
                "starts_after_event_id, last_event_id, last_generation_change_event_id, "
                "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
                "revision, created_at, updated_at"
                ") VALUES (?, 'private', ?, ?, ?, 1, 1, 0, 0, 0, 0, 0, 0, 1, ?, ?)",
                (
                    str(uuid4()),
                    ids["person_b"],
                    ids["space_a"],
                    str(uuid4()),
                    now,
                    now,
                ),
            ),
            (
                "INSERT INTO canonical_conversations("
                "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
                "starts_after_event_id, last_event_id, last_generation_change_event_id, "
                "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
                "revision, created_at, updated_at"
                ") VALUES (?, 'private', ?, NULL, ?, 1, 1, 0, 0, 0, 0, 0, 0, 1, ?, ?)",
                (str(uuid4()), ids["person_a"], str(uuid4()), now, now),
            ),
            (
                "INSERT INTO canonical_conversations("
                "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
                "starts_after_event_id, last_event_id, last_generation_change_event_id, "
                "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
                "revision, created_at, updated_at"
                ") VALUES (?, 'private', ?, NULL, ?, 2, 1, 0, 0, 0, 0, 0, 0, 1, ?, ?)",
                (str(uuid4()), ids["person_b"], str(uuid4()), now, now),
            ),
            (
                "INSERT INTO canonical_conversations("
                "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
                "starts_after_event_id, last_event_id, last_generation_change_event_id, "
                "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
                "revision, created_at, updated_at"
                ") VALUES (?, 'private', ?, NULL, ?, 1, 0, 0, 0, 0, 0, 0, 0, 1, ?, ?)",
                (str(uuid4()), ids["person_b"], str(uuid4()), now, now),
            ),
            (
                "INSERT INTO canonical_conversations("
                "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
                "starts_after_event_id, last_event_id, last_generation_change_event_id, "
                "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
                "revision, created_at, updated_at"
                ") VALUES (?, 'private', ?, NULL, ?, 1, 1, 0, 0, 0, 0, 0, 0, 0, ?, ?)",
                (str(uuid4()), ids["person_b"], str(uuid4()), now, now),
            ),
            (
                "INSERT INTO conversation_legacy_aliases("
                "id, conversation_id, scope_key, is_primary, created_at, updated_at"
                ") VALUES (?, ?, '', 0, ?, ?)",
                (str(uuid4()), conversation_id, now, now),
            ),
            (
                "INSERT INTO conversation_legacy_aliases("
                "id, conversation_id, scope_key, is_primary, created_at, updated_at"
                ") VALUES (?, ?, ' padded', 0, ?, ?)",
                (str(uuid4()), conversation_id, now, now),
            ),
            (
                "INSERT INTO conversation_legacy_aliases("
                "id, conversation_id, scope_key, is_primary, created_at, updated_at"
                ") VALUES (?, ?, ?, 0, ?, ?)",
                (str(uuid4()), conversation_id, "x" * 256, now, now),
            ),
        )
        for sql, params in negatives:
            connection.execute("BEGIN")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, params)
            connection.rollback()


def test_route_and_receipt_constraint_negatives(c3_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c3_db) as connection:
        ids = _seed_identity(connection, now)
        conversation_id = str(uuid4())
        _insert_conversation(
            connection,
            conversation_id=conversation_id,
            alias_id=str(uuid4()),
            kind="private",
            owner_id=ids["person_a"],
            now=now,
            generation=3,
        )
        connection.execute(
            "INSERT INTO person_active_routes("
            "person_id, identity_binding_id, presence_id, route_generation, paused, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, ?, 4, 0, 1, ?, ?)",
            (ids["person_a"], ids["binding_a"], ids["presence_qq"], now, now),
        )
        connection.execute(
            "INSERT INTO space_binding_ingest_routes("
            "space_binding_id, ingest_presence_id, route_generation, paused, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, 1, 0, 1, ?, ?)",
            (ids["space_binding_a"], ids["presence_qq"], now, now),
        )
        connection.execute(
            "INSERT INTO space_active_routes("
            "space_id, space_binding_id, presence_id, route_generation, paused, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, ?, 1, 0, 1, ?, ?)",
            (ids["space_a"], ids["space_binding_a"], ids["presence_qq"], now, now),
        )
        connection.commit()
        connection.execute(
            "UPDATE person_active_routes SET route_generation=5, paused=1, revision=2 "
            "WHERE person_id=?",
            (ids["person_a"],),
        )
        connection.commit()
        assert connection.execute(
            "SELECT generation FROM canonical_conversations WHERE id=?",
            (conversation_id,),
        ).fetchone() == (3,)

        route_insert_negatives = (
            (
                "INSERT INTO person_active_routes("
                "person_id, identity_binding_id, presence_id, route_generation, paused, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, ?, 1, 0, 1, ?, ?)",
                (ids["person_b"], ids["binding_a"], ids["presence_qq"], now, now),
            ),
            (
                "INSERT INTO person_active_routes("
                "person_id, identity_binding_id, presence_id, route_generation, paused, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, ?, 1, 0, 1, ?, ?)",
                (ids["person_b"], ids["binding_b"], ids["presence_telegram"], now, now),
            ),
            (
                "INSERT INTO space_binding_ingest_routes("
                "space_binding_id, ingest_presence_id, route_generation, paused, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, 1, 0, 1, ?, ?)",
                (ids["space_binding_b"], ids["presence_telegram"], now, now),
            ),
            (
                "INSERT INTO space_active_routes("
                "space_id, space_binding_id, presence_id, route_generation, paused, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, ?, 1, 0, 1, ?, ?)",
                (ids["space_b"], ids["space_binding_a"], ids["presence_qq"], now, now),
            ),
            (
                "INSERT INTO space_active_routes("
                "space_id, space_binding_id, presence_id, route_generation, paused, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, ?, 1, 0, 1, ?, ?)",
                (ids["space_b"], ids["space_binding_b"], ids["presence_telegram"], now, now),
            ),
            (
                "INSERT INTO person_active_routes("
                "person_id, identity_binding_id, presence_id, route_generation, paused, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, ?, 0, 0, 1, ?, ?)",
                (ids["person_b"], ids["binding_b"], ids["presence_qq"], now, now),
            ),
            (
                "INSERT INTO person_active_routes("
                "person_id, identity_binding_id, presence_id, route_generation, paused, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, ?, 1, 2, 1, ?, ?)",
                (ids["person_b"], ids["binding_b"], ids["presence_qq"], now, now),
            ),
            (
                "INSERT INTO person_active_routes("
                "person_id, identity_binding_id, presence_id, route_generation, paused, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, ?, 1, 0, 0, ?, ?)",
                (ids["person_b"], ids["binding_b"], ids["presence_qq"], now, now),
            ),
        )
        for sql, params in route_insert_negatives:
            connection.execute("BEGIN")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, params)
            connection.rollback()

        route_update_negatives = (
            (
                "UPDATE person_active_routes SET identity_binding_id=? WHERE person_id=?",
                (ids["binding_b"], ids["person_a"]),
            ),
            (
                "UPDATE person_active_routes SET presence_id=? WHERE person_id=?",
                (ids["presence_telegram"], ids["person_a"]),
            ),
            (
                "UPDATE space_binding_ingest_routes SET ingest_presence_id=? "
                "WHERE space_binding_id=?",
                (ids["presence_telegram"], ids["space_binding_a"]),
            ),
            (
                "UPDATE space_active_routes SET space_binding_id=? WHERE space_id=?",
                (ids["space_binding_b"], ids["space_a"]),
            ),
            (
                "UPDATE space_active_routes SET presence_id=? WHERE space_id=?",
                (ids["presence_telegram"], ids["space_a"]),
            ),
        )
        for sql, params in route_update_negatives:
            connection.execute("BEGIN")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, params)
            connection.rollback()

        principal_a = str(uuid4())
        principal_b = str(uuid4())
        request_id = str(uuid4())
        audit_id = _insert_audit(connection, now)
        connection.execute(
            "INSERT INTO control_command_receipts("
            "principal_id, request_id, payload_hash, status, result_resource_id, "
            "effective_state_json, result_revision, audit_id, created_at, updated_at"
            ") VALUES (?, ?, ?, 'succeeded', 'conversation.rollup.enabled', ?, 1, ?, ?, ?)",
            (principal_a, request_id, _VALID_HASH, _VALID_STATE, audit_id, now, now),
        )
        assert connection.execute(
            "SELECT result_resource_id, effective_state_json, result_revision, "
            "problem_code, audit_id FROM control_command_receipts "
            "WHERE principal_id=? AND request_id=?",
            (principal_a, request_id),
        ).fetchone() == (
            "conversation.rollup.enabled",
            _VALID_STATE,
            1,
            None,
            audit_id,
        )
        connection.execute(
            "INSERT INTO control_command_receipts("
            "principal_id, request_id, payload_hash, status, problem_code, "
            "created_at, updated_at"
            ") VALUES (?, ?, ?, 'failed', 'validation_error', ?, ?)",
            (principal_b, request_id, _VALID_HASH, now, now),
        )
        connection.commit()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, effective_state_json, "
                "audit_id, created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?)",
                (principal_a, request_id, "b" * 64, _VALID_STATE, audit_id, now, now),
            )
        connection.rollback()

        receipt_negatives = (
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, effective_state_json, "
                "audit_id, created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?)",
                (str(uuid4()).upper(), str(uuid4()), _VALID_HASH, _VALID_STATE, audit_id, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, effective_state_json, "
                "audit_id, created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?)",
                (str(uuid4()), str(uuid4()), "A" * 64, _VALID_STATE, audit_id, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, effective_state_json, "
                "audit_id, created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?)",
                (str(uuid4()), str(uuid4()), "z" * 64, _VALID_STATE, audit_id, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, created_at, updated_at"
                ") VALUES (?, ?, ?, 'pending', ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, problem_code, "
                "effective_state_json, audit_id, created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', 'validation_error', ?, ?, ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, _VALID_STATE, audit_id, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, created_at, updated_at"
                ") VALUES (?, ?, ?, 'failed', ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, effective_state_json, "
                "result_revision, audit_id, created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', ?, 0, ?, ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, _VALID_STATE, audit_id, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, effective_state_json, "
                "audit_id, operation_kind, created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', ?, ?, 'backfill', ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, _VALID_STATE, audit_id, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, effective_state_json, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', ?, ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, _VALID_STATE, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, audit_id, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', ?, ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, audit_id, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, problem_code, "
                "effective_state_json, created_at, updated_at"
                ") VALUES (?, ?, ?, 'failed', 'validation_error', ?, ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, _VALID_STATE, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, result_resource_id, "
                "problem_code, created_at, updated_at"
                ") VALUES (?, ?, ?, 'failed', 'conversation.rollup.enabled', "
                "'validation_error', ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, result_resource_id, "
                "effective_state_json, audit_id, created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', '', ?, ?, ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, _VALID_STATE, audit_id, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, result_resource_id, "
                "effective_state_json, audit_id, created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', ' padded-key', ?, ?, ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, _VALID_STATE, audit_id, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, result_resource_id, "
                "effective_state_json, audit_id, created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    str(uuid4()),
                    _VALID_HASH,
                    "x" * 256,
                    _VALID_STATE,
                    audit_id,
                    now,
                    now,
                ),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, effective_state_json, "
                "audit_id, created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', '{', ?, ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, audit_id, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, effective_state_json, "
                "audit_id, created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', '[]', ?, ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, audit_id, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, effective_state_json, "
                "audit_id, created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    str(uuid4()),
                    _VALID_HASH,
                    '{"k":"' + ("x" * 4090) + '"}',
                    audit_id,
                    now,
                    now,
                ),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, effective_state_json, "
                "audit_id, created_at, updated_at"
                ") VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, ' {"enabled":true}', audit_id, now, now),
            ),
            (
                "INSERT INTO control_command_receipts("
                "principal_id, request_id, payload_hash, status, result_revision, "
                "problem_code, created_at, updated_at"
                ") VALUES (?, ?, ?, 'failed', 1, 'validation_error', ?, ?)",
                (str(uuid4()), str(uuid4()), _VALID_HASH, now, now),
            ),
        )
        for sql, params in receipt_negatives:
            connection.execute("BEGIN")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, params)
            connection.rollback()


def test_control_command_receipts_are_secret_free(c3_db: Path) -> None:
    with _connect(c3_db) as connection:
        columns = [
            str(row[1]).casefold()
            for row in connection.execute("PRAGMA table_info(control_command_receipts)")
        ]
        assert "payload_hash" in columns
        assert "effective_state_json" in columns
        assert "payload" not in columns
        for column in columns:
            assert not any(token in column for token in _SECRET_COLUMN_TOKENS)
        schema_sql = "\n".join(
            str(row[0] or "")
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='control_command_receipts'"
            )
        ).casefold()
        assert "payload_hash" in schema_sql
        assert "effective_state_json" in schema_sql
        for token in _SECRET_COLUMN_TOKENS:
            assert token not in schema_sql


def test_parent_updates_cannot_desynchronize_existing_routes(c3_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c3_db) as connection:
        ids = _seed_identity(connection, now)
        connection.execute(
            "INSERT INTO person_active_routes("
            "person_id, identity_binding_id, presence_id, route_generation, paused, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, ?, 1, 0, 1, ?, ?)",
            (ids["person_a"], ids["binding_a"], ids["presence_qq"], now, now),
        )
        connection.execute(
            "INSERT INTO space_binding_ingest_routes("
            "space_binding_id, ingest_presence_id, route_generation, paused, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, 1, 0, 1, ?, ?)",
            (ids["space_binding_a"], ids["presence_qq"], now, now),
        )
        connection.execute(
            "INSERT INTO space_active_routes("
            "space_id, space_binding_id, presence_id, route_generation, paused, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, ?, 1, 0, 1, ?, ?)",
            (ids["space_a"], ids["space_binding_a"], ids["presence_qq"], now, now),
        )
        connection.commit()
        before = _parent_and_route_snapshot(connection)
        parent_update_negatives = (
            (
                "UPDATE identity_bindings SET platform='telegram' WHERE id=?",
                (ids["binding_a"],),
            ),
            (
                "UPDATE identity_bindings SET person_id=? WHERE id=?",
                (ids["person_b"], ids["binding_a"]),
            ),
            (
                "UPDATE space_bindings SET platform='telegram' WHERE id=?",
                (ids["space_binding_a"],),
            ),
            (
                "UPDATE space_bindings SET space_id=? WHERE id=?",
                (ids["space_b"], ids["space_binding_a"]),
            ),
            (
                "UPDATE presences SET platform='telegram' WHERE id=?",
                (ids["presence_qq"],),
            ),
        )
        for sql, params in parent_update_negatives:
            connection.execute("BEGIN")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, params)
            assert _parent_and_route_snapshot(connection) == before
            connection.rollback()
            assert _parent_and_route_snapshot(connection) == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        connection.execute(
            "UPDATE identity_bindings SET display_name='renamed', status='disabled', "
            "revision=2 WHERE id=?",
            (ids["binding_a"],),
        )
        connection.execute(
            "UPDATE space_bindings SET display_name='renamed', status='disabled', "
            "revision=2 WHERE id=?",
            (ids["space_binding_a"],),
        )
        connection.execute(
            "UPDATE presences SET enabled=0, ingest_eligible=0, revision=2 WHERE id=?",
            (ids["presence_qq"],),
        )
        connection.commit()
        after = _parent_and_route_snapshot(connection)
        assert after[3:] == before[3:]
        assert after[0] != before[0]
        assert after[1] != before[1]
        assert after[2] != before[2]
        assert connection.execute(
            "SELECT person_id, platform FROM identity_bindings WHERE id=?",
            (ids["binding_a"],),
        ).fetchone() == (ids["person_a"], "qq")
        assert connection.execute(
            "SELECT space_id, platform FROM space_bindings WHERE id=?",
            (ids["space_binding_a"],),
        ).fetchone() == (ids["space_a"], "qq")
        assert connection.execute(
            "SELECT platform FROM presences WHERE id=?",
            (ids["presence_qq"],),
        ).fetchone() == ("qq",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_control_receipt_snapshot_replay_and_opaque_resource_id(c3_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c3_db) as connection:
        audit_id = _insert_audit(connection, now)
        principal_id = str(uuid4())
        request_id = str(uuid4())
        connection.execute(
            "INSERT INTO control_command_receipts("
            "principal_id, request_id, payload_hash, status, result_resource_id, "
            "effective_state_json, result_revision, audit_id, created_at, updated_at"
            ") VALUES (?, ?, ?, 'succeeded', 'conversation.rollup.enabled', ?, 1, ?, ?, ?)",
            (principal_id, request_id, _VALID_HASH, _VALID_STATE, audit_id, now, now),
        )
        connection.commit()
        assert connection.execute(
            "SELECT status, result_resource_id, effective_state_json, result_revision, "
            "problem_code, audit_id FROM control_command_receipts "
            "WHERE principal_id=? AND request_id=?",
            (principal_id, request_id),
        ).fetchone() == (
            "succeeded",
            "conversation.rollup.enabled",
            _VALID_STATE,
            1,
            None,
            audit_id,
        )

        uuid_resource = str(uuid4())
        connection.execute(
            "INSERT INTO control_command_receipts("
            "principal_id, request_id, payload_hash, status, result_resource_id, "
            "effective_state_json, result_revision, audit_id, created_at, updated_at"
            ") VALUES (?, ?, ?, 'succeeded', ?, ?, 2, ?, ?, ?)",
            (
                str(uuid4()),
                str(uuid4()),
                _VALID_HASH,
                uuid_resource,
                _VALID_STATE,
                audit_id,
                now,
                now,
            ),
        )
        failed_principal = str(uuid4())
        failed_request = str(uuid4())
        connection.execute(
            "INSERT INTO control_command_receipts("
            "principal_id, request_id, payload_hash, status, problem_code, "
            "created_at, updated_at"
            ") VALUES (?, ?, ?, 'failed', 'conflict', ?, ?)",
            (failed_principal, failed_request, _VALID_HASH, now, now),
        )
        connection.commit()
        assert connection.execute(
            "SELECT status, result_resource_id, effective_state_json, result_revision, "
            "problem_code, audit_id FROM control_command_receipts "
            "WHERE principal_id=? AND request_id=?",
            (failed_principal, failed_request),
        ).fetchone() == ("failed", None, None, None, "conflict", None)
