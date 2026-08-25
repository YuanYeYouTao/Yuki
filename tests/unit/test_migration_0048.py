"""Identity cutover tables, legacy carrier FK rebuild, and 0048 proofs."""

from __future__ import annotations

import ast
import sqlite3
from importlib.machinery import SourceFileLoader
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.unit.test_migration_0043 import _downgrade, _normalized_schema, _tables, _upgrade
from tests.unit.test_migration_0045 import _column_names, _index_names, _insert_legacy_scope
from tests.unit.test_migration_0047 import (
    _insert_automation,
    _insert_chat_event,
    _insert_emoji_asset,
    _seed_people_and_groups,
    _seed_plugin,
)

from qq_ai_bot.identity.canonical_memory_schema import (
    C21_FACT_UNIQUE_INDEX_NAMES,
    C21_OWNERSHIP_COLUMNS,
    C21_OWNERSHIP_FOREIGN_KEYS,
    C21_OWNERSHIP_INDEXES,
    C21_REFLECTION_UNIQUE_INDEX_NAMES,
)
from qq_ai_bot.identity.db_models import CANONICAL_IDENTITY_TABLES
from qq_ai_bot.identity.legacy_fk_inventory import (
    LEGACY_CARRIER_FOREIGN_KEYS,
    LEGACY_CARRIER_PARENTS,
    LEGACY_CARRIER_REBUILD_TABLES,
)

_MIGRATION_PATH = Path("migrations/versions/0048_identity_cutover.py")
_CUTOVER_TABLES = ("identity_cutover_manifests", "identity_cutover_runs")
_NOW = "2026-08-24T00:00:00+00:00"
_CARRIER_DATA_TABLES = (
    "chat_events",
    "conversation_scopes",
    "emoji_assets",
    "memberships",
    "automations",
    "plugin_background_target_grants",
    "plugin_state",
    "memory_dream_runs",
)


def _foreign_parents(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[2]) for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')}


def _pragma_carrier_fks(
    connection: sqlite3.Connection, tables: tuple[str, ...]
) -> list[tuple[str, str, str, str, str, str]]:
    actual: list[tuple[str, str, str, str, str, str]] = []
    present = _tables(connection)
    for table in tables:
        if table not in present:
            continue
        for row in connection.execute(f'PRAGMA foreign_key_list("{table}")'):
            if row[2] in {"people", "groups"}:
                actual.append(
                    (table, str(row[3]), str(row[2]), str(row[4]), str(row[5]), str(row[6]))
                )
    return sorted(actual)


def _data_signature(path: Path, tables: tuple[str, ...]) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(path) as connection:
        present = _tables(connection)
        return {
            table: list(connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'))
            for table in tables
            if table in present
        }


def _schema_and_data_signature(path: Path) -> tuple[object, object, object]:
    with sqlite3.connect(path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    return (
        version,
        _normalized_schema(path),
        _data_signature(path, (*_CARRIER_DATA_TABLES, "people", "groups", *_CUTOVER_TABLES)),
    )


def _seed_representative_carrier_rows(connection: sqlite3.Connection) -> None:
    _seed_people_and_groups(connection)
    _seed_plugin(connection)
    _insert_legacy_scope(connection, scope_key="private:bot-1:peer-1", now=_NOW)
    connection.execute(
        "INSERT INTO chat_events("
        "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
        "sender_user_id, direction, content, visual_summary, segments_json, "
        "origin, occurred_at, observed_at"
        ") VALUES ('bot-1', 'phase-e-1', 'private', 'peer-1', 'peer-1', 'inbound', "
        "'phase-e-fts-token', '', '[]', 'user_message', ?, ?)",
        (_NOW, _NOW),
    )
    _insert_chat_event(connection, "phase-e-2")
    connection.execute(
        "INSERT INTO memberships(user_id, group_id, group_card, first_seen_at, last_seen_at) "
        "VALUES ('peer-1', '2001', '', ?, ?)",
        (_NOW, _NOW),
    )
    emoji_id = str(uuid4())
    _insert_emoji_asset(connection, emoji_id, "c" * 64)
    connection.execute(
        "UPDATE emoji_assets SET first_seen_user_id='peer-1', first_seen_group_id='2001' "
        "WHERE id=?",
        (emoji_id,),
    )
    _insert_automation(connection, "phase-e-auto")
    connection.execute(
        "INSERT INTO plugin_background_target_grants("
        "plugin_id, target_type, target_id, bot_user_id, enabled, "
        "created_by_user_id, created_at, updated_at"
        ") VALUES ('fixture', 'private', 'peer-1', 'bot-1', 1, 'peer-1', ?, ?)",
        (_NOW, _NOW),
    )
    connection.execute(
        "INSERT INTO plugin_state("
        "plugin_id, namespace, key, value_json, version, subject_user_id, updated_at"
        ") VALUES ('fixture', 'ns', 'k', '{}', 1, 'peer-1', ?)",
        (_NOW,),
    )
    connection.execute(
        "INSERT INTO memory_dream_runs("
        "public_id, mode, status, snapshot_max_fact_id, snapshot_created_at, "
        "created_by_user_id, statistics_json, created_at, updated_at"
        ") VALUES (?, 'full', 'planned', 0, ?, 'peer-1', '{}', ?, ?)",
        (str(uuid4()), _NOW, _NOW, _NOW),
    )


def test_alembic_heads_is_exactly_0048() -> None:
    config = Config("alembic.ini")
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["0048"]


def test_0048_is_self_contained_alembic() -> None:
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "qq_ai_bot" not in source
    assert "Base.metadata" not in source
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
    assert imported <= {"__future__", "re", "collections", "sqlalchemy", "alembic"}


def test_0005_still_has_historical_people_groups_fks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "at-0005.db"
    _upgrade(path, monkeypatch, "0005")
    with sqlite3.connect(path) as connection:
        assert not (set(_CUTOVER_TABLES) & _tables(connection))
        assert "people" in _foreign_parents(connection, "chat_events")
        assert "groups" in _foreign_parents(connection, "chat_events")
        assert "people" in _foreign_parents(connection, "memberships")
        assert "groups" in _foreign_parents(connection, "memberships")


def test_fresh_head_creates_cutover_tables_and_drops_carrier_fks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fresh-head.db"
    _upgrade(path, monkeypatch, "head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0048",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = _tables(connection)
        assert set(_CUTOVER_TABLES) <= tables
        assert set(CANONICAL_IDENTITY_TABLES) <= tables
        for table, _column, parent, _remote in LEGACY_CARRIER_FOREIGN_KEYS:
            if table not in tables:
                continue
            assert parent not in _foreign_parents(connection, table), (table, parent)
        index_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name='uq_chat_events_bot_platform_message'"
            ).fetchone()[0]
        )
        assert "canonical_event_id IS NULL" in index_sql
        assert "fingerprint" in _column_names(connection, "identity_cutover_manifests")
        assert "source_fingerprint" in _column_names(connection, "identity_cutover_runs")
        assert "canonical_conversation_rollups" in tables
        assert "canonical_conversation_rollup_jobs" in tables
        assert "conversation_rollup_emergency_overlays" in tables
        assert "canonical_conversation_rollup_emergency_overlays" in tables
        assert "summary_kind" in _column_names(connection, "canonical_conversation_rollups")
        assert "signal_revision" in _column_names(connection, "canonical_conversation_rollup_jobs")
        assert "base_semantic_revision" in _column_names(
            connection, "conversation_rollup_emergency_overlays"
        )
        assert "base_semantic_revision" in _column_names(
            connection, "canonical_conversation_rollup_emergency_overlays"
        )
        assert "uq_chat_events_bot_platform_message" in _index_names(connection, "chat_events")


def test_0042_to_head_matches_fresh_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = tmp_path / "fresh.db"
    upgraded = tmp_path / "from-0042.db"
    _upgrade(fresh, monkeypatch, "head")
    _upgrade(upgraded, monkeypatch, "0042")
    _upgrade(upgraded, monkeypatch, "head")
    assert _normalized_schema(fresh) == _normalized_schema(upgraded)
    with sqlite3.connect(fresh) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_downgrade_0048_restores_0047_carrier_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "downgrade.db"
    _upgrade(path, monkeypatch, "head")
    _downgrade(path, monkeypatch, "0047")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0047",)
        tables = _tables(connection)
        assert not (set(_CUTOVER_TABLES) & tables)
        assert "chat_events" in tables
        assert "people" in _foreign_parents(connection, "chat_events")
        assert "groups" in _foreign_parents(connection, "chat_events")
        create_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_events'"
            ).fetchone()[0]
        )
        assert "CONSTRAINT uq_chat_events_bot_platform_message UNIQUE" in create_sql
        assert "canonical_event_id IS NULL" not in create_sql
        unique_index = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='uq_chat_events_bot_platform_message'"
        ).fetchone()
        assert unique_index is None


def test_fresh_0048_downgrade_matches_baseline_0047(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tmp_path / "baseline-0047.db"
    roundtrip = tmp_path / "fresh-0048-down.db"
    _upgrade(baseline, monkeypatch, "0047")
    _upgrade(roundtrip, monkeypatch, "head")
    _downgrade(roundtrip, monkeypatch, "0047")
    assert _normalized_schema(baseline) == _normalized_schema(roundtrip)
    with sqlite3.connect(roundtrip) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0047",)


def test_0042_head_downgrade_matches_baseline_0047(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tmp_path / "baseline-0047.db"
    from_0042 = tmp_path / "from-0042-down.db"
    _upgrade(baseline, monkeypatch, "0047")
    _upgrade(from_0042, monkeypatch, "0042")
    _upgrade(from_0042, monkeypatch, "head")
    _downgrade(from_0042, monkeypatch, "0047")
    assert _normalized_schema(baseline) == _normalized_schema(from_0042)
    with sqlite3.connect(from_0042) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_0048_rebuilds_historical_chat_events_fts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "upgrade-fts.db"
    historical_token = "upgrade-fts-hist-token"
    live_token = "upgrade-fts-live-token"
    _upgrade(path, monkeypatch, "0047")
    with sqlite3.connect(path) as connection:
        _seed_people_and_groups(connection)
        connection.execute(
            "INSERT INTO chat_events("
            "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
            "sender_user_id, direction, content, visual_summary, segments_json, "
            "origin, occurred_at, observed_at"
            ") VALUES ('bot-1', 'fts-hist-1', 'private', 'peer-1', 'peer-1', 'inbound', "
            "?, '', '[]', 'user_message', ?, ?)",
            (historical_token, _NOW, _NOW),
        )
        connection.commit()
    _upgrade(path, monkeypatch, "head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0048",)
        historical = list(
            connection.execute(
                "SELECT rowid FROM chat_events_fts "
                "WHERE chat_events_fts MATCH '\"upgrade-fts-hist-token\"'"
            )
        )
        assert historical
        fts_rows = list(
            connection.execute("SELECT rowid, content FROM chat_events_fts ORDER BY rowid")
        )
        events = list(connection.execute("SELECT id, content FROM chat_events ORDER BY id"))
        assert fts_rows == events
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='trigger' AND name LIKE 'chat_events_fts_%'"
            )
        }
        assert triggers == {
            "chat_events_fts_ai",
            "chat_events_fts_ad",
            "chat_events_fts_au",
        }
        connection.execute(
            "INSERT INTO chat_events("
            "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
            "sender_user_id, direction, content, visual_summary, segments_json, "
            "origin, occurred_at, observed_at"
            ") VALUES ('bot-1', 'fts-live-1', 'private', 'peer-1', 'peer-1', 'inbound', "
            "?, '', '[]', 'user_message', ?, ?)",
            (live_token, _NOW, _NOW),
        )
        connection.commit()
        live = list(
            connection.execute(
                "SELECT rowid FROM chat_events_fts "
                "WHERE chat_events_fts MATCH '\"upgrade-fts-live-token\"'"
            )
        )
        assert live
        fts_after = list(
            connection.execute("SELECT rowid, content FROM chat_events_fts ORDER BY rowid")
        )
        events_after = list(connection.execute("SELECT id, content FROM chat_events ORDER BY id"))
        assert fts_after == events_after


def test_carrier_rows_survive_upgrade_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "carrier-roundtrip.db"
    _upgrade(path, monkeypatch, "0047")
    with sqlite3.connect(path) as connection:
        _seed_representative_carrier_rows(connection)
        connection.commit()
        before = {
            table: list(connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'))
            for table in _CARRIER_DATA_TABLES
        }
        fts_before = list(
            connection.execute("SELECT rowid, content FROM chat_events_fts ORDER BY rowid")
        )
    _upgrade(path, monkeypatch, "head")
    _downgrade(path, monkeypatch, "0047")
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        after = {
            table: list(connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'))
            for table in _CARRIER_DATA_TABLES
        }
        assert after == before
        fts_after = list(
            connection.execute("SELECT rowid, content FROM chat_events_fts ORDER BY rowid")
        )
        events = list(connection.execute("SELECT id, content FROM chat_events ORDER BY id"))
        assert fts_after == events
        assert fts_after == fts_before
        matched = list(
            connection.execute(
                "SELECT rowid FROM chat_events_fts "
                "WHERE chat_events_fts MATCH '\"phase-e-fts-token\"'"
            )
        )
        assert matched
        create_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_events'"
            ).fetchone()[0]
        )
        assert "CONSTRAINT uq_chat_events_bot_platform_message UNIQUE" in create_sql
        assert "(bot_user_id, platform_message_id)" in create_sql
        assert "canonical_event_id IS NULL" not in create_sql


def test_frozen_inverse_matches_0047_pragma_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = SourceFileLoader("revision_0048_inventory", str(_MIGRATION_PATH)).load_module()
    path = tmp_path / "at-0047.db"
    _upgrade(path, monkeypatch, "0047")
    with sqlite3.connect(path) as connection:
        actual = _pragma_carrier_fks(connection, loaded._REBUILD_TABLES)
    frozen = sorted(
        (table, local, parent, remote, on_update, on_delete)
        for table, local, parent, remote, on_update, on_delete, _clause in (
            loaded._CARRIER_FOREIGN_KEYS
        )
    )
    assert actual == frozen
    assert len(frozen) == 34
    assert {item[0] for item in loaded._CARRIER_FOREIGN_KEYS} == set(loaded._REBUILD_TABLES)


def test_v2_runtime_state_refuses_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "v2-refuse.db"
    _upgrade(path, monkeypatch, "head")
    with sqlite3.connect(path) as connection:
        _seed_people_and_groups(connection)
        _insert_chat_event(connection, "v2-keep")
        connection.execute(
            "UPDATE identity_runtime_state SET state='v2', cutover_id=?, "
            "source_fingerprint=?, completed_at=? WHERE id=1",
            (str(uuid4()), "a" * 64, _NOW),
        )
        connection.commit()
    before = _schema_and_data_signature(path)
    with pytest.raises(Exception, match="0048 downgrade blocked"):
        _downgrade(path, monkeypatch, "0047")
    assert _schema_and_data_signature(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0048",)
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone() == ("v2",)


def test_incompatible_carrier_rows_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing-parent.db"
    _upgrade(missing, monkeypatch, "head")
    with sqlite3.connect(missing) as connection:
        _seed_people_and_groups(connection)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO chat_events("
            "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
            "sender_user_id, direction, content, visual_summary, segments_json, "
            "origin, occurred_at, observed_at"
            ") VALUES ('bot-1', 'orphan-1', 'private', 'ghost-user', 'ghost-user', "
            "'inbound', 'orphan', '', '[]', 'user_message', ?, ?)",
            (_NOW, _NOW),
        )
        connection.execute("PRAGMA foreign_keys=ON")
        connection.commit()
    missing_before = _schema_and_data_signature(missing)
    with pytest.raises(Exception, match="0048 downgrade blocked"):
        _downgrade(missing, monkeypatch, "0047")
    assert _schema_and_data_signature(missing) == missing_before

    duplicate = tmp_path / "canonical-duplicate.db"
    _upgrade(duplicate, monkeypatch, "head")
    with sqlite3.connect(duplicate) as connection:
        _seed_people_and_groups(connection)
        for suffix in ("a", "b"):
            connection.execute(
                "INSERT INTO chat_events("
                "bot_user_id, platform_message_id, scope_type, private_peer_user_id, "
                "sender_user_id, direction, content, visual_summary, segments_json, "
                "origin, occurred_at, observed_at, canonical_event_id"
                ") VALUES ('bot-1', 'dup-legacy', 'private', 'peer-1', 'peer-1', "
                "'inbound', ?, '', '[]', 'user_message', ?, ?, ?)",
                (f"dup-{suffix}", _NOW, _NOW, str(uuid4())),
            )
        connection.commit()
    duplicate_before = _schema_and_data_signature(duplicate)
    with pytest.raises(Exception, match="0048 downgrade blocked"):
        _downgrade(duplicate, monkeypatch, "0047")
    assert _schema_and_data_signature(duplicate) == duplicate_before


def test_strip_legacy_fk_sql_keeps_valid_create_table() -> None:
    loaded = SourceFileLoader("revision_0048_strip", str(_MIGRATION_PATH)).load_module()
    source = (
        'CREATE TABLE "emoji_assets" ('
        "id VARCHAR(36) NOT NULL, "
        "first_seen_user_id VARCHAR(64) REFERENCES people(user_id) ON DELETE SET NULL, "
        "first_seen_group_id VARCHAR(64) REFERENCES groups(group_id) ON UPDATE CASCADE, "
        "source_event_id INTEGER, "
        "PRIMARY KEY (id), "
        "CHECK (status IN ('candidate', 'missing')), "
        "FOREIGN KEY(creator_user_id) REFERENCES people (user_id) ON DELETE CASCADE, "
        "FOREIGN KEY(source_event_id) REFERENCES chat_events (id) ON DELETE SET NULL"
        ")"
    )
    stripped = loaded._strip_legacy_fk_sql(source)
    assert "REFERENCES people" not in stripped
    assert "REFERENCES groups" not in stripped
    assert "FOREIGN KEY(source_event_id) REFERENCES chat_events" in stripped
    assert "NULL NULL" not in stripped
    assert stripped.count("(") == stripped.count(")")


def test_rebuild_inventory_matches_0048_literals() -> None:
    loaded = SourceFileLoader("revision_0048", str(_MIGRATION_PATH)).load_module()
    assert set(loaded._REBUILD_TABLES) == set(LEGACY_CARRIER_REBUILD_TABLES)
    assert LEGACY_CARRIER_PARENTS == frozenset({"people", "groups"})
    assert {
        (table, local, parent, remote)
        for table, local, parent, remote, _on_update, _on_delete, _clause in (
            loaded._CARRIER_FOREIGN_KEYS
        )
    } == set(LEGACY_CARRIER_FOREIGN_KEYS)


def test_0048_rebuild_preserves_c21_memory_owner_columns_and_fks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "c21-rebuild.db"
    _upgrade(path, monkeypatch, "head")
    expected_fks = {
        (table, parent, column, parent_column, "RESTRICT", "RESTRICT", "NONE")
        for table, column, parent, parent_column in C21_OWNERSHIP_FOREIGN_KEYS
        if table == "memory_tool_receipts"
    }
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        columns = set(_column_names(connection, "memory_tool_receipts"))
        assert {"canonical_person_id", "canonical_space_id"} <= columns
        receipt_fks = {
            (table, row[2], row[3], row[4], row[5], row[6], row[7])
            for table in ("memory_tool_receipts",)
            for row in connection.execute('PRAGMA foreign_key_list("memory_tool_receipts")')
            if str(row[3]) in C21_OWNERSHIP_COLUMNS["memory_tool_receipts"]
        }
        assert receipt_fks == expected_fks
        assert "people" not in {
            str(row[2])
            for row in connection.execute('PRAGMA foreign_key_list("memory_tool_receipts")')
        }
        fact_indexes = set(_index_names(connection, "memory_facts"))
        assert set(C21_FACT_UNIQUE_INDEX_NAMES) <= fact_indexes
        assert {
            "uq_memory_facts_active_person_key",
            "uq_memory_facts_active_person_group_key",
            "uq_memory_facts_active_group_key",
            "uq_memory_facts_active_self_key",
        } <= fact_indexes
        job_columns = set(_column_names(connection, "memory_jobs"))
        assert {"canonical_person_id", "canonical_space_id"} <= job_columns
        assert set(C21_OWNERSHIP_INDEXES) <= set().union(
            *(
                _index_names(connection, table)
                for table in (
                    "memory_jobs",
                    "memory_tool_receipts",
                    "memory_self_reflection_states",
                    "memory_self_reflection_runs",
                    "memory_dream_clusters",
                )
            )
        )
        assert set(C21_REFLECTION_UNIQUE_INDEX_NAMES) <= set().union(
            _index_names(connection, "memory_self_reflection_states"),
            _index_names(connection, "memory_self_reflection_runs"),
        )
        dream_columns = set(_column_names(connection, "memory_dream_clusters"))
        assert set(C21_OWNERSHIP_COLUMNS["memory_dream_clusters"]) <= dream_columns


def test_0005_excludes_cutover_tables_as_literals() -> None:
    source = Path("migrations/versions/0005_person_centric_v1.py").read_text(encoding="utf-8")
    assert "qq_ai_bot.identity" not in source
    assert '"identity_cutover_manifests"' in source
    assert '"identity_cutover_runs"' in source
    assert '"canonical_conversation_rollups"' in source
    assert '"canonical_conversation_rollup_jobs"' in source
    assert '"conversation_rollup_emergency_overlays"' in source
    assert '"canonical_conversation_rollup_emergency_overlays"' in source
