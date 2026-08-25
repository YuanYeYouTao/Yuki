"""Extension ownership/correlation shadows and 0047 cutover-descendant proofs."""

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
from sqlalchemy import create_engine, event, text
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
from tests.unit.test_migration_0045 import _column_names, _index_names
from tests.unit.test_migration_0046 import (
    _enable_sqlite_fk,
    _insert_memory_fact,
    _sqlite_trigger_names,
)

from qq_ai_bot.identity.canonical_extension_schema import (
    C6_EXCLUDED_EXTENSION,
    C6_EXTENSION_INVENTORY,
    C6_OWNERSHIP_COLUMNS,
    C6_OWNERSHIP_FOREIGN_KEYS,
    C6_OWNERSHIP_INDEXES,
    C6_OWNERSHIP_TABLES,
    C6_SHAPE_DISCRIMINATORS,
    C6_TRIGGER_NAMES,
    C6_TRIGGER_SQL,
)
from qq_ai_bot.identity.canonical_memory_schema import (
    C21_FACT_UNIQUE_INDEX_NAMES,
    C21_FACT_UNIQUE_INDEX_SQL,
    C21_MEMORY_FACT_CONFLICT_SQL,
    C21_OWNER_TABLES,
    C21_OWNERSHIP_COLUMNS,
    C21_OWNERSHIP_FOREIGN_KEYS,
    C21_OWNERSHIP_INDEXES,
    C21_REFLECTION_UNIQUE_INDEX_NAMES,
    C21_REFLECTION_UNIQUE_INDEX_SQL,
    C21_TRIGGER_NAMES,
    C21_TRIGGER_SQL,
)
from qq_ai_bot.identity.canonical_ownership_schema import C5_TRIGGER_NAMES, C5_TRIGGER_SQL
from qq_ai_bot.identity.db_models import (
    _install_c6_triggers_after_metadata_create,
    _install_c21_triggers_after_metadata_create,
)
from qq_ai_bot.persistence.metadata import Base

_MIGRATION_PATH = Path("migrations/versions/0047_canonical_extension_shadows.py")
_KNOWN_WRITERS = (
    Path("src/qq_ai_bot/automation/repository.py"),
    Path("src/qq_ai_bot/admin/config_service.py"),
    Path("src/qq_ai_bot/plugin_host/repository.py"),
    Path("src/qq_ai_bot/plugin_host/session_repository.py"),
    Path("src/qq_ai_bot/plugin_host/notification_repository.py"),
    Path("src/qq_ai_bot/emoji/repository.py"),
    Path("src/qq_ai_bot/speech/repository.py"),
    Path("src/qq_ai_bot/mcp/repository.py"),
    Path("src/qq_ai_bot/model_runtime/repository.py"),
    Path("src/qq_ai_bot/persistence/turn_observations.py"),
    Path("src/qq_ai_bot/conversation/cadence.py"),
    Path("src/qq_ai_bot/persistence/web_repository.py"),
)
_WRITER_NAMES = {
    "AutomationModel",
    "PluginConfigValueModel",
    "PluginStateModel",
    "PluginAgentSessionModel",
    "PluginAgentMessageModel",
    "PluginBackgroundTargetGrantModel",
    "PluginNotificationOutboxModel",
    "PluginBackgroundTurnJobModel",
    "RuntimeConfigOverrideModel",
    "EmojiAssetModel",
    "EmojiScopeStateModel",
    "EmojiUsageEventModel",
    "SpeechGenerationModel",
    "ToolInvocationModel",
    "ModelInvocationModel",
    "RuntimeTurnObservationModel",
    "ReplyEffectEventModel",
    "WebSearchRunModel",
}
_SHADOW_NAMES = {column for columns in C6_OWNERSHIP_COLUMNS.values() for column in columns}
_EXPECTED_FKS = tuple(
    (table, parent, column, parent_column, "RESTRICT", "RESTRICT", "NONE")
    for table, column, parent, parent_column in C6_OWNERSHIP_FOREIGN_KEYS
)
_CREATING_REVISIONS = (
    "0005",
    "0006",
    "0008",
    "0012",
    "0013",
    "0014",
    "0015",
    "0018",
    "0019",
    "0028",
    "0037",
    "0039",
    "0046",
)
_EXPLICIT_CREATE_SOURCES = (
    Path("migrations/versions/0006_add_web_search_sources.py"),
    Path("migrations/versions/0008_add_runtime_admin_system.py"),
    Path("migrations/versions/0012_add_automation_runtime.py"),
    Path("migrations/versions/0013_add_planner_and_plugin_runtime.py"),
    Path("migrations/versions/0014_add_emoji_system.py"),
    Path("migrations/versions/0015_add_local_speech_system.py"),
    Path("migrations/versions/0018_add_model_invocations.py"),
    Path("migrations/versions/0019_add_tool_kernel_mcp.py"),
    Path("migrations/versions/0028_plugin_external_notifications.py"),
    Path("migrations/versions/0037_runtime_turn_correlation.py"),
    Path("migrations/versions/0039_reply_effect_events.py"),
)
_C6_SPECIFIC_TOKENS = (
    "canonical_creator_person_id",
    "canonical_target_person_id",
    "canonical_target_space_id",
    "canonical_owner_person_id",
    "canonical_sender_person_id",
    "canonical_created_by_person_id",
    "canonical_first_seen_person_id",
    "canonical_first_seen_space_id",
    "canonical_actor_person_id",
    "canonical_presence_id",
    "extension_shadow",
)
_FORBIDDEN_TABLES = {
    "yuki",
    "yukis",
    "yuki_self",
    "yukiself",
    "gateway_connections",
    "presence_active_routes",
    "delivery_routes",
}
_NOW = "2026-08-24T00:00:00+00:00"


def _create_orm_c6_schema(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    event.listen(engine, "connect", _enable_sqlite_fk)
    Base.metadata.create_all(engine)
    engine.dispose()


def _normalized_c6_schema(path: Path) -> dict[str, Any]:
    full = _normalized_schema(path)
    tables = [name for name in full["tables"] if name in C6_OWNERSHIP_TABLES]
    return {
        "tables": tables,
        "columns": {
            name: [
                column
                for column in full["columns"][name]
                if column[0] in C6_OWNERSHIP_COLUMNS[name]
            ]
            for name in tables
        },
        "indexes": {
            name: [item for item in full["indexes"][name] if item[0] in C6_OWNERSHIP_INDEXES]
            for name in tables
        },
        "foreign_keys": {
            name: [
                item for item in full["foreign_keys"][name] if item[1] in C6_OWNERSHIP_COLUMNS[name]
            ]
            for name in tables
        },
        "triggers": {
            name: sql for name, sql in full["triggers"].items() if name in C6_TRIGGER_NAMES
        },
    }


def _c6_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return set(_column_names(connection, table)) & set(C6_OWNERSHIP_COLUMNS[table])


def _c6_present(connection: sqlite3.Connection) -> bool:
    tables = _tables(connection)
    return any(table in tables and _c6_columns(connection, table) for table in C6_OWNERSHIP_TABLES)


def _seed_people_and_groups(connection: sqlite3.Connection) -> None:
    for user_id, is_bot in (("bot-1", 1), ("peer-1", 0), ("peer-2", 0), ("1001", 0)):
        connection.execute(
            "INSERT INTO people(user_id, nickname, enabled, is_bot, first_seen_at, last_seen_at) "
            "VALUES (?, '', 1, ?, ?, ?)",
            (user_id, is_bot, _NOW, _NOW),
        )
    for group_id in ("2001", "2002"):
        connection.execute(
            "INSERT INTO groups(group_id, name, enabled, require_mention, autonomous_enabled, "
            "first_seen_at, last_seen_at, updated_at) VALUES (?, '', 1, 1, 1, ?, ?, ?)",
            (group_id, _NOW, _NOW, _NOW),
        )


def _seed_plugin(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO plugin_installations("
        "plugin_id, name, version, plugin_api, yuki_requires, manifest_hash, "
        "entrypoint, status, enabled, approved_permissions_json, "
        "requested_permissions_json, failure_count, discovered_at, updated_at"
        ") VALUES ('fixture', 'Fixture', '1.0.0', '2.0', '>=3.7.0', 'hash', "
        "'fixture:plugin', 'running', 1, '[]', '[]', 0, ?, ?)",
        (_NOW, _NOW),
    )


def _seed_voice_profile(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO speech_voice_profiles("
        "profile_id, display_name, provider, engine_model_version, language, "
        "supported_languages_json, model_relative_path, model_checksum, default_style, "
        "enabled, is_default, source, source_note, license_note, manifest_hash, "
        "created_at, updated_at"
        ") VALUES ('roxy', 'Roxy', 'genie', 'v2proplus', 'zh', '[]', 'voices/roxy', "
        "?, 'neutral', 1, 1, 'user_supplied', '', '', ?, ?, ?)",
        ("a" * 64, "b" * 64, _NOW, _NOW),
    )


def _insert_c6_conversation(
    connection: sqlite3.Connection,
    *,
    conversation_id: str,
    owner_person_id: str,
) -> None:
    alias_id = str(uuid4())
    connection.execute(
        "INSERT INTO canonical_conversations("
        "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
        "starts_after_event_id, last_event_id, last_generation_change_event_id, "
        "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
        "revision, created_at, updated_at"
        ") VALUES (?, 'private', ?, NULL, ?, 1, 1, 0, 0, 0, 0, 0, 0, 1, ?, ?)",
        (conversation_id, owner_person_id, alias_id, _NOW, _NOW),
    )
    connection.execute(
        "INSERT INTO conversation_legacy_aliases("
        "id, conversation_id, scope_key, is_primary, created_at, updated_at"
        ") VALUES (?, ?, ?, 1, ?, ?)",
        (alias_id, conversation_id, f"scope:{alias_id}", _NOW, _NOW),
    )


def _insert_web_search_run(
    connection: sqlite3.Connection,
    *,
    conversation_key: str = "private:peer-1",
    trigger_message_id: str = "web-1",
    **shadows: object,
) -> None:
    columns = [
        "conversation_key",
        "trigger_message_id",
        "query",
        "provider",
        "created_at",
        "partial_failure",
    ]
    values: list[object] = [conversation_key, trigger_message_id, "hello", "demo", _NOW, 0]
    for column, value in shadows.items():
        columns.append(column)
        values.append(value)
    connection.execute(
        f"INSERT INTO web_search_runs({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in values)})",
        values,
    )


def _insert_chat_event(connection: sqlite3.Connection, platform_message_id: str) -> int:
    connection.execute(
        "INSERT INTO chat_events("
        "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
        "sender_user_id, direction, content, visual_summary, segments_json, "
        "origin, occurred_at, observed_at"
        ") VALUES ('bot-1', ?, 'private', 'peer-1', 'peer-1', 'inbound', "
        "'hello', '', '[]', 'user_message', ?, ?)",
        (platform_message_id, _NOW, _NOW),
    )
    row = connection.execute("SELECT last_insert_rowid()").fetchone()
    assert row is not None
    return int(row[0])


def _insert_emoji_asset(connection: sqlite3.Connection, emoji_id: str, sha: str) -> None:
    connection.execute(
        "INSERT INTO emoji_assets("
        "id, sha256, relative_path, image_format, mime_type, byte_size, width, height, "
        "frame_count, animated, status, description, emotion_tags_json, "
        "usage_scenarios_json, ocr_text, intensity, confidence, analysis_version, "
        "pinned, source_sub_type, source_emoji_id, source_package_id, seen_count, "
        "use_count, first_seen_at, last_seen_at, created_at, updated_at"
        ") VALUES (?, ?, ?, 'png', 'image/png', 12, 8, 8, 1, 0, 'candidate', '', "
        "'[]', '[]', '', 0.5, 0.0, '', 0, '', '', '', 1, 0, ?, ?, ?, ?)",
        (emoji_id, sha, f"emoji/{emoji_id}.png", _NOW, _NOW, _NOW, _NOW),
    )


def _insert_automation(connection: sqlite3.Connection, name: str, **shadows: object) -> None:
    columns = [
        "creator_user_id",
        "bot_user_id",
        "name",
        "status",
        "timezone",
        "schedule_json",
        "script_json",
        "script_hash",
        "required_capabilities_json",
        "authority_snapshot_json",
        "created_from_message_id",
        "run_count",
        "consecutive_failures",
        "misfire_grace_seconds",
        "created_at",
        "updated_at",
    ]
    values: list[object] = [
        "peer-1",
        "bot-1",
        name,
        "active",
        "Asia/Shanghai",
        "{}",
        "{}",
        "hash",
        "[]",
        "{}",
        "event-1",
        0,
        0,
        1800,
        _NOW,
        _NOW,
    ]
    for column, value in shadows.items():
        columns.append(column)
        values.append(value)
    connection.execute(
        f"INSERT INTO automations({', '.join(columns)}) VALUES ({', '.join('?' for _ in values)})",
        values,
    )


def _insert_runtime_config(
    connection: sqlite3.Connection,
    *,
    key: str,
    scope_type: str,
    scope_id: str = "",
    **shadows: object,
) -> None:
    columns = [
        "config_key",
        "scope_type",
        "scope_id",
        "value_json",
        "value_type",
        "apply_mode",
        "version",
        "created_at",
        "updated_at",
        "updated_by",
    ]
    values: list[object] = [
        key,
        scope_type,
        scope_id,
        "1",
        "integer",
        "hot",
        1,
        _NOW,
        _NOW,
        "test",
    ]
    for column, value in shadows.items():
        columns.append(column)
        values.append(value)
    connection.execute(
        "INSERT INTO runtime_config_overrides("
        f"{', '.join(columns)}) VALUES ({', '.join('?' for _ in values)})",
        values,
    )


def _insert_plugin_config(
    connection: sqlite3.Connection,
    *,
    key: str,
    scope_type: str,
    scope_id: str = "",
    **shadows: object,
) -> None:
    columns = [
        "plugin_id",
        "scope_type",
        "scope_id",
        "key",
        "value_json",
        "version",
        "updated_at",
    ]
    values: list[object] = ["fixture", scope_type, scope_id, key, "1", 1, _NOW]
    for column, value in shadows.items():
        columns.append(column)
        values.append(value)
    connection.execute(
        f"INSERT INTO plugin_config_values({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in values)})",
        values,
    )


def _shadows_are_null(connection: sqlite3.Connection, table: str) -> bool:
    columns = C6_OWNERSHIP_COLUMNS[table]
    rows = connection.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
    return bool(rows) and all(all(value is None for value in row) for row in rows)


@pytest.fixture(params=["alembic", "orm"])
def c6_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    path = tmp_path / f"{request.param}.db"
    if request.param == "alembic":
        _upgrade(path, monkeypatch, "head")
    else:
        _create_orm_c6_schema(path)
    return path


def test_c6_inventory_covers_required_extension_and_excludes_catalogs() -> None:
    tables = {item["table"] for item in C6_EXTENSION_INVENTORY}
    assert tables == {
        "automations",
        "plugin_config_values",
        "plugin_state",
        "plugin_agent_sessions",
        "plugin_agent_messages",
        "plugin_background_target_grants",
        "plugin_notification_outbox",
        "plugin_background_turn_jobs",
        "runtime_config_overrides",
        "emoji_assets",
        "emoji_scope_states",
        "emoji_usage_events",
        "speech_generations",
        "tool_invocations",
        "web_search_runs",
        "model_invocations",
        "runtime_turn_observations",
        "reply_effect_events",
    }
    reasons = " ".join(item["reason"] for item in C6_EXTENSION_INVENTORY)
    assert "Presence correlation" in reasons
    assert "plugin-owned" in reasons
    assert "provider_id is not an owner" in reasons
    assert "conversation_key" in reasons
    excluded = " ".join(reason for _name, reason in C6_EXCLUDED_EXTENSION)
    assert "run_id" in excluded
    assert "plugin is not a Person" in excluded
    assert "C5" in excluded
    assert "C21" in excluded
    assert "Control Plane" in excluded
    assert "forbidden new tables" in excluded
    assert not any(index.startswith("uq_") for index in C6_OWNERSHIP_INDEXES)
    assert C6_OWNERSHIP_COLUMNS["automations"] == (
        "canonical_creator_person_id",
        "canonical_target_person_id",
        "canonical_target_space_id",
        "canonical_presence_id",
    )


def test_creating_revisions_and_0046_do_not_create_c6_shadows(
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
            assert not _c6_present(connection)
            trigger_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            assert not (set(C6_TRIGGER_NAMES) & trigger_names)
            if revision == "0005":
                assert not (set(C6_OWNERSHIP_TABLES) & _tables(connection))


def test_fresh_upgrade_head_creates_only_inventory_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fresh-head.db"
    _upgrade(path, monkeypatch, "0047")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0047",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not (_FORBIDDEN_TABLES & _tables(connection))
        for table, columns in C6_OWNERSHIP_COLUMNS.items():
            assert set(columns) <= set(_column_names(connection, table))
            for column in columns:
                info = {
                    str(row[1]): row for row in connection.execute(f'PRAGMA table_info("{table}")')
                }
                assert info[column][3] == 0
                assert info[column][4] is None
        assert set(C6_OWNERSHIP_INDEXES) <= set().union(
            *(_index_names(connection, table) for table in C6_OWNERSHIP_TABLES)
        )
        for table in C6_OWNERSHIP_TABLES:
            flags = {
                str(row[1]): int(row[2])
                for row in connection.execute(f'PRAGMA index_list("{table}")')
            }
            for item in set(C6_OWNERSHIP_INDEXES) & set(flags):
                assert flags[item] == 0
        shadow_fks = [
            (table, row[2], row[3], row[4], row[5], row[6], row[7])
            for table in C6_OWNERSHIP_TABLES
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
            if str(row[3]) in C6_OWNERSHIP_COLUMNS[table]
        ]
        assert set(shadow_fks) == set(_EXPECTED_FKS)
        trigger_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert set(C6_TRIGGER_NAMES) <= trigger_names
        assert set(C5_TRIGGER_NAMES) <= trigger_names


def test_empty_fresh_and_0046_to_0047_schemas_are_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = tmp_path / "fresh.db"
    upgraded = tmp_path / "from-0046.db"
    _upgrade(fresh, monkeypatch, "0047")
    _upgrade(upgraded, monkeypatch, "0046")
    before = _schema_dump(upgraded)
    _upgrade(upgraded, monkeypatch, "0047")
    assert _normalized_schema(fresh) == _normalized_schema(upgraded)
    after = _schema_dump(upgraded)
    preserved = {
        "alembic_version",
        *C6_OWNERSHIP_TABLES,
        *C21_OWNER_TABLES,
        "trg_person_aliases_ownership_shadow_update",
        "trg_memory_facts_ownership_shadow_update",
    }
    assert all(after[key] == sql for key, sql in before.items() if key[1] not in preserved)


def test_orm_metadata_matches_0047_c6_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrated = tmp_path / "migrated.db"
    orm = tmp_path / "orm.db"
    _upgrade(migrated, monkeypatch, "head")
    _create_orm_c6_schema(orm)
    assert _normalized_c6_schema(migrated) == _normalized_c6_schema(orm)


def test_metadata_create_all_installs_c6_triggers_without_private_helper(
    tmp_path: Path,
) -> None:
    assert event.contains(Base.metadata, "after_create", _install_c6_triggers_after_metadata_create)
    assert event.contains(
        Base.metadata, "after_create", _install_c21_triggers_after_metadata_create
    )
    path = tmp_path / "full-create-all.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    event.listen(engine, "connect", _enable_sqlite_fk)
    Base.metadata.create_all(engine)
    names = _sqlite_trigger_names(path)
    assert set(C6_TRIGGER_NAMES) <= names
    assert set(C5_TRIGGER_NAMES) <= names
    assert set(C21_TRIGGER_NAMES) <= names
    assert len(set(C6_TRIGGER_NAMES) & names) == len(C6_TRIGGER_NAMES)
    assert len(set(C21_TRIGGER_NAMES) & names) == len(C21_TRIGGER_NAMES)
    Base.metadata.create_all(engine)
    assert _sqlite_trigger_names(path) == names
    engine.dispose()


def test_create_all_waits_until_all_c6_hosts_exist(tmp_path: Path) -> None:
    path = tmp_path / "partial-create-all.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    event.listen(engine, "connect", _enable_sqlite_fk)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["persons"],
            Base.metadata.tables["spaces"],
            Base.metadata.tables["presences"],
            Base.metadata.tables["people"],
            Base.metadata.tables["automations"],
        ],
    )
    assert not (set(C6_TRIGGER_NAMES) & _sqlite_trigger_names(path))
    Base.metadata.create_all(engine)
    assert set(C6_TRIGGER_NAMES) <= _sqlite_trigger_names(path)
    engine.dispose()


def test_c6_metadata_hook_skips_non_sqlite_dialect() -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeConnection:
        dialect = FakeDialect()

        def execute(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("sqlite trigger SQL must not run")

    _install_c6_triggers_after_metadata_create(Base.metadata, FakeConnection())  # type: ignore[arg-type]


def test_alembic_heads_is_exactly_0047() -> None:
    config = Config("alembic.ini")
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["0048"]


def test_0047_is_self_contained_alembic() -> None:
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
    loaded = SourceFileLoader("revision_0047", str(_MIGRATION_PATH)).load_module()
    assert loaded._C6_TRIGGER_SQL == C6_TRIGGER_SQL
    assert loaded._C6_TRIGGER_NAMES == C6_TRIGGER_NAMES
    assert loaded._EXTENSION_INDEXES == C6_OWNERSHIP_INDEXES
    assert loaded._C6_SHAPE_DISCRIMINATORS == C6_SHAPE_DISCRIMINATORS
    assert loaded._C21_TRIGGER_SQL == C21_TRIGGER_SQL
    assert loaded._C21_TRIGGER_NAMES == C21_TRIGGER_NAMES
    assert loaded._C21_INDEXES == C21_OWNERSHIP_INDEXES
    assert loaded._C21_FACT_UNIQUE_INDEX_SQL == C21_FACT_UNIQUE_INDEX_SQL
    assert loaded._C21_REFLECTION_UNIQUE_INDEX_SQL == C21_REFLECTION_UNIQUE_INDEX_SQL
    assert loaded._C21_MEMORY_FACT_CONFLICT_SQL == C21_MEMORY_FACT_CONFLICT_SQL
    assert loaded._C5_HARDENED_UPDATE_SQL == (
        next(
            item for item in C5_TRIGGER_SQL if "trg_person_aliases_ownership_shadow_update" in item
        ),
        next(item for item in C5_TRIGGER_SQL if "trg_memory_facts_ownership_shadow_update" in item),
    )
    legacy = SourceFileLoader(
        "revision_0046",
        "migrations/versions/0046_canonical_ownership_shadows.py",
    ).load_module()
    assert loaded._C5_LEGACY_UPDATE_SQL == (
        next(
            item
            for item in legacy._C5_TRIGGER_SQL
            if "trg_person_aliases_ownership_shadow_update" in item
        ),
        next(
            item
            for item in legacy._C5_TRIGGER_SQL
            if "trg_memory_facts_ownership_shadow_update" in item
        ),
    )


def test_0005_and_explicit_creates_do_not_mention_c6_columns() -> None:
    source_0005 = Path("migrations/versions/0005_person_centric_v1.py").read_text(encoding="utf-8")
    for token in _C6_SPECIFIC_TOKENS:
        assert token not in source_0005
    for path in _EXPLICIT_CREATE_SOURCES:
        source = path.read_text(encoding="utf-8")
        for token in _C6_SPECIFIC_TOKENS:
            assert token not in source, path.name
        assert "canonical_first_seen" not in source
        if path.name != "0039_reply_effect_events.py":
            assert "extension_shadow" not in source


def test_populated_downgrade_0047_to_0046_preserves_legacy_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / "expected-0046.db"
    path = tmp_path / "populated-downgrade.db"
    _upgrade(expected, monkeypatch, "0046")
    _upgrade(path, monkeypatch, "0047")
    with _connect(path) as connection:
        ids = _seed_identity(connection, _NOW)
        _seed_people_and_groups(connection)
        _seed_plugin(connection)
        _seed_voice_profile(connection)
        conversation_id = str(uuid4())
        _insert_c6_conversation(
            connection,
            conversation_id=conversation_id,
            owner_person_id=ids["person_a"],
        )
        event_id = _insert_chat_event(connection, "event-keep")
        _insert_automation(
            connection,
            "keep-me",
            canonical_creator_person_id=ids["person_a"],
        )
        _insert_runtime_config(
            connection,
            key="keep.global",
            scope_type="global",
        )
        _insert_plugin_config(connection, key="keep", scope_type="user", scope_id="peer-1")
        connection.execute(
            "INSERT INTO plugin_state(plugin_id, namespace, key, value_json, version, updated_at) "
            "VALUES ('fixture', 'ns', 'k', '{}', 1, ?)",
            (_NOW,),
        )
        connection.execute(
            "INSERT INTO plugin_agent_sessions("
            "session_id, plugin_id, scope_type, scope_id, name, model, instructions, "
            "persistence, context_profile, allowed_capabilities_json, status, "
            "next_sequence, turn_count, created_at, updated_at, last_active_at"
            ") VALUES ('sess-1', 'fixture', 'plugin', '', '', '', 'hello', "
            "'durable', 'none', '[]', 'active', 1, 0, ?, ?, ?)",
            (_NOW, _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO plugin_agent_messages("
            "session_id, sequence, role, content, metadata_json, created_at"
            ") VALUES ('sess-1', 1, 'user', 'hi', '{}', ?)",
            (_NOW,),
        )
        connection.execute(
            "INSERT INTO plugin_background_target_grants("
            "plugin_id, target_type, target_id, bot_user_id, enabled, "
            "created_by_user_id, created_at, updated_at"
            ") VALUES ('fixture', 'private', 'peer-1', 'bot-1', 1, '1001', ?, ?)",
            (_NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO plugin_notification_outbox("
            "notification_id, part_key, source_event_id, plugin_id, target_type, "
            "target_id, bot_user_id, part_type, text, status, attempts, max_attempts, "
            "next_attempt_at, created_at, updated_at"
            ") VALUES ('n1', 'p1', ?, 'fixture', 'private', 'peer-1', 'bot-1', "
            "'text', 'hi', 'pending', 0, 5, ?, ?, ?)",
            (event_id, _NOW, _NOW, _NOW),
        )
        job_event = _insert_chat_event(connection, "event-job")
        connection.execute(
            "INSERT INTO plugin_background_turn_jobs("
            "source_event_id, plugin_id, target_type, target_id, bot_user_id, "
            "agent_intent, status, attempts, max_attempts, next_attempt_at, "
            "generated_text, tool_calls_used, model_requests, created_at, updated_at"
            ") VALUES (?, 'fixture', 'private', 'peer-1', 'bot-1', '', 'pending', "
            "0, 3, ?, '', 0, 0, ?, ?)",
            (job_event, _NOW, _NOW, _NOW),
        )
        emoji_id = str(uuid4())
        _insert_emoji_asset(connection, emoji_id, "c" * 64)
        connection.execute(
            "INSERT INTO emoji_scope_states("
            "emoji_id, scope_type, scope_id, enabled, weight, adopted_at, updated_at"
            ") VALUES (?, 'global', '', 1, 1.0, ?, ?)",
            (emoji_id, _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO emoji_usage_events("
            "emoji_id, source, trigger_message_id, created_at"
            ") VALUES (?, 'reply', '', ?)",
            (emoji_id, _NOW),
        )
        connection.execute(
            "INSERT INTO speech_generations("
            "request_id, conversation_key_hash, profile_id, engine_version, "
            "target_language, text_hash, normalized_text_hash, character_count, "
            "cache_key, output_relative_path, output_format, status, created_at"
            ") VALUES ('req-1', ?, 'roxy', 'v2', 'zh', ?, ?, 4, 'cache', '', 'wav', "
            "'queued', ?)",
            ("d" * 64, "e" * 64, "f" * 64, _NOW),
        )
        connection.execute(
            "INSERT INTO tool_invocations("
            "conversation_key_hash, provider_id, tool_name, success, latency_seconds, "
            "result_size, artifact_created, created_at"
            ") VALUES (?, 'mcp.demo', 'search', 1, 0.1, 0, 0, ?)",
            ("g" * 64, _NOW),
        )
        _insert_web_search_run(connection)
        connection.execute(
            "INSERT INTO model_invocations("
            "task, profile_id, provider, model, success, latency_seconds, created_at"
            ") VALUES ('chat', 'flash', 'fake', 'demo', 1, 0.2, ?)",
            (_NOW,),
        )
        connection.execute(
            "INSERT INTO runtime_turn_observations("
            "runtime_turn_id, origin, scope_type, handled, sent_messages, "
            "total_latency_ms, created_at, expires_at"
            ") VALUES ('turn-1', 'user_message', 'private', 1, 1, 10, ?, ?)",
            (_NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO reply_effect_events("
            "conversation_key_hash, source_event_hash, text_sent, voice_sent, "
            "emoji_sent, voice_cadence_eligible, voice_request_basis, source, "
            "occurred_at, recorded_at"
            ") VALUES (?, ?, 1, 0, 0, 1, 'none', 'runtime', ?, ?)",
            ("h" * 64, "i" * 64, _NOW, _NOW),
        )
        connection.commit()
        automation_before = connection.execute(
            "SELECT name, creator_user_id, bot_user_id FROM automations ORDER BY name"
        ).fetchall()
        config_before = connection.execute(
            "SELECT config_key, scope_type, scope_id FROM runtime_config_overrides "
            "ORDER BY config_key"
        ).fetchall()
        plugin_before = connection.execute(
            "SELECT key, scope_type, scope_id FROM plugin_config_values ORDER BY key"
        ).fetchall()
    _alembic_downgrade(path, monkeypatch, "0046")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0046",)
        assert not _c6_present(connection)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT name, creator_user_id, bot_user_id FROM automations ORDER BY name"
            ).fetchall()
            == automation_before
        )
        assert (
            connection.execute(
                "SELECT config_key, scope_type, scope_id FROM runtime_config_overrides "
                "ORDER BY config_key"
            ).fetchall()
            == config_before
        )
        assert (
            connection.execute(
                "SELECT key, scope_type, scope_id FROM plugin_config_values ORDER BY key"
            ).fetchall()
            == plugin_before
        )
    assert _normalized_schema(path) == _normalized_schema(expected)


def test_legacy_writers_leave_shadows_null(c6_db: Path) -> None:
    with _connect(c6_db) as connection:
        _seed_people_and_groups(connection)
        _seed_plugin(connection)
        _seed_voice_profile(connection)
        _insert_automation(connection, "null-shadow")
        _insert_runtime_config(connection, key="null.global", scope_type="global")
        _insert_runtime_config(connection, key="null.user", scope_type="user", scope_id="peer-1")
        _insert_plugin_config(connection, key="null-global", scope_type="global")
        _insert_plugin_config(connection, key="null-user", scope_type="user", scope_id="peer-1")
        connection.execute(
            "INSERT INTO plugin_state(plugin_id, namespace, key, value_json, version, "
            "subject_user_id, updated_at) VALUES ('fixture', 'ns', 'k', '{}', 1, 'peer-1', ?)",
            (_NOW,),
        )
        connection.execute(
            "INSERT INTO plugin_agent_sessions("
            "session_id, plugin_id, owner_user_id, scope_type, scope_id, name, model, "
            "instructions, persistence, context_profile, allowed_capabilities_json, "
            "status, next_sequence, turn_count, created_at, updated_at, last_active_at"
            ") VALUES ('sess-null', 'fixture', 'peer-1', 'user', 'peer-1', '', '', 'hello', "
            "'durable', 'none', '[]', 'active', 1, 0, ?, ?, ?)",
            (_NOW, _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO plugin_agent_messages("
            "session_id, sequence, role, sender_user_id, content, metadata_json, created_at"
            ") VALUES ('sess-null', 1, 'user', 'peer-1', 'hi', '{}', ?)",
            (_NOW,),
        )
        connection.execute(
            "INSERT INTO plugin_background_target_grants("
            "plugin_id, target_type, target_id, bot_user_id, enabled, "
            "created_by_user_id, created_at, updated_at"
            ") VALUES ('fixture', 'group', '2001', 'bot-1', 1, '1001', ?, ?)",
            (_NOW, _NOW),
        )
        event_id = _insert_chat_event(connection, "null-outbox")
        connection.execute(
            "INSERT INTO plugin_notification_outbox("
            "notification_id, part_key, source_event_id, plugin_id, target_type, "
            "target_id, bot_user_id, part_type, text, status, attempts, max_attempts, "
            "next_attempt_at, created_at, updated_at"
            ") VALUES ('n-null', 'p1', ?, 'fixture', 'group', '2001', 'bot-1', "
            "'text', 'hi', 'pending', 0, 5, ?, ?, ?)",
            (event_id, _NOW, _NOW, _NOW),
        )
        job_event = _insert_chat_event(connection, "null-job")
        connection.execute(
            "INSERT INTO plugin_background_turn_jobs("
            "source_event_id, plugin_id, target_type, target_id, bot_user_id, "
            "agent_intent, status, attempts, max_attempts, next_attempt_at, "
            "generated_text, tool_calls_used, model_requests, created_at, updated_at"
            ") VALUES (?, 'fixture', 'group', '2001', 'bot-1', '', 'pending', "
            "0, 3, ?, '', 0, 0, ?, ?)",
            (job_event, _NOW, _NOW, _NOW),
        )
        emoji_id = str(uuid4())
        _insert_emoji_asset(connection, emoji_id, "1" * 64)
        connection.execute(
            "UPDATE emoji_assets SET first_seen_user_id='peer-1', first_seen_group_id='2001' "
            "WHERE id=?",
            (emoji_id,),
        )
        connection.execute(
            "INSERT INTO emoji_scope_states("
            "emoji_id, scope_type, scope_id, enabled, weight, adopted_at, updated_at"
            ") VALUES (?, 'group', '2001', 1, 1.0, ?, ?)",
            (emoji_id, _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO emoji_usage_events("
            "emoji_id, actor_user_id, group_id, trigger_message_id, source, created_at"
            ") VALUES (?, 'peer-1', '2001', '', 'reply', ?)",
            (emoji_id, _NOW),
        )
        connection.execute(
            "INSERT INTO speech_generations("
            "request_id, conversation_key_hash, profile_id, engine_version, "
            "target_language, text_hash, normalized_text_hash, character_count, "
            "cache_key, output_relative_path, output_format, status, created_at"
            ") VALUES ('req-null', ?, 'roxy', 'v2', 'zh', ?, ?, 4, 'cache-null', "
            "'', 'wav', 'queued', ?)",
            ("j" * 64, "k" * 64, "l" * 64, _NOW),
        )
        connection.execute(
            "INSERT INTO tool_invocations("
            "conversation_key_hash, provider_id, tool_name, success, latency_seconds, "
            "result_size, artifact_created, created_at"
            ") VALUES (?, 'mcp.demo', 'search', 1, 0.1, 0, 0, ?)",
            ("m" * 64, _NOW),
        )
        _insert_web_search_run(connection, trigger_message_id="web-null")
        connection.execute(
            "INSERT INTO model_invocations("
            "task, profile_id, provider, model, success, latency_seconds, created_at"
            ") VALUES ('chat', 'flash', 'fake', 'demo', 1, 0.2, ?)",
            (_NOW,),
        )
        connection.execute(
            "INSERT INTO runtime_turn_observations("
            "runtime_turn_id, origin, scope_type, handled, sent_messages, "
            "total_latency_ms, created_at, expires_at"
            ") VALUES ('turn-null', 'user_message', 'group', 1, 1, 10, ?, ?)",
            (_NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO reply_effect_events("
            "conversation_key_hash, source_event_hash, text_sent, voice_sent, "
            "emoji_sent, voice_cadence_eligible, voice_request_basis, source, "
            "occurred_at, recorded_at"
            ") VALUES (?, ?, 1, 0, 0, 1, 'none', 'runtime', ?, ?)",
            ("n" * 64, "o" * 64, _NOW, _NOW),
        )
        connection.commit()
        for table in C6_OWNERSHIP_TABLES:
            assert _shadows_are_null(connection, table), table


def test_many_legacy_rows_can_share_one_canonical_owner(c6_db: Path) -> None:
    with _connect(c6_db) as connection:
        ids = _seed_identity(connection, _NOW)
        _seed_people_and_groups(connection)
        _insert_automation(
            connection,
            "share-a",
            canonical_creator_person_id=ids["person_a"],
            canonical_target_space_id=ids["space_a"],
        )
        _insert_automation(
            connection,
            "share-b",
            canonical_creator_person_id=ids["person_a"],
            canonical_target_space_id=ids["space_a"],
        )
        _insert_runtime_config(
            connection,
            key="share.a",
            scope_type="user",
            scope_id="peer-1",
            canonical_person_id=ids["person_a"],
        )
        _insert_runtime_config(
            connection,
            key="share.b",
            scope_type="user",
            scope_id="peer-2",
            canonical_person_id=ids["person_a"],
        )
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM automations WHERE canonical_creator_person_id=?",
            (ids["person_a"],),
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_config_overrides WHERE canonical_person_id=?",
            (ids["person_a"],),
        ).fetchone() == (2,)


def test_scope_and_target_shadow_contract(c6_db: Path) -> None:
    with _connect(c6_db) as connection:
        ids = _seed_identity(connection, _NOW)
        _seed_people_and_groups(connection)
        _seed_plugin(connection)
        _insert_runtime_config(connection, key="ok.global", scope_type="global")
        _insert_runtime_config(
            connection,
            key="ok.user",
            scope_type="user",
            scope_id="peer-1",
            canonical_person_id=ids["person_a"],
        )
        _insert_runtime_config(
            connection,
            key="ok.group",
            scope_type="group",
            scope_id="2001",
            canonical_space_id=ids["space_a"],
        )
        _insert_plugin_config(connection, key="ok-global", scope_type="global")
        _insert_automation(
            connection,
            "ok-targets-null",
            canonical_creator_person_id=ids["person_a"],
        )
        connection.execute(
            "INSERT INTO plugin_agent_sessions("
            "session_id, plugin_id, owner_user_id, scope_type, scope_id, name, model, "
            "instructions, persistence, context_profile, allowed_capabilities_json, "
            "status, next_sequence, turn_count, created_at, updated_at, last_active_at, "
            "canonical_owner_person_id, canonical_space_id"
            ") VALUES ('sess-group', 'fixture', 'peer-1', 'group', '2001', '', '', 'hello', "
            "'durable', 'none', '[]', 'active', 1, 0, ?, ?, ?, ?, ?)",
            (_NOW, _NOW, _NOW, ids["person_a"], ids["space_a"]),
        )
        emoji_id = str(uuid4())
        _insert_emoji_asset(connection, emoji_id, "2" * 64)
        connection.execute(
            "INSERT INTO emoji_scope_states("
            "emoji_id, scope_type, scope_id, enabled, weight, adopted_at, updated_at"
            ") VALUES (?, 'global', '', 1, 1.0, ?, ?)",
            (emoji_id, _NOW, _NOW),
        )
        connection.commit()
        rejected = (
            lambda: _insert_runtime_config(
                connection,
                key="bad.global-person",
                scope_type="global",
                canonical_person_id=ids["person_a"],
            ),
            lambda: _insert_runtime_config(
                connection,
                key="bad.both",
                scope_type="user",
                scope_id="peer-1",
                canonical_person_id=ids["person_a"],
                canonical_space_id=ids["space_a"],
            ),
            lambda: _insert_plugin_config(
                connection,
                key="bad-user-space",
                scope_type="user",
                scope_id="peer-1",
                canonical_space_id=ids["space_a"],
            ),
            lambda: _insert_automation(
                connection,
                "bad-both-targets",
                canonical_target_person_id=ids["person_a"],
                canonical_target_space_id=ids["space_a"],
            ),
            lambda: connection.execute(
                "INSERT INTO emoji_scope_states("
                "emoji_id, scope_type, scope_id, enabled, weight, adopted_at, updated_at, "
                "canonical_space_id) VALUES (?, 'global', '', 1, 1.0, ?, ?, ?)",
                (emoji_id, _NOW, _NOW, ids["space_a"]),
            ),
            lambda: connection.execute(
                "INSERT INTO plugin_agent_sessions("
                "session_id, plugin_id, scope_type, scope_id, instructions, "
                "allowed_capabilities_json, created_at, updated_at, last_active_at, "
                "canonical_space_id) VALUES ('sess-user-space', 'fixture', 'user', "
                "'peer-1', 'hello', '[]', ?, ?, ?, ?)",
                (_NOW, _NOW, _NOW, ids["space_a"]),
            ),
            lambda: connection.execute(
                "INSERT INTO runtime_turn_observations("
                "runtime_turn_id, origin, scope_type, handled, sent_messages, "
                "total_latency_ms, created_at, expires_at, canonical_space_id"
                ") VALUES ('turn-bad', 'user_message', 'private', 1, 0, 0, ?, ?, ?)",
                (_NOW, _NOW, ids["space_a"]),
            ),
        )
        for action in rejected:
            connection.execute("BEGIN")
            with pytest.raises(sqlite3.IntegrityError):
                action()
            connection.rollback()


def test_uuid_and_foreign_keys_reject_dangling_and_parent_mutation(c6_db: Path) -> None:
    with _connect(c6_db) as connection:
        ids = _seed_identity(connection, _NOW)
        _seed_people_and_groups(connection)
        _seed_voice_profile(connection)
        isolated_person = str(uuid4())
        isolated_space = str(uuid4())
        isolated_presence = str(uuid4())
        isolated_conversation = str(uuid4())
        connection.execute(
            "INSERT INTO persons(id, enabled, revision, created_at, updated_at) "
            "VALUES (?, 1, 1, ?, ?)",
            (isolated_person, _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO spaces(id, name, enabled, autonomous_enabled, require_mention, "
            "revision, created_at, updated_at) VALUES (?, '', 1, 1, 1, 1, ?, ?)",
            (isolated_space, _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO presences("
            "id, platform, external_account_id, enabled, ingest_eligible, "
            "revision, created_at, updated_at"
            ") VALUES (?, 'qq', '8100', 1, 1, 1, ?, ?)",
            (isolated_presence, _NOW, _NOW),
        )
        _insert_c6_conversation(
            connection,
            conversation_id=isolated_conversation,
            owner_person_id=ids["person_a"],
        )
        _insert_automation(
            connection,
            "fk-row",
            canonical_creator_person_id=isolated_person,
            canonical_presence_id=isolated_presence,
        )
        _insert_runtime_config(
            connection,
            key="fk.space",
            scope_type="group",
            scope_id="2001",
            canonical_space_id=isolated_space,
        )
        connection.execute(
            "INSERT INTO speech_generations("
            "request_id, conversation_key_hash, profile_id, engine_version, "
            "target_language, text_hash, normalized_text_hash, character_count, "
            "cache_key, output_relative_path, output_format, status, created_at, "
            "canonical_conversation_id"
            ") VALUES ('req-fk', ?, 'roxy', 'v2', 'zh', ?, ?, 4, 'cache-fk', "
            "'', 'wav', 'queued', ?, ?)",
            ("p" * 64, "q" * 64, "r" * 64, _NOW, isolated_conversation),
        )
        _insert_web_search_run(
            connection,
            trigger_message_id="web-fk",
            canonical_conversation_id=isolated_conversation,
        )
        connection.commit()
        missing = str(uuid4())
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_automation(connection, "uuid1", canonical_creator_person_id=str(uuid1()))
        connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_automation(
                connection,
                "upper",
                canonical_creator_person_id=isolated_person.upper(),
            )
        connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_automation(connection, "dangling", canonical_creator_person_id=missing)
        connection.rollback()
        parent_mutations = (
            ("DELETE FROM persons WHERE id=?", (isolated_person,)),
            ("UPDATE persons SET id=? WHERE id=?", (missing, isolated_person)),
            ("DELETE FROM spaces WHERE id=?", (isolated_space,)),
            ("UPDATE spaces SET id=? WHERE id=?", (missing, isolated_space)),
            ("DELETE FROM presences WHERE id=?", (isolated_presence,)),
            ("UPDATE presences SET id=? WHERE id=?", (missing, isolated_presence)),
            ("DELETE FROM canonical_conversations WHERE id=?", (isolated_conversation,)),
            (
                "UPDATE canonical_conversations SET id=? WHERE id=?",
                (missing, isolated_conversation),
            ),
        )
        before = (
            connection.execute("SELECT id FROM persons WHERE id=?", (isolated_person,)).fetchone(),
            connection.execute("SELECT id FROM spaces WHERE id=?", (isolated_space,)).fetchone(),
            connection.execute(
                "SELECT id FROM presences WHERE id=?", (isolated_presence,)
            ).fetchone(),
            connection.execute(
                "SELECT id FROM canonical_conversations WHERE id=?",
                (isolated_conversation,),
            ).fetchone(),
        )
        for statement, params in parent_mutations:
            connection.execute("BEGIN")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, params)
            connection.rollback()
        assert (
            connection.execute("SELECT id FROM persons WHERE id=?", (isolated_person,)).fetchone(),
            connection.execute("SELECT id FROM spaces WHERE id=?", (isolated_space,)).fetchone(),
            connection.execute(
                "SELECT id FROM presences WHERE id=?", (isolated_presence,)
            ).fetchone(),
            connection.execute(
                "SELECT id FROM canonical_conversations WHERE id=?",
                (isolated_conversation,),
            ).fetchone(),
        ) == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_old_uniques_still_hold_and_c6_adds_none(c6_db: Path) -> None:
    with _connect(c6_db) as connection:
        _seed_people_and_groups(connection)
        _seed_plugin(connection)
        _insert_runtime_config(connection, key="dup.key", scope_type="global")
        _insert_plugin_config(connection, key="dup", scope_type="global")
        connection.commit()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_runtime_config(connection, key="dup.key", scope_type="global")
        connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_plugin_config(connection, key="dup", scope_type="global")
        connection.rollback()
        for table in C6_OWNERSHIP_TABLES:
            flags = {
                str(row[1]): int(row[2])
                for row in connection.execute(f'PRAGMA index_list("{table}")')
            }
            for item in set(C6_OWNERSHIP_INDEXES) & set(flags):
                assert flags[item] == 0


def test_old_writer_inventory_does_not_pass_shadow_columns() -> None:
    found_writers = 0
    for path in _KNOWN_WRITERS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name in _WRITER_NAMES:
                    found_writers += 1
                    keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
                    assert keywords.isdisjoint(_SHADOW_NAMES), path
                if func_name == "values":
                    keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
                    if keywords & {
                        "creator_user_id",
                        "scope_type",
                        "plugin_id",
                        "conversation_key_hash",
                        "runtime_turn_id",
                    }:
                        found_writers += 1
                        assert keywords.isdisjoint(_SHADOW_NAMES), path
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "INSERT INTO " in node.value:
                    for table in C6_OWNERSHIP_TABLES:
                        if f"INSERT INTO {table}" in node.value:
                            found_writers += 1
                            for column in _SHADOW_NAMES:
                                assert column not in node.value
    assert found_writers >= 12


def test_discriminator_only_update_is_rejected(c6_db: Path) -> None:
    with _connect(c6_db) as connection:
        ids = _seed_identity(connection, _NOW)
        _seed_people_and_groups(connection)
        _seed_plugin(connection)
        _insert_runtime_config(
            connection,
            key="disc.user",
            scope_type="user",
            scope_id="peer-1",
            canonical_person_id=ids["person_a"],
        )
        _insert_plugin_config(
            connection,
            key="disc-user",
            scope_type="user",
            scope_id="peer-1",
            canonical_person_id=ids["person_a"],
        )
        _insert_runtime_config(connection, key="disc.null", scope_type="user", scope_id="peer-2")
        emoji_id = str(uuid4())
        _insert_emoji_asset(connection, emoji_id, "3" * 64)
        connection.execute(
            "INSERT INTO emoji_scope_states("
            "emoji_id, scope_type, scope_id, enabled, weight, adopted_at, updated_at, "
            "canonical_space_id) VALUES (?, 'group', '2001', 1, 1.0, ?, ?, ?)",
            (emoji_id, _NOW, _NOW, ids["space_a"]),
        )
        connection.execute(
            "INSERT INTO plugin_agent_sessions("
            "session_id, plugin_id, scope_type, scope_id, name, model, instructions, "
            "persistence, context_profile, allowed_capabilities_json, status, "
            "next_sequence, turn_count, created_at, updated_at, last_active_at, "
            "canonical_space_id) VALUES ('sess-disc', 'fixture', 'group', '2001', "
            "'', '', 'hello', 'durable', 'none', '[]', 'active', 1, 0, ?, ?, ?, ?)",
            (_NOW, _NOW, _NOW, ids["space_a"]),
        )
        connection.execute(
            "INSERT INTO plugin_background_target_grants("
            "plugin_id, target_type, target_id, bot_user_id, enabled, "
            "created_by_user_id, created_at, updated_at, canonical_target_person_id"
            ") VALUES ('fixture', 'private', 'peer-1', 'bot-1', 1, '1001', ?, ?, ?)",
            (_NOW, _NOW, ids["person_a"]),
        )
        connection.execute(
            "INSERT INTO runtime_turn_observations("
            "runtime_turn_id, origin, scope_type, handled, sent_messages, "
            "total_latency_ms, created_at, expires_at, canonical_person_id"
            ") VALUES ('turn-disc', 'user_message', 'private', 1, 0, 0, ?, ?, ?)",
            (_NOW, _NOW, ids["person_a"]),
        )
        connection.commit()
        rejected = (
            "UPDATE runtime_config_overrides SET scope_type='global', scope_id='' "
            "WHERE config_key='disc.user'",
            "UPDATE plugin_config_values SET scope_type='global', scope_id='' "
            "WHERE key='disc-user'",
            "UPDATE emoji_scope_states SET scope_type='global', scope_id='' "
            f"WHERE emoji_id='{emoji_id}'",
            "UPDATE plugin_agent_sessions SET scope_type='user', scope_id='peer-1' "
            "WHERE session_id='sess-disc'",
            "UPDATE plugin_background_target_grants SET target_type='group', "
            "target_id='2001' WHERE target_id='peer-1'",
            "UPDATE runtime_turn_observations SET scope_type='group' "
            "WHERE runtime_turn_id='turn-disc'",
        )
        for statement in rejected:
            connection.execute("BEGIN")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
            connection.rollback()
        connection.execute(
            "UPDATE runtime_config_overrides SET scope_type='global', scope_id='' "
            "WHERE config_key='disc.null'"
        )
        connection.commit()
        assert connection.execute(
            "SELECT scope_type, canonical_person_id FROM runtime_config_overrides "
            "WHERE config_key='disc.null'"
        ).fetchone() == ("global", None)


def test_0046_c5_discriminator_bypass_is_closed_by_0047(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reproducible = tmp_path / "at-0046.db"
    _upgrade(reproducible, monkeypatch, "0046")
    with _connect(reproducible) as connection:
        ids = _seed_identity(connection, _NOW)
        _seed_people_and_groups(connection)
        connection.execute(
            "INSERT INTO person_aliases(user_id, group_scope, alias, alias_type, "
            "first_seen_at, last_seen_at, canonical_person_id, canonical_space_id) "
            "VALUES ('peer-1', '2001', 'Ada', 'group_card', ?, ?, ?, ?)",
            (_NOW, _NOW, ids["person_a"], ids["space_a"]),
        )
        _insert_memory_fact(
            connection,
            now=_NOW,
            scope_type="self",
            memory_key="c5-bypass",
            visibility_type="private",
            visibility_user_id="peer-1",
            canonical_visibility_person_id=ids["person_a"],
        )
        connection.commit()
        connection.execute("UPDATE person_aliases SET group_scope=''")
        connection.execute(
            "UPDATE memory_facts SET visibility_type='global', visibility_user_id=NULL "
            "WHERE memory_key='c5-bypass'"
        )
        connection.commit()
        assert connection.execute(
            "SELECT group_scope, canonical_space_id FROM person_aliases WHERE alias='Ada'"
        ).fetchone() == ("", ids["space_a"])
        assert connection.execute(
            "SELECT visibility_type, canonical_visibility_person_id FROM memory_facts "
            "WHERE memory_key='c5-bypass'"
        ).fetchone() == ("global", ids["person_a"])

    hardened = tmp_path / "to-0047.db"
    _upgrade(hardened, monkeypatch, "0046")
    with _connect(hardened) as connection:
        ids = _seed_identity(connection, _NOW)
        _seed_people_and_groups(connection)
        connection.execute(
            "INSERT INTO person_aliases(user_id, group_scope, alias, alias_type, "
            "first_seen_at, last_seen_at, canonical_person_id, canonical_space_id) "
            "VALUES ('peer-1', '2001', 'Ada', 'group_card', ?, ?, ?, ?)",
            (_NOW, _NOW, ids["person_a"], ids["space_a"]),
        )
        _insert_memory_fact(
            connection,
            now=_NOW,
            scope_type="self",
            memory_key="c5-bypass",
            visibility_type="private",
            visibility_user_id="peer-1",
            canonical_visibility_person_id=ids["person_a"],
        )
        connection.commit()
    _upgrade(hardened, monkeypatch, "0047")
    with _connect(hardened) as connection:
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE person_aliases SET group_scope=''")
        connection.rollback()
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE memory_facts SET visibility_type='global', visibility_user_id=NULL "
                "WHERE memory_key='c5-bypass'"
            )
        connection.rollback()
        assert connection.execute(
            "SELECT group_scope, canonical_space_id FROM person_aliases WHERE alias='Ada'"
        ).fetchone() == ("2001", ids["space_a"])


def test_create_all_restores_missing_non_marker_triggers(tmp_path: Path) -> None:
    path = tmp_path / "restore-triggers.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    event.listen(engine, "connect", _enable_sqlite_fk)
    Base.metadata.create_all(engine)
    assert C6_TRIGGER_NAMES[0] in _sqlite_trigger_names(path)
    assert C5_TRIGGER_NAMES[0] in _sqlite_trigger_names(path)
    missing_c6 = "trg_reply_effect_events_extension_shadow_update"
    missing_c5 = "trg_person_speech_preferences_ownership_shadow_update"
    assert missing_c6 != C6_TRIGGER_NAMES[0]
    assert missing_c5 != C5_TRIGGER_NAMES[0]
    with engine.begin() as connection:
        connection.execute(text(f"DROP TRIGGER {missing_c6}"))
        connection.execute(text(f"DROP TRIGGER {missing_c5}"))
    assert missing_c6 not in _sqlite_trigger_names(path)
    assert missing_c5 not in _sqlite_trigger_names(path)
    Base.metadata.tables["reply_effect_events"].drop(engine)
    Base.metadata.tables["person_speech_preferences"].drop(engine)
    Base.metadata.create_all(engine)
    names = _sqlite_trigger_names(path)
    assert set(C6_TRIGGER_NAMES) <= names
    assert set(C5_TRIGGER_NAMES) <= names
    assert set(C21_TRIGGER_NAMES) <= names
    engine.dispose()


def _normalized_c21_schema(path: Path) -> dict[str, Any]:
    full = _normalized_schema(path)
    tables = [name for name in full["tables"] if name in C21_OWNER_TABLES]
    fact_indexes = set(C21_FACT_UNIQUE_INDEX_NAMES)
    reflection_indexes = set(C21_REFLECTION_UNIQUE_INDEX_NAMES)
    wanted_indexes = set(C21_OWNERSHIP_INDEXES) | fact_indexes | reflection_indexes
    return {
        "tables": tables,
        "columns": {
            name: [
                column
                for column in full["columns"][name]
                if column[0] in C21_OWNERSHIP_COLUMNS[name]
            ]
            for name in tables
        },
        "indexes": {
            name: [item for item in full["indexes"][name] if item[0] in wanted_indexes]
            for name in (*tables, "memory_facts")
            if name in full["indexes"]
        },
        "foreign_keys": {
            name: [
                item
                for item in full["foreign_keys"][name]
                if item[1] in C21_OWNERSHIP_COLUMNS[name]
            ]
            for name in tables
        },
        "triggers": {
            name: sql for name, sql in full["triggers"].items() if name in C21_TRIGGER_NAMES
        },
    }


def test_fresh_upgrade_0047_creates_c21_memory_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fresh-c21.db"
    _upgrade(path, monkeypatch, "0047")
    expected_fks = {
        (table, parent, column, parent_column, "RESTRICT", "RESTRICT", "NONE")
        for table, column, parent, parent_column in C21_OWNERSHIP_FOREIGN_KEYS
    }
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0047",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, columns in C21_OWNERSHIP_COLUMNS.items():
            present = set(_column_names(connection, table))
            assert set(columns) <= present
            info = {str(row[1]): row for row in connection.execute(f'PRAGMA table_info("{table}")')}
            for column in columns:
                assert info[column][3] == 0
                assert info[column][4] is None
        indexes = set().union(*(_index_names(connection, table) for table in C21_OWNER_TABLES))
        indexes.update(_index_names(connection, "memory_facts"))
        assert set(C21_OWNERSHIP_INDEXES) <= indexes
        assert set(C21_FACT_UNIQUE_INDEX_NAMES) <= indexes
        assert set(C21_REFLECTION_UNIQUE_INDEX_NAMES) <= indexes
        assert set(_index_names(connection, "memory_facts")) >= {
            "uq_memory_facts_active_person_key",
            "uq_memory_facts_active_person_group_key",
            "uq_memory_facts_active_group_key",
            "uq_memory_facts_active_self_key",
            *C21_FACT_UNIQUE_INDEX_NAMES,
        }
        shadow_fks = [
            (table, row[2], row[3], row[4], row[5], row[6], row[7])
            for table in C21_OWNER_TABLES
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
            if str(row[3]) in C21_OWNERSHIP_COLUMNS[table]
        ]
        assert set(shadow_fks) == expected_fks
        trigger_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert set(C21_TRIGGER_NAMES) <= trigger_names


def test_orm_metadata_matches_0047_c21_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrated = tmp_path / "migrated-c21.db"
    orm = tmp_path / "orm-c21.db"
    _upgrade(migrated, monkeypatch, "head")
    _create_orm_c6_schema(orm)
    assert _normalized_c21_schema(migrated) == _normalized_c21_schema(orm)


def test_0047_blocks_canonical_memory_fact_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "c21-conflict.db"
    _upgrade(path, monkeypatch, "0046")
    with _connect(path) as connection:
        ids = _seed_identity(connection, _NOW)
        _seed_people_and_groups(connection)
        for subject in ("peer-1", "peer-2"):
            _insert_memory_fact(
                connection,
                now=_NOW,
                scope_type="person",
                memory_key="dup-person",
                subject_user_id=subject,
                canonical_subject_person_id=ids["person_a"],
            )
        connection.commit()
    with pytest.raises(Exception, match="canonical memory fact conflict"):
        _upgrade(path, monkeypatch, "0047")


def test_0005_excludes_c21_memory_owner_tables_and_tokens() -> None:
    source = Path("migrations/versions/0005_person_centric_v1.py").read_text(encoding="utf-8")
    for table in (*C21_OWNER_TABLES, "memory_facts"):
        assert f'"{table}"' in source
    for token in (
        "uq_memory_facts_active_canonical_person_key",
        "trg_memory_jobs_memory_owner_insert",
        "memory_owner_insert",
        "_C21_",
    ):
        assert token not in source


def test_c6_inventory_still_excludes_memory_owners() -> None:
    excluded = " ".join(reason for _name, reason in C6_EXCLUDED_EXTENSION)
    assert "C21" in excluded
    assert "memory_jobs" not in C6_OWNERSHIP_COLUMNS
    assert "memory_tool_receipts" not in C6_OWNERSHIP_COLUMNS
