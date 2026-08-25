"""Canonical event shadows, OneBot receipts, and 0045 cutover-descendant proofs."""

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
from tests.unit.test_migration_0044 import _connect, _insert_conversation, _seed_identity

from qq_ai_bot.conversation.canonical_db_models import (
    CANONICAL_CONVERSATION_CREATE_ORDER,
    CANONICAL_EVENT_TABLES,
    CanonicalEventReceiptModel,
    _install_c4_triggers_if_ready,
)
from qq_ai_bot.conversation.canonical_event_schema import (
    C4_TRIGGER_NAMES,
    C4_TRIGGER_SQL,
    CHAT_EVENT_CANONICAL_SHADOW_COLUMNS,
    CHAT_EVENT_CANONICAL_SHADOW_INDEXES,
    CONVERSATION_SCOPE_CANONICAL_SHADOW_COLUMNS,
    CONVERSATION_SCOPE_CANONICAL_SHADOW_INDEXES,
)
from qq_ai_bot.conversation.canonical_schema import C3_TRIGGER_NAMES
from qq_ai_bot.identity.db_models import CANONICAL_IDENTITY_CREATE_ORDER
from qq_ai_bot.persistence.metadata import Base
from qq_ai_bot.persistence.models import ChatEventModel

_MIGRATION_PATH = Path("migrations/versions/0045_canonical_event_shadows.py")
_FORBIDDEN_TABLES = {
    "yuki",
    "yukis",
    "yuki_self",
    "yukiself",
    "gateway_connections",
    "identity_cutover_manifests",
    "identity_cutover_runs",
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
    "content",
    "segment",
    "payload",
)
_VALID_HASH = "a" * 64
_OLD_CHAT_EVENT_COLUMNS = (
    "id",
    "bot_user_id",
    "platform_message_id",
    "scope_type",
    "group_id",
    "private_peer_user_id",
    "sender_user_id",
    "sender_nickname",
    "sender_group_card",
    "direction",
    "event_kind",
    "source_plugin_id",
    "external_source",
    "external_event_key",
    "external_event_type",
    "external_payload_json",
    "external_target_id",
    "content",
    "visual_summary",
    "segments_json",
    "reply_to_message_id",
    "origin",
    "automation_id",
    "automation_run_id",
    "occurred_at",
    "observed_at",
)
_OLD_SCOPE_COLUMNS = (
    "id",
    "scope_key",
    "bot_user_id",
    "scope_type",
    "private_peer_user_id",
    "group_id",
    "generation",
    "starts_after_event_id",
    "last_event_id",
    "last_generation_change_event_id",
    "uncovered_event_count",
    "uncovered_character_count",
    "created_at",
    "updated_at",
)
_KNOWN_WRITERS = (
    Path("src/qq_ai_bot/persistence/scoped_event_uow.py"),
    Path("src/qq_ai_bot/conversation/rollup/repository.py"),
    Path("src/qq_ai_bot/memory/quality/performance.py"),
)


def _enable_sqlite_fk(dbapi_connection: object, _record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _create_orm_c4_schema(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    event.listen(engine, "connect", _enable_sqlite_fk)
    tables = [
        Base.metadata.tables[name]
        for name in (
            "people",
            "groups",
            *CANONICAL_IDENTITY_CREATE_ORDER,
            *CANONICAL_CONVERSATION_CREATE_ORDER,
            "chat_events",
            "conversation_scopes",
            "automations",
            "automation_runs",
            *CANONICAL_EVENT_TABLES,
        )
    ]
    Base.metadata.create_all(engine, tables=tables)
    with engine.begin() as connection:
        _install_c4_triggers_if_ready(connection)
    engine.dispose()


def _downgrade(path: Path, monkeypatch: pytest.MonkeyPatch, revision: str) -> None:
    _alembic_downgrade(path, monkeypatch, revision)


def _column_names(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]


def _normalized_c4_schema(path: Path) -> dict[str, Any]:
    full = _normalized_schema(path)
    tables = [name for name in full["tables"] if name in CANONICAL_EVENT_TABLES]
    return {
        "tables": tables,
        "columns": {name: full["columns"][name] for name in tables},
        "indexes": {name: full["indexes"][name] for name in tables},
        "foreign_keys": {name: full["foreign_keys"][name] for name in tables},
        "constraints": {name: full["constraints"][name] for name in tables},
        "triggers": {
            name: sql for name, sql in full["triggers"].items() if name in C4_TRIGGER_NAMES
        },
        "chat_event_shadows": [
            column
            for column in full["columns"]["chat_events"]
            if column[0] in CHAT_EVENT_CANONICAL_SHADOW_COLUMNS
        ],
        "scope_shadows": [
            column
            for column in full["columns"]["conversation_scopes"]
            if column[0] in CONVERSATION_SCOPE_CANONICAL_SHADOW_COLUMNS
        ],
        "chat_event_foreign_keys": full["foreign_keys"]["chat_events"],
        "scope_foreign_keys": full["foreign_keys"]["conversation_scopes"],
        "chat_event_c4_indexes": [
            item
            for item in full["indexes"]["chat_events"]
            if item[0] in CHAT_EVENT_CANONICAL_SHADOW_INDEXES
        ],
        "scope_c4_indexes": [
            item
            for item in full["indexes"]["conversation_scopes"]
            if item[0] in CONVERSATION_SCOPE_CANONICAL_SHADOW_INDEXES
        ],
    }


_EXPECTED_CHAT_EVENT_SHADOW_FKS = (
    (
        "canonical_conversations",
        "canonical_conversation_id",
        "id",
        "RESTRICT",
        "RESTRICT",
        "NONE",
    ),
    ("persons", "author_person_id", "id", "RESTRICT", "RESTRICT", "NONE"),
    ("presences", "author_presence_id", "id", "RESTRICT", "RESTRICT", "NONE"),
    ("presences", "ingress_presence_id", "id", "RESTRICT", "RESTRICT", "NONE"),
)
_EXPECTED_SCOPE_SHADOW_FKS = (
    (
        "canonical_conversations",
        "canonical_conversation_id",
        "id",
        "RESTRICT",
        "RESTRICT",
        "NONE",
    ),
)


def _foreign_keys(connection: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    return sorted(
        (row[2], row[3], row[4], row[5], row[6], row[7])
        for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
    )


def _index_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA index_list("{table}")')}


def _insert_shadow_message(
    connection: sqlite3.Connection,
    *,
    platform_message_id: str,
    now: str,
    **shadows: object,
) -> None:
    columns = [
        "bot_user_id",
        "platform_message_id",
        "scope_type",
        "private_peer_user_id",
        "sender_user_id",
        "direction",
        "content",
        "visual_summary",
        "segments_json",
        "origin",
        "occurred_at",
        "observed_at",
    ]
    values: list[object] = [
        "bot-1",
        platform_message_id,
        "private",
        "peer-1",
        "sender-1",
        "inbound",
        "hello",
        "",
        "[]",
        "user_message",
        now,
        now,
    ]
    for name, value in shadows.items():
        columns.append(name)
        values.append(value)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO chat_events({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )


def _insert_isolated_person(connection: sqlite3.Connection, person_id: str, now: str) -> None:
    connection.execute(
        "INSERT INTO persons(id, enabled, revision, created_at, updated_at) VALUES (?, 1, 1, ?, ?)",
        (person_id, now, now),
    )


def _insert_isolated_presence(
    connection: sqlite3.Connection,
    presence_id: str,
    now: str,
    *,
    account: str,
) -> None:
    connection.execute(
        "INSERT INTO presences("
        "id, platform, external_account_id, enabled, ingest_eligible, "
        "revision, created_at, updated_at"
        ") VALUES (?, 'qq', ?, 1, 1, 1, ?, ?)",
        (presence_id, account, now, now),
    )


def _seed_legacy_people(connection: sqlite3.Connection, now: str) -> None:
    for user_id, is_bot in (("bot-1", 1), ("peer-1", 0), ("peer-2", 0), ("sender-1", 0)):
        connection.execute(
            "INSERT INTO people(user_id, nickname, enabled, is_bot, first_seen_at, last_seen_at) "
            "VALUES (?, '', 1, ?, ?, ?)",
            (user_id, is_bot, now, now),
        )


def _insert_legacy_message(
    connection: sqlite3.Connection,
    *,
    platform_message_id: str,
    now: str,
    origin: str = "user_message",
) -> None:
    connection.execute(
        "INSERT INTO chat_events("
        "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
        "sender_user_id, direction, content, visual_summary, segments_json, "
        "origin, occurred_at, observed_at"
        ") VALUES ('bot-1', ?, 'private', 'peer-1', 'sender-1', 'inbound', "
        "'hello', '', '[]', ?, ?, ?)",
        (platform_message_id, origin, now, now),
    )


def _insert_legacy_scope(
    connection: sqlite3.Connection,
    *,
    scope_key: str,
    now: str,
    private_peer_user_id: str = "peer-1",
) -> None:
    connection.execute(
        "INSERT INTO conversation_scopes("
        "scope_key, bot_user_id, scope_type, private_peer_user_id, group_id, "
        "generation, starts_after_event_id, last_event_id, "
        "last_generation_change_event_id, uncovered_event_count, "
        "uncovered_character_count, created_at, updated_at"
        ") VALUES (?, 'bot-1', 'private', ?, NULL, 1, 0, 0, 0, 0, 0, ?, ?)",
        (scope_key, private_peer_user_id, now, now),
    )


def _select_old_event_rows(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    columns = ", ".join(_OLD_CHAT_EVENT_COLUMNS)
    return list(
        connection.execute(
            f"SELECT {columns} FROM chat_events ORDER BY platform_message_id"
        ).fetchall()
    )


def _select_old_scope_rows(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    columns = ", ".join(_OLD_SCOPE_COLUMNS)
    return list(
        connection.execute(
            f"SELECT {columns} FROM conversation_scopes ORDER BY scope_key"
        ).fetchall()
    )


def _shadow_values(connection: sqlite3.Connection, platform_message_id: str) -> tuple[object, ...]:
    columns = ", ".join(CHAT_EVENT_CANONICAL_SHADOW_COLUMNS)
    row = connection.execute(
        f"SELECT {columns} FROM chat_events WHERE platform_message_id=?",
        (platform_message_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


@pytest.fixture(params=["alembic", "orm"])
def c4_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    path = tmp_path / f"{request.param}.db"
    if request.param == "alembic":
        _upgrade(path, monkeypatch, "head")
    else:
        _create_orm_c4_schema(path)
    return path


def test_0005_does_not_create_c4_shadows_or_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "at-0005.db"
    _upgrade(path, monkeypatch, "0005")
    with sqlite3.connect(path) as connection:
        assert "canonical_event_receipts" not in _tables(connection)
        columns = set(_column_names(connection, "chat_events"))
        assert not (set(CHAT_EVENT_CANONICAL_SHADOW_COLUMNS) & columns)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN "
                f"({', '.join('?' for _ in C4_TRIGGER_NAMES)})",
                C4_TRIGGER_NAMES,
            ).fetchall()
            == []
        )


def test_0042_does_not_create_c4_shadows_or_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "at-0042.db"
    _upgrade(path, monkeypatch, "0042")
    with sqlite3.connect(path) as connection:
        assert "canonical_event_receipts" not in _tables(connection)
        assert not (
            set(CHAT_EVENT_CANONICAL_SHADOW_COLUMNS) & set(_column_names(connection, "chat_events"))
        )
        assert not (
            set(CONVERSATION_SCOPE_CANONICAL_SHADOW_COLUMNS)
            & set(_column_names(connection, "conversation_scopes"))
        )


def test_0044_does_not_create_c4_shadows_or_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "at-0044.db"
    _upgrade(path, monkeypatch, "0044")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0044",)
        assert "canonical_event_receipts" not in _tables(connection)
        assert not (
            set(CHAT_EVENT_CANONICAL_SHADOW_COLUMNS) & set(_column_names(connection, "chat_events"))
        )
        assert not (
            set(CONVERSATION_SCOPE_CANONICAL_SHADOW_COLUMNS)
            & set(_column_names(connection, "conversation_scopes"))
        )
        trigger_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert not (set(C4_TRIGGER_NAMES) & trigger_names)
        assert set(C3_TRIGGER_NAMES) <= trigger_names
        assert "uq_chat_events_canonical_event_keeper" not in _index_names(
            connection, "chat_events"
        )


def test_fresh_upgrade_head_creates_c4_shadows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fresh-head.db"
    _upgrade(path, monkeypatch, "head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0046",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = _tables(connection)
        assert set(CANONICAL_EVENT_TABLES) <= tables
        assert not (_FORBIDDEN_TABLES & tables)
        columns = set(_column_names(connection, "chat_events"))
        assert set(CHAT_EVENT_CANONICAL_SHADOW_COLUMNS) <= columns
        assert set(_OLD_CHAT_EVENT_COLUMNS) <= columns
        assert "canonical_conversation_id" in _column_names(connection, "conversation_scopes")
        trigger_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert set(C4_TRIGGER_NAMES) <= trigger_names
        assert set(_EXPECTED_CHAT_EVENT_SHADOW_FKS) <= set(_foreign_keys(connection, "chat_events"))
        assert set(_EXPECTED_SCOPE_SHADOW_FKS) <= set(
            _foreign_keys(connection, "conversation_scopes")
        )
        assert "uq_chat_events_canonical_event_keeper" in _index_names(connection, "chat_events")


def test_empty_fresh_and_0044_to_0045_schemas_are_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = tmp_path / "fresh.db"
    upgraded = tmp_path / "from-0044.db"
    _upgrade(fresh, monkeypatch, "0045")
    _upgrade(upgraded, monkeypatch, "0044")
    before = _schema_dump(upgraded)
    _upgrade(upgraded, monkeypatch, "0045")
    assert _normalized_schema(fresh) == _normalized_schema(upgraded)
    after = _schema_dump(upgraded)
    preserved = {"alembic_version", "chat_events", "conversation_scopes"}
    assert all(after[key] == sql for key, sql in before.items() if key[1] not in preserved)


def test_populated_downgrade_0045_to_0044_preserves_legacy_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / "expected-0044.db"
    path = tmp_path / "populated-downgrade.db"
    _upgrade(expected, monkeypatch, "0044")
    _upgrade(path, monkeypatch, "0045")
    now = "2026-08-24T00:00:00+00:00"
    with _connect(path) as connection:
        ids = _seed_identity(connection, now)
        _seed_legacy_people(connection, now)
        connection.commit()
        conversation_id = str(uuid4())
        _insert_conversation(
            connection,
            conversation_id=conversation_id,
            alias_id=str(uuid4()),
            kind="private",
            owner_id=ids["person_a"],
            now=now,
        )
        logical_id = str(uuid4())
        _insert_shadow_message(
            connection,
            platform_message_id="legacy-1",
            now=now,
            canonical_event_id=logical_id,
            canonical_conversation_id=conversation_id,
            author_kind="yuki",
            author_presence_id=ids["presence_qq"],
            utterance_fingerprint=_VALID_HASH,
            suppression_status="keeper",
        )
        _insert_shadow_message(
            connection,
            platform_message_id="legacy-2",
            now=now,
            canonical_event_id=logical_id,
            utterance_fingerprint=_VALID_HASH,
            suppression_status="duplicate",
        )
        _insert_legacy_scope(connection, scope_key="private:bot-1:peer-1", now=now)
        _insert_legacy_scope(
            connection,
            scope_key="private:bot-1:peer-2",
            now=now,
            private_peer_user_id="peer-2",
        )
        connection.execute(
            "UPDATE conversation_scopes SET canonical_conversation_id=? "
            "WHERE scope_key='private:bot-1:peer-1'",
            (conversation_id,),
        )
        connection.execute("UPDATE people SET nickname='kept' WHERE user_id='peer-1'")
        connection.commit()
        events_before = _select_old_event_rows(connection)
        scopes_before = _select_old_scope_rows(connection)
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    _downgrade(path, monkeypatch, "0044")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0044",)
        assert "canonical_event_receipts" not in _tables(connection)
        assert not (
            set(CHAT_EVENT_CANONICAL_SHADOW_COLUMNS) & set(_column_names(connection, "chat_events"))
        )
        assert "canonical_conversation_id" not in _column_names(connection, "conversation_scopes")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _select_old_event_rows(connection) == events_before
        assert _select_old_scope_rows(connection) == scopes_before
    assert _normalized_schema(path) == _normalized_schema(expected)


def test_orm_metadata_matches_0045_c4_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrated = tmp_path / "migrated.db"
    orm = tmp_path / "orm.db"
    _upgrade(migrated, monkeypatch, "head")
    _create_orm_c4_schema(orm)
    assert set(CANONICAL_EVENT_TABLES) <= set(Base.metadata.tables)
    assert _normalized_c4_schema(migrated) == _normalized_c4_schema(orm)
    assert [column.key for column in ChatEventModel.__table__.columns][-10:] == list(
        CHAT_EVENT_CANONICAL_SHADOW_COLUMNS
    )


def test_alembic_heads_is_exactly_0045() -> None:
    config = Config("alembic.ini")
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["0046"]


def test_0045_is_self_contained_alembic() -> None:
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
    assert "not enforced" not in source
    assert "REFERENCES canonical_conversations(id)" in source
    assert "REFERENCES persons(id)" in source
    assert "REFERENCES presences(id)" in source
    assert "ON UPDATE RESTRICT ON DELETE RESTRICT" in source
    loaded = SourceFileLoader("revision_0045", str(_MIGRATION_PATH)).load_module()
    assert loaded._C4_TRIGGER_SQL == C4_TRIGGER_SQL
    assert loaded._C4_TRIGGER_NAMES == C4_TRIGGER_NAMES
    assert loaded._CHAT_EVENT_SHADOW_INDEXES == CHAT_EVENT_CANONICAL_SHADOW_INDEXES


def test_0005_excludes_receipts_and_lists_shadow_columns() -> None:
    source = Path("migrations/versions/0005_person_centric_v1.py").read_text(encoding="utf-8")
    assert '"canonical_event_receipts"' in source
    assert '"uq_chat_events_canonical_event_keeper"' in source
    for column in CHAT_EVENT_CANONICAL_SHADOW_COLUMNS:
        assert f'"{column}"' in source


def test_legacy_inserts_leave_shadows_null(c4_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c4_db) as connection:
        _seed_legacy_people(connection, now)
        _insert_legacy_message(connection, platform_message_id="old-shape", now=now)
        _insert_legacy_scope(connection, scope_key="private:bot-1:peer-1", now=now)
        connection.commit()
        assert _shadow_values(connection, "old-shape") == (None,) * len(
            CHAT_EVENT_CANONICAL_SHADOW_COLUMNS
        )
        assert connection.execute(
            "SELECT canonical_conversation_id FROM conversation_scopes WHERE scope_key=?",
            ("private:bot-1:peer-1",),
        ).fetchone() == (None,)


def test_old_uniques_and_kind_payload_still_hold(c4_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c4_db) as connection:
        _seed_legacy_people(connection, now)
        _insert_legacy_message(connection, platform_message_id="dup-key", now=now)
        connection.commit()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_legacy_message(connection, platform_message_id="dup-key", now=now)
        connection.rollback()
        connection.execute(
            "INSERT INTO chat_events("
            "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
            "sender_user_id, direction, event_kind, source_plugin_id, external_source, "
            "external_event_key, external_event_type, external_payload_json, "
            "external_target_id, content, visual_summary, segments_json, origin, "
            "occurred_at, observed_at"
            ") VALUES ('bot-1', 'ext-1', 'private', 'peer-1', 'sender-1', 'external', "
            "'external_event', 'demo.plugin', 'demo', 'ext-key', 'notice', '{}', "
            "'peer-1', '', '', '[]', 'plugin_background', ?, ?)",
            (now, now),
        )
        connection.commit()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO chat_events("
                "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
                "sender_user_id, direction, event_kind, source_plugin_id, external_source, "
                "external_event_key, external_event_type, external_payload_json, "
                "external_target_id, content, visual_summary, segments_json, origin, "
                "occurred_at, observed_at"
                ") VALUES ('bot-1', 'ext-2', 'private', 'peer-1', 'sender-1', 'external', "
                "'external_event', 'demo.plugin', 'demo', 'ext-key', 'notice', '{}', "
                "'peer-1', '', '', '[]', 'plugin_background', ?, ?)",
                (now, now),
            )
        connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO chat_events("
                "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
                "sender_user_id, direction, event_kind, source_plugin_id, content, "
                "visual_summary, segments_json, origin, occurred_at, observed_at"
                ") VALUES ('bot-1', 'bad-kind', 'private', 'peer-1', 'sender-1', "
                "'inbound', 'external_event', 'demo.plugin', 'x', '', '[]', "
                "'user_message', ?, ?)",
                (now, now),
            )
        connection.rollback()


def test_author_kind_contract_and_origin_independence(c4_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c4_db) as connection:
        ids = _seed_identity(connection, now)
        _seed_legacy_people(connection, now)
        connection.commit()
        conversation_id = str(uuid4())
        _insert_conversation(
            connection,
            conversation_id=conversation_id,
            alias_id=str(uuid4()),
            kind="private",
            owner_id=ids["person_a"],
            now=now,
        )
        accepted = (
            ("person", ids["person_a"], None),
            ("yuki", None, ids["presence_qq"]),
            ("external_bot", None, None),
            ("system", None, None),
        )
        for index, (kind, person_id, presence_id) in enumerate(accepted):
            connection.execute(
                "INSERT INTO chat_events("
                "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
                "sender_user_id, direction, content, visual_summary, segments_json, "
                "origin, author_kind, author_person_id, author_presence_id, "
                "occurred_at, observed_at"
                ") VALUES ('bot-1', ?, 'private', 'peer-1', 'sender-1', 'inbound', "
                "'hello', '', '[]', 'user_message', ?, ?, ?, ?, ?)",
                (f"author-{index}", kind, person_id, presence_id, now, now),
            )
        connection.execute(
            "INSERT INTO chat_events("
            "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
            "sender_user_id, direction, event_kind, source_plugin_id, external_source, "
            "external_event_key, external_event_type, external_payload_json, "
            "external_target_id, content, visual_summary, segments_json, origin, "
            "author_kind, author_person_id, occurred_at, observed_at"
            ") VALUES ('bot-1', 'plugin-person', 'private', 'peer-1', 'sender-1', "
            "'external', 'external_event', 'demo.plugin', 'demo', 'ext-2', 'notice', "
            "'{}', 'peer-1', '', '', '[]', 'plugin_background', 'person', ?, ?, ?)",
            (ids["person_a"], now, now),
        )
        connection.commit()
        rejected_kinds = ("plugin", "automation", "command")
        for kind in rejected_kinds:
            connection.execute("BEGIN")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO chat_events("
                    "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
                    "sender_user_id, direction, content, visual_summary, segments_json, "
                    "origin, author_kind, occurred_at, observed_at"
                    ") VALUES ('bot-1', ?, 'private', 'peer-1', 'sender-1', 'inbound', "
                    "'hello', '', '[]', 'user_message', ?, ?, ?)",
                    (f"bad-{kind}", kind, now, now),
                )
            connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO chat_events("
                "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
                "sender_user_id, direction, content, visual_summary, segments_json, "
                "origin, author_kind, occurred_at, observed_at"
                ") VALUES ('bot-1', 'person-missing', 'private', 'peer-1', 'sender-1', "
                "'inbound', 'hello', '', '[]', 'user_message', 'person', ?, ?)",
                (now, now),
            )
        connection.rollback()


def test_duplicate_canonical_event_mapping_and_suppression(c4_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    logical_id = str(uuid4())
    other_id = str(uuid4())
    with _connect(c4_db) as connection:
        _seed_legacy_people(connection, now)
        _insert_shadow_message(
            connection,
            platform_message_id="keeper-row",
            now=now,
            canonical_event_id=logical_id,
            utterance_fingerprint=_VALID_HASH,
            suppression_status="keeper",
        )
        _insert_shadow_message(
            connection,
            platform_message_id="duplicate-row",
            now=now,
            canonical_event_id=logical_id,
            utterance_fingerprint=_VALID_HASH,
            suppression_status="duplicate",
        )
        _insert_shadow_message(
            connection,
            platform_message_id="duplicate-row-2",
            now=now,
            canonical_event_id=logical_id,
            utterance_fingerprint=_VALID_HASH,
            suppression_status="duplicate",
        )
        _insert_shadow_message(
            connection,
            platform_message_id="other-keeper",
            now=now,
            canonical_event_id=other_id,
            utterance_fingerprint="b" * 64,
            suppression_status="keeper",
        )
        connection.commit()
        rows = connection.execute(
            "SELECT platform_message_id, canonical_event_id, suppression_status "
            "FROM chat_events WHERE canonical_event_id=? ORDER BY platform_message_id",
            (logical_id,),
        ).fetchall()
        assert rows == [
            ("duplicate-row", logical_id, "duplicate"),
            ("duplicate-row-2", logical_id, "duplicate"),
            ("keeper-row", logical_id, "keeper"),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_events "
            "WHERE canonical_event_id=? AND suppression_status='keeper'",
            (logical_id,),
        ).fetchone() == (1,)
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_shadow_message(
                connection,
                platform_message_id="second-keeper",
                now=now,
                canonical_event_id=logical_id,
                utterance_fingerprint=_VALID_HASH,
                suppression_status="keeper",
            )
        connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_shadow_message(
                connection,
                platform_message_id="orphan-suppression",
                now=now,
                suppression_status="keeper",
            )
        connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_shadow_message(
                connection,
                platform_message_id="duplicate-without-fingerprint",
                now=now,
                canonical_event_id=logical_id,
                suppression_status="duplicate",
            )
        connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_shadow_message(
                connection,
                platform_message_id="uuid1",
                now=now,
                canonical_event_id=str(uuid1()),
            )
        connection.rollback()
        connection.execute(
            "INSERT INTO chat_events("
            "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
            "sender_user_id, direction, event_kind, source_plugin_id, external_source, "
            "external_event_key, external_event_type, external_payload_json, "
            "external_target_id, content, visual_summary, segments_json, origin, "
            "occurred_at, observed_at"
            ") VALUES ('bot-1', 'plugin-no-fingerprint', 'private', 'peer-1', "
            "'sender-1', 'external', 'external_event', 'demo.plugin', 'demo', "
            "'ext-fingerprint', 'notice', '{}', 'peer-1', '', '', '[]', "
            "'plugin_background', ?, ?)",
            (now, now),
        )
        connection.commit()
        assert connection.execute(
            "SELECT utterance_fingerprint, suppression_status FROM chat_events "
            "WHERE platform_message_id='plugin-no-fingerprint'"
        ).fetchone() == (None, None)


def test_canonical_shadow_foreign_keys_reject_dangling_and_parent_mutation(
    c4_db: Path,
) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c4_db) as connection:
        assert set(_EXPECTED_CHAT_EVENT_SHADOW_FKS) <= set(_foreign_keys(connection, "chat_events"))
        assert set(_EXPECTED_SCOPE_SHADOW_FKS) <= set(
            _foreign_keys(connection, "conversation_scopes")
        )
        ids = _seed_identity(connection, now)
        _seed_legacy_people(connection, now)
        connection.commit()
        isolated_person = str(uuid4())
        isolated_presence = str(uuid4())
        _insert_isolated_person(connection, isolated_person, now)
        _insert_isolated_presence(connection, isolated_presence, now, account="8100")
        connection.commit()
        conversation_id = str(uuid4())
        _insert_conversation(
            connection,
            conversation_id=conversation_id,
            alias_id=str(uuid4()),
            kind="private",
            owner_id=ids["person_a"],
            now=now,
        )
        _insert_shadow_message(
            connection,
            platform_message_id="author-person",
            now=now,
            author_kind="person",
            author_person_id=isolated_person,
        )
        _insert_shadow_message(
            connection,
            platform_message_id="author-yuki",
            now=now,
            author_kind="yuki",
            author_presence_id=isolated_presence,
            ingress_presence_id=isolated_presence,
            canonical_conversation_id=conversation_id,
        )
        _insert_legacy_scope(connection, scope_key="private:bot-1:peer-1", now=now)
        connection.execute(
            "UPDATE conversation_scopes SET canonical_conversation_id=? "
            "WHERE scope_key='private:bot-1:peer-1'",
            (conversation_id,),
        )
        connection.commit()

        def snapshot() -> tuple[object, ...]:
            return (
                connection.execute(
                    "SELECT id, revision FROM persons WHERE id=?",
                    (isolated_person,),
                ).fetchone(),
                connection.execute(
                    "SELECT id, revision FROM presences WHERE id=?",
                    (isolated_presence,),
                ).fetchone(),
                connection.execute(
                    "SELECT id FROM canonical_conversations WHERE id=?",
                    (conversation_id,),
                ).fetchone(),
                connection.execute(
                    "SELECT platform_message_id, author_person_id, author_presence_id, "
                    "ingress_presence_id, canonical_conversation_id "
                    "FROM chat_events ORDER BY platform_message_id"
                ).fetchall(),
                connection.execute(
                    "SELECT scope_key, canonical_conversation_id "
                    "FROM conversation_scopes ORDER BY scope_key"
                ).fetchall(),
            )

        before = snapshot()
        missing = str(uuid4())
        child_negatives = (
            {"author_kind": "person", "author_person_id": missing},
            {"author_kind": "yuki", "author_presence_id": missing},
            {"ingress_presence_id": missing},
            {"canonical_conversation_id": missing},
        )
        for index, shadows in enumerate(child_negatives):
            connection.execute("BEGIN")
            with pytest.raises(sqlite3.IntegrityError):
                _insert_shadow_message(
                    connection,
                    platform_message_id=f"dangling-{index}",
                    now=now,
                    **shadows,
                )
            connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE conversation_scopes SET canonical_conversation_id=? "
                "WHERE scope_key='private:bot-1:peer-1'",
                (missing,),
            )
        connection.rollback()
        parent_mutations = (
            ("DELETE FROM persons WHERE id=?", (isolated_person,)),
            ("UPDATE persons SET id=? WHERE id=?", (missing, isolated_person)),
            ("DELETE FROM presences WHERE id=?", (isolated_presence,)),
            ("UPDATE presences SET id=? WHERE id=?", (missing, isolated_presence)),
            ("DELETE FROM canonical_conversations WHERE id=?", (conversation_id,)),
            (
                "UPDATE canonical_conversations SET id=? WHERE id=?",
                (missing, conversation_id),
            ),
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
            "UPDATE presences SET revision=2, updated_at=? WHERE id=?",
            (now, isolated_presence),
        )
        connection.execute(
            "UPDATE canonical_conversations SET revision=2, updated_at=? WHERE id=?",
            (now, conversation_id),
        )
        connection.commit()
        assert connection.execute(
            "SELECT revision FROM persons WHERE id=?",
            (isolated_person,),
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT revision FROM presences WHERE id=?",
            (isolated_presence,),
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT revision FROM canonical_conversations WHERE id=?",
            (conversation_id,),
        ).fetchone() == (2,)


def test_multiple_scopes_can_share_one_canonical_conversation(c4_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c4_db) as connection:
        ids = _seed_identity(connection, now)
        _seed_legacy_people(connection, now)
        connection.commit()
        conversation_id = str(uuid4())
        _insert_conversation(
            connection,
            conversation_id=conversation_id,
            alias_id=str(uuid4()),
            kind="private",
            owner_id=ids["person_a"],
            now=now,
        )
        _insert_legacy_scope(connection, scope_key="private:bot-1:peer-1", now=now)
        _insert_legacy_scope(
            connection,
            scope_key="private:bot-1:peer-2",
            now=now,
            private_peer_user_id="peer-2",
        )
        connection.execute(
            "UPDATE conversation_scopes SET canonical_conversation_id=? ",
            (conversation_id,),
        )
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM conversation_scopes WHERE canonical_conversation_id=?",
            (conversation_id,),
        ).fetchone() == (2,)
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE conversation_scopes SET canonical_conversation_id=? "
                "WHERE scope_key='private:bot-1:peer-1'",
                (str(uuid4()),),
            )
        connection.rollback()
        assert connection.execute(
            "SELECT generation, scope_key FROM conversation_scopes ORDER BY scope_key"
        ).fetchall() == [
            (1, "private:bot-1:peer-1"),
            (1, "private:bot-1:peer-2"),
        ]


def test_receipt_transport_unique_and_secret_free(c4_db: Path) -> None:
    now = "2026-08-24T00:00:00+00:00"
    with _connect(c4_db) as connection:
        ids = _seed_identity(connection, now)
        event_id = str(uuid4())
        connection.execute(
            "INSERT INTO canonical_event_receipts("
            "ingress_presence_id, event_type, platform_message_id, canonical_event_id, "
            "created_at, observed_at"
            ") VALUES (?, 'message', 'mid-1', ?, ?, ?)",
            (ids["presence_qq"], event_id, now, now),
        )
        connection.execute(
            "INSERT INTO canonical_event_receipts("
            "ingress_presence_id, event_type, platform_message_id, canonical_event_id, "
            "created_at, observed_at"
            ") VALUES (?, 'message', 'mid-1', ?, ?, ?)",
            (ids["presence_telegram"], str(uuid4()), now, now),
        )
        connection.commit()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO canonical_event_receipts("
                "ingress_presence_id, event_type, platform_message_id, "
                "canonical_event_id, created_at, observed_at"
                ") VALUES (?, 'message', 'mid-1', ?, ?, ?)",
                (ids["presence_qq"], str(uuid4()), now, now),
            )
        connection.rollback()
        negatives = (
            (
                (
                    str(uuid4()),
                    "message",
                    "mid-2",
                    str(uuid4()),
                    now,
                    now,
                ),
            ),
            (
                (
                    ids["presence_qq"],
                    " message",
                    "mid-3",
                    str(uuid4()),
                    now,
                    now,
                ),
            ),
            (
                (
                    ids["presence_qq"],
                    "message",
                    " mid-4",
                    str(uuid4()),
                    now,
                    now,
                ),
            ),
            (
                (
                    ids["presence_qq"],
                    "message",
                    "mid-5",
                    str(uuid4()).upper(),
                    now,
                    now,
                ),
            ),
        )
        for params in negatives:
            connection.execute("BEGIN")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO canonical_event_receipts("
                    "ingress_presence_id, event_type, platform_message_id, "
                    "canonical_event_id, created_at, observed_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    params[0],
                )
            connection.rollback()
        columns = [
            name.casefold() for name in _column_names(connection, "canonical_event_receipts")
        ]
        assert columns == [
            "id",
            "ingress_presence_id",
            "event_type",
            "platform_message_id",
            "canonical_event_id",
            "created_at",
            "observed_at",
        ]
        for column in columns:
            assert not any(token in column for token in _SECRET_COLUMN_TOKENS)
        schema_sql = "\n".join(
            str(row[0] or "")
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='canonical_event_receipts'"
            )
        ).casefold()
        for token in _SECRET_COLUMN_TOKENS:
            assert token not in schema_sql


def test_old_writer_inventory_does_not_pass_shadow_columns() -> None:
    shadow_names = set(CHAT_EVENT_CANONICAL_SHADOW_COLUMNS) | set(
        CONVERSATION_SCOPE_CANONICAL_SHADOW_COLUMNS
    )
    found_writers = 0
    for path in _KNOWN_WRITERS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "ChatEventModel":
                    found_writers += 1
                    keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
                    assert keywords.isdisjoint(shadow_names)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "INSERT INTO chat_events" in node.value:
                    found_writers += 1
                    lowered = node.value
                    for column in shadow_names:
                        assert column not in lowered
    assert found_writers >= 3


def test_receipt_model_is_not_a_generic_bus() -> None:
    columns = {column.key for column in CanonicalEventReceiptModel.__table__.columns}
    assert "source_plugin_id" not in columns
    assert "external_event_key" not in columns
    assert "content" not in columns
