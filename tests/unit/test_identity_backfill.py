"""C7 audited identity backfill preflight."""

from __future__ import annotations

import argparse
import ast
import sqlite3
import threading
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, event
from tests.support.identity_erase import (
    assert_test_database,
    erase_canonical_identity_backfill,
)
from tests.unit.test_migration_0043 import _upgrade
from tests.unit.test_migration_0046 import _enable_sqlite_fk

from qq_ai_bot.cli import _add_identity_parser, _identity_command
from qq_ai_bot.config import Settings
from qq_ai_bot.identity.backfill_repository import (
    Failpoint,
    IdentityBackfillRepository,
    connect_sqlite,
    sqlite_path_from_url,
)
from qq_ai_bot.identity.backfill_service import (
    EXIT_CONFLICTS,
    EXIT_ERROR,
    EXIT_OK,
    IdentityBackfillService,
)
from qq_ai_bot.identity.backfill_types import AccountEvidence, BackfillSettingsInput
from qq_ai_bot.identity.classifier import classify_account
from qq_ai_bot.identity.inventory import (
    ACCOUNT_SOURCE_INVENTORY,
    DEFERRED_SHADOWS,
    FILLABLE_SHADOWS,
    HUMAN_PLUGIN_MESSAGE_ROLES,
    IDENTITY_PLATFORM,
    REQUIRED_C7_SCHEMA,
    SHADOW_FILL_SPECS,
    SHAPE_ONLY_OPTIONAL_SHADOWS,
    SPACE_SOURCE_INVENTORY,
    STRONG_PERSON_SOURCES,
    WEAK_PERSON_SOURCES,
    shadow_inventory_drift,
    shadow_spec_policy_errors,
)
from qq_ai_bot.identity.reporting import render_report
from qq_ai_bot.persistence.metadata import Base

_NOW = "2026-08-24T00:00:00+00:00"
_CUTOVER_ID = "550e8400-e29b-41d4-a716-446655440099"
_SRC = Path("src/qq_ai_bot")
_IDENTITY_SRC = _SRC / "identity"
_FORBIDDEN_BUSINESS_IMPORTS = {
    "argparse",
    "qq_ai_bot.cli",
    "qq_ai_bot.identity.reporting",
    "qq_ai_bot.identity.db_models",
    "sqlalchemy.orm",
}


def _create_schema(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    event.listen(engine, "connect", _enable_sqlite_fk)
    Base.metadata.create_all(engine)
    engine.dispose()


def _settings(
    *,
    superusers: tuple[str, ...] = (),
    enabled_groups: tuple[str, ...] = (),
    ignored_bots: tuple[str, ...] = (),
) -> BackfillSettingsInput:
    return BackfillSettingsInput(
        superusers=frozenset(superusers),
        enabled_groups=frozenset(enabled_groups),
        ignored_bot_users=frozenset(ignored_bots),
    )


def _service(
    path: Path,
    settings: BackfillSettingsInput,
    failpoint: Failpoint | None = None,
) -> IdentityBackfillService:
    return IdentityBackfillService(path, settings, failpoint=failpoint)


def _open(path: Path) -> sqlite3.Connection:
    connection = connect_sqlite(path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _insert_people(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    nickname: str = "",
    is_bot: int = 0,
) -> None:
    connection.execute(
        "INSERT INTO people(user_id, nickname, enabled, is_bot, first_seen_at, last_seen_at) "
        "VALUES (?, ?, 1, ?, ?, ?)",
        (user_id, nickname, is_bot, _NOW, _NOW),
    )


def _insert_group(
    connection: sqlite3.Connection,
    group_id: str,
    *,
    name: str = "room",
) -> None:
    connection.execute(
        "INSERT INTO groups("
        "group_id, name, enabled, require_mention, autonomous_enabled, "
        "first_seen_at, last_seen_at, updated_at"
        ") VALUES (?, ?, 1, 1, 1, ?, ?, ?)",
        (group_id, name, _NOW, _NOW, _NOW),
    )


def _insert_membership(connection: sqlite3.Connection, user_id: str, group_id: str) -> None:
    connection.execute(
        "INSERT INTO memberships(user_id, group_id, group_card, first_seen_at, last_seen_at) "
        "VALUES (?, ?, '', ?, ?)",
        (user_id, group_id, _NOW, _NOW),
    )


def _insert_event(
    connection: sqlite3.Connection,
    *,
    bot: str,
    sender: str,
    peer: str | None = None,
    group: str | None = None,
    message_id: str = "m1",
) -> None:
    scope = "group" if group else "private"
    connection.execute(
        "INSERT INTO chat_events("
        "bot_user_id, platform_message_id, scope_type, group_id, private_peer_user_id, "
        "sender_user_id, direction, content, visual_summary, segments_json, "
        "origin, occurred_at, observed_at"
        ") VALUES (?, ?, ?, ?, ?, ?, 'inbound', 'secret-body', '', '[]', "
        "'user_message', ?, ?)",
        (bot, message_id, scope, group, peer, sender, _NOW, _NOW),
    )


def _insert_person(connection: sqlite3.Connection, person_id: str) -> None:
    connection.execute(
        "INSERT INTO persons(id, enabled, revision, created_at, updated_at) VALUES (?, 1, 1, ?, ?)",
        (person_id, _NOW, _NOW),
    )


def _insert_binding(
    connection: sqlite3.Connection,
    *,
    binding_id: str,
    person_id: str,
    external_id: str,
    display_name: str = "",
) -> None:
    connection.execute(
        "INSERT INTO identity_bindings("
        "id, person_id, platform, external_account_id, display_name, "
        "status, revision, created_at, updated_at"
        ") VALUES (?, ?, 'qq', ?, ?, 'active', 1, ?, ?)",
        (binding_id, person_id, external_id, display_name, _NOW, _NOW),
    )


def _insert_presence(
    connection: sqlite3.Connection,
    *,
    presence_id: str,
    external_id: str,
) -> None:
    connection.execute(
        "INSERT INTO presences("
        "id, platform, external_account_id, enabled, ingest_eligible, "
        "revision, created_at, updated_at"
        ") VALUES (?, 'qq', ?, 1, 1, 1, ?, ?)",
        (presence_id, external_id, _NOW, _NOW),
    )


def _insert_space_binding(
    connection: sqlite3.Connection,
    *,
    binding_id: str,
    space_id: str,
    external_id: str,
    display_name: str = "preconfigured",
) -> None:
    connection.execute(
        "INSERT INTO spaces("
        "id, name, enabled, autonomous_enabled, require_mention, "
        "revision, created_at, updated_at"
        ") VALUES (?, ?, 1, 1, 1, 1, ?, ?)",
        (space_id, display_name, _NOW, _NOW),
    )
    connection.execute(
        "INSERT INTO space_bindings("
        "id, space_id, platform, external_space_id, display_name, "
        "status, revision, created_at, updated_at"
        ") VALUES (?, ?, 'qq', ?, ?, 'active', 1, ?, ?)",
        (binding_id, space_id, external_id, display_name, _NOW, _NOW),
    )


def _flip_runtime_v2(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE identity_runtime_state SET state = 'v2', cutover_id = ?, "
        "source_fingerprint = 'cutover-fingerprint', completed_at = ? WHERE id = 1",
        (_CUTOVER_ID, _NOW),
    )


def _seed_standard(connection: sqlite3.Connection) -> None:
    _insert_people(connection, "8000", nickname="Yuki", is_bot=1)
    _insert_people(connection, "1001", nickname="Ada")
    _insert_people(connection, "7777", nickname="OtherBot", is_bot=1)
    _insert_group(connection, "2001", name="hall")
    _insert_membership(connection, "1001", "2001")
    _insert_event(
        connection,
        bot="8000",
        sender="1001",
        group="2001",
        message_id="g-1",
    )
    _insert_event(
        connection,
        bot="8000",
        sender="1001",
        peer="1001",
        message_id="p-1",
    )


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_inventory_covers_required_sources() -> None:
    account_keys = {item[0] for item in ACCOUNT_SOURCE_INVENTORY}
    space_keys = {item[0] for item in SPACE_SOURCE_INVENTORY}
    assert "settings.superusers" in account_keys
    assert "settings.ignored_bot_users" in account_keys
    assert "chat_events.bot_user_id" in account_keys
    assert "people.user_id" in account_keys
    assert "identity_bindings.external_account_id" in account_keys
    assert "presences.external_account_id" in account_keys
    assert "space_bindings.external_space_id" in space_keys
    assert "settings.enabled_groups" in space_keys
    assert "groups.group_id" in space_keys
    assert IDENTITY_PLATFORM == "qq"
    assert "settings.superusers" in STRONG_PERSON_SOURCES
    assert "memberships.user_id" in WEAK_PERSON_SOURCES
    assert HUMAN_PLUGIN_MESSAGE_ROLES == frozenset({"user"})
    assert "identity_runtime_state" in REQUIRED_C7_SCHEMA
    assert "plugin_agent_messages" in REQUIRED_C7_SCHEMA
    assert "role" in REQUIRED_C7_SCHEMA["plugin_agent_messages"]
    assert any("canonical_conversation_id" in item[0] for item in DEFERRED_SHADOWS)
    assert any(item[0] == "people.canonical_person_id" for item in FILLABLE_SHADOWS)


def test_shadow_fill_specs_cover_fillable_inventory() -> None:
    missing, extra = shadow_inventory_drift()
    assert not missing, sorted(missing)
    assert not extra, sorted(extra)
    fillable_kinds = {item[0]: item[1] for item in FILLABLE_SHADOWS}
    assert {spec.dotted: spec.kind for spec in SHADOW_FILL_SPECS} == fillable_kinds
    assert shadow_spec_policy_errors() == ()
    assert SHAPE_ONLY_OPTIONAL_SHADOWS == {
        "automations.canonical_target_person_id",
        "automations.canonical_target_space_id",
        "runtime_turn_observations.canonical_person_id",
        "runtime_turn_observations.canonical_space_id",
    }
    assert all(
        spec.source_column is None
        for spec in SHADOW_FILL_SPECS
        if spec.completeness == "shape_only_optional"
    )
    assert all(
        spec.source_column is not None
        for spec in SHADOW_FILL_SPECS
        if spec.completeness == "verified_from_source"
    )


def test_business_modules_do_not_import_cli_renderer_or_orm() -> None:
    for name in (
        "inventory.py",
        "classifier.py",
        "backfill_types.py",
        "sanitize.py",
        "backfill_repository.py",
        "backfill_service.py",
        "canonical_memory_owners.py",
        "c21_readable_owners.py",
        "errors.py",
    ):
        modules = _imported_modules(_IDENTITY_SRC / name)
        assert not (modules & _FORBIDDEN_BUSINESS_IMPORTS), (name, modules)


def test_production_has_no_destructive_identity_erase() -> None:
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "identity" not in text and "backfill" not in text:
            continue
        assert "--erase" not in text
        assert "--reset" not in text
        assert "erase_canonical_identity_backfill" not in text


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({"yuki_self": True}, "yuki_presence"),
        ({"ignored_bot": True}, "external_bot"),
        ({"legacy_is_bot": True}, "external_bot"),
        ({"superuser": True}, "person"),
        ({"human_sender": True}, "person"),
        ({"private_peer": True}, "person"),
        ({"member": True}, "person"),
        ({"people_human": True}, "person"),
        ({"yuki_self": True, "legacy_is_bot": True}, "yuki_presence"),
        ({"yuki_self": True, "superuser": True}, "conflict"),
        ({"ignored_bot": True, "superuser": True}, "conflict"),
        ({"yuki_self": True, "ignored_bot": True}, "conflict"),
        ({"ignored_bot": True, "human_sender": True, "member": True}, "external_bot"),
        ({"legacy_is_bot": True, "human_sender": True, "member": True}, "external_bot"),
        ({"yuki_self": True, "member": True, "human_sender": True}, "yuki_presence"),
        ({"yuki_self": True, "people_human": True}, "conflict"),
        ({"ignored_bot": True, "people_human": True}, "conflict"),
        ({"ignored_bot": True, "strong_person": True}, "conflict"),
        ({"ignored_bot": True, "supporting_person": True}, "external_bot"),
    ],
)
def test_classifier_rules(flags: dict[str, bool], expected: str) -> None:
    evidence = AccountEvidence(
        external_id="1",
        sources=frozenset({"test"}),
        yuki_self=flags.get("yuki_self", False),
        ignored_bot=flags.get("ignored_bot", False),
        legacy_is_bot=flags.get("legacy_is_bot", False),
        human_sender=flags.get("human_sender", False),
        private_peer=flags.get("private_peer", False),
        member=flags.get("member", False),
        superuser=flags.get("superuser", False),
        people_human=flags.get("people_human", False),
        supporting_person=flags.get("supporting_person", False),
        strong_person=flags.get("strong_person", False),
        nickname="",
        existing_person_id=None,
        existing_binding_person_id=None,
        existing_presence_id=None,
        shadow_person_ids=frozenset(),
        shadow_presence_ids=frozenset(),
    )
    classification, _category = classify_account(evidence)
    assert classification == expected


def test_yuki_and_ignored_bot_are_not_persons(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    _create_schema(path)
    with _open(path) as connection:
        _seed_standard(connection)
        connection.commit()
    report = _service(
        path,
        _settings(superusers=("9000",), enabled_groups=("2002",), ignored_bots=("7777",)),
    ).apply()
    assert report.status == "succeeded"
    assert report.counts.person_class == 2
    assert report.counts.yuki_presence_class == 1
    assert report.counts.external_bot_class == 1
    with _open(path) as connection:
        people = {
            str(row[0]): row[1]
            for row in connection.execute("SELECT user_id, canonical_person_id FROM people")
        }
        assert people["8000"] is None
        assert people["7777"] is None
        assert people["1001"] is not None
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM identity_bindings WHERE external_account_id = '9000'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM people WHERE user_id = '9000'").fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM space_bindings WHERE external_space_id = '2002'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM groups WHERE group_id = '2002'").fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM presences WHERE external_account_id = '8000'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM identity_bindings "
                "WHERE external_account_id IN ('8000', '7777')"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        repo = IdentityBackfillRepository(path)
        assert repo.conversation_shadows_populated(connection) == 0


def test_dry_run_is_zero_write(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    _create_schema(path)
    with _open(path) as connection:
        _seed_standard(connection)
        connection.commit()
        repo = IdentityBackfillRepository(path)
        before_schema = repo.schema_signature(connection)
        before_business = repo.business_signature(connection)
        before_runs = repo.count_rows(connection, "identity_backfill_runs")
        before_conflicts = repo.count_rows(connection, "identity_conflicts")
        before_changes = int(connection.total_changes)
        settings = _settings(ignored_bots=("7777",))
        snapshot = repo.load_snapshot(connection, settings)
        after_changes = int(connection.total_changes)
        assert after_changes == before_changes
        assert snapshot[-1]
    report = _service(path, _settings(ignored_bots=("7777",))).dry_run()
    assert report.status == "succeeded"
    assert report.run_recorded is False
    assert report.business_diff == 0
    rendered = render_report(report, "json")
    assert "8000" not in rendered
    assert "1001" not in rendered
    assert "secret-body" not in rendered
    assert str(path) not in rendered
    assert "secret" not in rendered.casefold()
    with _open(path) as connection:
        repo = IdentityBackfillRepository(path)
        assert repo.schema_signature(connection) == before_schema
        assert repo.business_signature(connection) == before_business
        assert repo.count_rows(connection, "identity_backfill_runs") == before_runs
        assert repo.count_rows(connection, "identity_conflicts") == before_conflicts


def test_second_apply_is_business_noop(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    _create_schema(path)
    with _open(path) as connection:
        _seed_standard(connection)
        connection.commit()
    settings = _settings(superusers=("9000",), enabled_groups=("2002",), ignored_bots=("7777",))
    first = _service(path, settings).apply()
    assert first.status == "succeeded"
    assert first.business_diff == 1
    with _open(path) as connection:
        repo = IdentityBackfillRepository(path)
        signature = repo.business_signature(connection)
        stamps = connection.execute(
            "SELECT id, revision, updated_at FROM persons UNION ALL "
            "SELECT id, revision, updated_at FROM identity_bindings UNION ALL "
            "SELECT id, revision, updated_at FROM spaces UNION ALL "
            "SELECT id, revision, updated_at FROM space_bindings UNION ALL "
            "SELECT id, revision, updated_at FROM presences"
        ).fetchall()
        runs_before = repo.count_rows(connection, "identity_backfill_runs")
    second = _service(path, settings).apply()
    assert second.status == "succeeded"
    assert second.business_diff == 0
    assert second.run_recorded is True
    with _open(path) as connection:
        repo = IdentityBackfillRepository(path)
        assert repo.business_signature(connection) == signature
        assert (
            connection.execute(
                "SELECT id, revision, updated_at FROM persons UNION ALL "
                "SELECT id, revision, updated_at FROM identity_bindings UNION ALL "
                "SELECT id, revision, updated_at FROM spaces UNION ALL "
                "SELECT id, revision, updated_at FROM space_bindings UNION ALL "
                "SELECT id, revision, updated_at FROM presences"
            ).fetchall()
            == stamps
        )
        assert repo.count_rows(connection, "identity_backfill_runs") == runs_before + 1
        assert repo.count_rows(connection, "identity_conflicts") == 0


def test_same_account_yuki_and_human_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    _create_schema(path)
    with _open(path) as connection:
        _insert_people(connection, "8000", is_bot=0)
        _insert_people(connection, "1001")
        _insert_group(connection, "2001")
        _insert_membership(connection, "8000", "2001")
        _insert_event(connection, bot="8000", sender="1001", group="2001")
        connection.commit()
    report = _service(path, _settings()).apply()
    assert report.status == "conflicted"
    assert report.counts.conflicts >= 1
    assert report.counts.processed >= 1
    assert {item.error_category for item in report.conflicts} >= {"yuki_and_person"}
    rendered = render_report(report, "json")
    assert "8000" not in rendered
    with _open(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM presences").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity_backfill_runs").fetchone()[0] == 1
        assert tuple(
            connection.execute(
                "SELECT status, error_category FROM identity_backfill_runs"
            ).fetchone()
        ) == ("failed", "identity_conflict")
        assert connection.execute("SELECT COUNT(*) FROM identity_conflicts").fetchone()[0] == 1


def test_preconfigured_owner_mismatch_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    _create_schema(path)
    person_a = "550e8400-e29b-41d4-a716-446655440000"
    person_b = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
    with _open(path) as connection:
        _insert_people(connection, "1001")
        connection.execute(
            "INSERT INTO persons(id, enabled, revision, created_at, updated_at) "
            "VALUES (?, 1, 1, ?, ?), (?, 1, 1, ?, ?)",
            (person_a, _NOW, _NOW, person_b, _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO identity_bindings("
            "id, person_id, platform, external_account_id, display_name, "
            "status, revision, created_at, updated_at"
            ") VALUES (?, ?, 'qq', '1001', '', 'active', 1, ?, ?)",
            ("7ba7b810-9dad-41d1-80b4-00c04fd430c8", person_a, _NOW, _NOW),
        )
        connection.execute(
            "UPDATE people SET canonical_person_id = ? WHERE user_id = '1001'",
            (person_b,),
        )
        connection.commit()
    report = _service(path, _settings()).apply()
    assert report.status == "conflicted"
    assert {item.error_category for item in report.conflicts} & {
        "canonical_owner_mismatch",
        "populated_merge_forbidden",
    }
    with _open(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM identity_bindings").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 2


def test_preconfigured_binding_is_reused(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    _create_schema(path)
    person_id = "550e8400-e29b-41d4-a716-446655440000"
    binding_id = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
    with _open(path) as connection:
        _insert_people(connection, "1001", nickname="Ada")
        connection.execute(
            "INSERT INTO persons(id, enabled, revision, created_at, updated_at) "
            "VALUES (?, 1, 1, ?, ?)",
            (person_id, _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO identity_bindings("
            "id, person_id, platform, external_account_id, display_name, "
            "status, revision, created_at, updated_at"
            ") VALUES (?, ?, 'qq', '1001', 'Ada', 'active', 1, ?, ?)",
            (binding_id, person_id, _NOW, _NOW),
        )
        connection.commit()
    report = _service(path, _settings()).apply()
    assert report.status == "succeeded"
    with _open(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 1
        assert tuple(
            connection.execute(
                "SELECT person_id, id FROM identity_bindings WHERE external_account_id = '1001'"
            ).fetchone()
        ) == (person_id, binding_id)
        assert (
            connection.execute(
                "SELECT canonical_person_id FROM people WHERE user_id = '1001'"
            ).fetchone()[0]
            == person_id
        )


def test_exception_rolls_back_business_rows(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    _create_schema(path)
    with _open(path) as connection:
        _seed_standard(connection)
        connection.commit()

    def boom(name: str) -> None:
        if name == "after_foundation_writes":
            raise RuntimeError("failpoint")

    with pytest.raises(RuntimeError, match="failpoint"):
        _service(path, _settings(ignored_bots=("7777",)), failpoint=boom).apply()
    with _open(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM presences").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM people WHERE canonical_person_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert tuple(
            connection.execute(
                "SELECT status, error_category FROM identity_backfill_runs"
            ).fetchone()
        ) == ("failed", "apply_aborted")


def test_concurrent_apply_is_serialized(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    _create_schema(path)
    with _open(path) as connection:
        _seed_standard(connection)
        connection.commit()
    settings = _settings(ignored_bots=("7777",))
    errors: list[BaseException] = []
    reports = []

    def worker() -> None:
        try:
            reports.append(_service(path, settings).apply())
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    second.start()
    first.join()
    second.join()
    assert errors == []
    assert len(reports) == 2
    assert all(item.status == "succeeded" for item in reports)
    with _open(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM presences").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM identity_backfill_runs").fetchone()[0] == 2


def test_erase_then_reapply_is_naturally_equivalent(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    _create_schema(path)
    with _open(path) as connection:
        _seed_standard(connection)
        connection.commit()
    settings = _settings(superusers=("9000",), enabled_groups=("2002",), ignored_bots=("7777",))
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    _service(path, settings).apply()
    with _open(path) as connection:
        before_legacy = connection.execute(
            "SELECT user_id, nickname, is_bot FROM people ORDER BY user_id"
        ).fetchall()
        projection = IdentityBackfillRepository(path).natural_key_projection(connection)
        first_person = connection.execute("SELECT id FROM persons ORDER BY id").fetchone()
    erase_canonical_identity_backfill(url)
    with _open(path) as connection:
        assert (
            connection.execute(
                "SELECT user_id, nickname, is_bot FROM people ORDER BY user_id"
            ).fetchall()
            == before_legacy
        )
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM people WHERE canonical_person_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
    _service(path, settings).apply()
    with _open(path) as connection:
        after = IdentityBackfillRepository(path).natural_key_projection(connection)
        assert after == projection
        second_person = connection.execute("SELECT id FROM persons ORDER BY id").fetchone()
        assert first_person is not None and second_person is not None
        assert UUID(str(second_person[0])).version == 4


def test_erase_refuses_non_test_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "qq_ai_bot.db"
    path.write_bytes(b"")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(RuntimeError, match="pytest"):
        assert_test_database(path)


def test_cli_dry_run_and_conflict_exit_codes(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    _create_schema(path)
    with _open(path) as connection:
        _insert_people(connection, "8000", is_bot=0)
        _insert_people(connection, "1001")
        _insert_group(connection, "2001")
        _insert_membership(connection, "8000", "2001")
        _insert_event(connection, bot="8000", sender="1001", group="2001")
        connection.commit()
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{path.as_posix()}",
        superusers_csv="",
        enabled_groups_csv="",
        ignored_bot_users_csv="",
    )
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    _add_identity_parser(sub)
    dry = parser.parse_args(
        ["identity", "backfill", "--dry-run", "--database-url", settings.database_url]
    )
    apply_args = parser.parse_args(
        ["identity", "backfill", "--apply", "--database-url", settings.database_url]
    )
    assert _identity_command(settings, dry) == EXIT_CONFLICTS
    assert _identity_command(settings, apply_args) == EXIT_CONFLICTS
    with _open(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity_backfill_runs").fetchone()[0] == 1


def test_fresh_and_0046_to_0047_and_historical_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(ignored_bots=("7777",))

    fresh = tmp_path / "fresh-test.db"
    _create_schema(fresh)
    with _open(fresh) as connection:
        _seed_standard(connection)
        connection.commit()
    assert _service(fresh, settings).apply().status == "succeeded"

    migrated = tmp_path / "from-0046-test.db"
    _upgrade(migrated, monkeypatch, "0046")
    with _open(migrated) as connection:
        _seed_standard(connection)
        connection.commit()
    _upgrade(migrated, monkeypatch, "0047")
    assert _service(migrated, settings).apply().status == "succeeded"

    historical = tmp_path / "historical-test.db"
    _upgrade(historical, monkeypatch, "head")
    with _open(historical) as connection:
        _seed_standard(connection)
        connection.execute(
            "INSERT INTO person_aliases("
            "user_id, group_scope, alias, alias_type, first_seen_at, last_seen_at"
            ") VALUES ('1001', '2001', 'Ada', 'nickname', ?, ?)",
            (_NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO person_relationships("
            "user_id, affection_score, trust_score, created_at, updated_at"
            ") VALUES ('1001', 50, 50, ?, ?)",
            (_NOW, _NOW),
        )
        connection.commit()
    report = _service(historical, settings).apply()
    assert report.status == "succeeded"
    with _open(historical) as connection:
        assert connection.execute(
            "SELECT canonical_person_id FROM person_relationships WHERE user_id = '1001'"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT canonical_space_id FROM person_aliases WHERE user_id = '1001'"
        ).fetchone()[0]
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0048"
        assert all(
            UUID(str(row[0])).version == 4 for row in connection.execute("SELECT id FROM persons")
        )


def test_sqlite_url_helper_rejects_memory() -> None:
    with pytest.raises(ValueError):
        sqlite_path_from_url("sqlite+aiosqlite:///:memory:")


def test_cli_parser_has_no_erase() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    _add_identity_parser(sub)
    help_text = parser.format_help()
    assert "--erase" not in help_text
    assert "--reset" not in help_text
    ok = parser.parse_args(["identity", "backfill", "--dry-run"])
    assert ok.dry_run is True


def test_successful_cli_apply_exit_zero(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    _create_schema(path)
    with _open(path) as connection:
        _seed_standard(connection)
        connection.commit()
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{path.as_posix()}",
        superusers_csv="",
        enabled_groups_csv="",
        ignored_bot_users_csv="7777",
    )
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    _add_identity_parser(sub)
    args = parser.parse_args(
        [
            "identity",
            "backfill",
            "--apply",
            "--format",
            "text",
            "--database-url",
            settings.database_url,
        ]
    )
    assert _identity_command(settings, args) == EXIT_OK


def test_ignored_bot_sender_and_member_stay_external_bot(tmp_path: Path) -> None:
    path = tmp_path / "repro-a-test.db"
    _create_schema(path)
    with _open(path) as connection:
        _insert_people(connection, "7777", nickname="OtherBot", is_bot=1)
        _insert_people(connection, "8000", nickname="Yuki", is_bot=1)
        _insert_group(connection, "2001")
        _insert_membership(connection, "7777", "2001")
        _insert_event(connection, bot="8000", sender="7777", group="2001")
        connection.commit()
    report = _service(path, _settings(ignored_bots=("7777",))).dry_run()
    assert report.status == "succeeded"
    assert report.counts.conflicts == 0
    assert report.counts.person_class == 0
    assert report.counts.external_bot_class == 1
    assert report.counts.yuki_presence_class == 1
    apply_report = _service(path, _settings(ignored_bots=("7777",))).apply()
    assert apply_report.status == "succeeded"
    with _open(path) as connection:
        assert (
            connection.execute(
                "SELECT canonical_person_id FROM people WHERE user_id = '7777'"
            ).fetchone()[0]
            is None
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM identity_bindings WHERE external_account_id = '7777'"
            ).fetchone()[0]
            == 0
        )


def test_yuki_member_is_presence_but_strong_human_still_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "taxonomy-test.db"
    _create_schema(path)
    with _open(path) as connection:
        _insert_people(connection, "8000", is_bot=1)
        _insert_group(connection, "2001")
        _insert_membership(connection, "8000", "2001")
        _insert_event(connection, bot="8000", sender="8000", group="2001")
        connection.commit()
    weak = _service(path, _settings()).dry_run()
    assert weak.status == "succeeded"
    assert weak.counts.yuki_presence_class == 1
    assert weak.counts.person_class == 0
    with _open(path) as connection:
        connection.execute("UPDATE people SET is_bot = 0 WHERE user_id = '8000'")
        connection.commit()
    strong = _service(path, _settings()).dry_run()
    assert strong.status == "conflicted"
    assert {item.error_category for item in strong.conflicts} >= {"yuki_and_person"}


def test_canonical_only_binding_and_presence_conflict(tmp_path: Path) -> None:
    path = tmp_path / "repro-b-test.db"
    _create_schema(path)
    person_id = "550e8400-e29b-41d4-a716-446655440000"
    presence_id = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
    with _open(path) as connection:
        _insert_person(connection, person_id)
        _insert_binding(
            connection,
            binding_id="7ba7b810-9dad-41d1-80b4-00c04fd430c8",
            person_id=person_id,
            external_id="4242",
        )
        _insert_presence(connection, presence_id=presence_id, external_id="4242")
        connection.commit()
    report = _service(path, _settings()).dry_run()
    assert report.status == "conflicted"
    assert report.counts.processed >= 1
    assert report.counts.conflicts >= 1
    assert {item.error_category for item in report.conflicts} >= {"canonical_kind_mismatch"}
    apply_report = _service(path, _settings()).apply()
    assert apply_report.status == "conflicted"
    assert apply_report.counts.processed >= 1
    with _open(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM presences").fetchone()[0] == 1
        assert (
            connection.execute("SELECT processed_count FROM identity_backfill_runs").fetchone()[0]
            >= 1
        )


def test_canonical_only_preconfigs_are_reused(tmp_path: Path) -> None:
    path = tmp_path / "canonical-only-test.db"
    _create_schema(path)
    person_id = "550e8400-e29b-41d4-a716-446655440000"
    presence_id = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
    space_id = "7ba7b810-9dad-41d1-80b4-00c04fd430c8"
    with _open(path) as connection:
        _insert_person(connection, person_id)
        _insert_binding(
            connection,
            binding_id="8ba7b810-9dad-41d1-80b4-00c04fd430c8",
            person_id=person_id,
            external_id="4243",
            display_name="Ada",
        )
        _insert_presence(connection, presence_id=presence_id, external_id="8001")
        _insert_space_binding(
            connection,
            binding_id="9ba7b810-9dad-41d1-80b4-00c04fd430c8",
            space_id=space_id,
            external_id="3001",
        )
        connection.commit()
    report = _service(path, _settings()).apply()
    assert report.status == "succeeded"
    assert report.counts.person_class == 1
    assert report.counts.yuki_presence_class == 1
    assert report.counts.space_class == 1
    with _open(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM presences").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM spaces").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0


def test_source_fingerprint_tracks_real_classification_inputs(tmp_path: Path) -> None:
    path = tmp_path / "repro-c-test.db"
    _create_schema(path)
    person_a = "550e8400-e29b-41d4-a716-446655440000"
    person_b = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
    with _open(path) as connection:
        _insert_people(connection, "1001", nickname="Ada", is_bot=0)
        _insert_group(connection, "2001", name="hall")
        _insert_person(connection, person_a)
        _insert_person(connection, person_b)
        _insert_binding(
            connection,
            binding_id="7ba7b810-9dad-41d1-80b4-00c04fd430c8",
            person_id=person_a,
            external_id="1001",
            display_name="Ada",
        )
        connection.commit()
    first = _service(path, _settings()).dry_run()
    again = _service(path, _settings()).dry_run()
    assert first.source_fingerprint == again.source_fingerprint
    assert len(first.source_fingerprint) == 64
    with _open(path) as connection:
        connection.execute("UPDATE people SET is_bot = 1 WHERE user_id = '1001'")
        connection.commit()
    flipped_role = _service(path, _settings()).dry_run()
    assert flipped_role.source_fingerprint != first.source_fingerprint
    with _open(path) as connection:
        connection.execute("UPDATE people SET is_bot = 0 WHERE user_id = '1001'")
        connection.execute(
            "UPDATE identity_bindings SET person_id = ? WHERE external_account_id = '1001'",
            (person_b,),
        )
        connection.commit()
    flipped_owner = _service(path, _settings()).dry_run()
    assert flipped_owner.source_fingerprint != first.source_fingerprint
    with _open(path) as connection:
        connection.execute(
            "UPDATE identity_bindings SET person_id = ? WHERE external_account_id = '1001'",
            (person_a,),
        )
        connection.execute("UPDATE groups SET enabled = 0 WHERE group_id = '2001'")
        connection.commit()
    flipped_flag = _service(path, _settings()).dry_run()
    assert flipped_flag.source_fingerprint != first.source_fingerprint


def test_missing_database_is_precondition_and_does_not_create(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "typo.db"
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{missing.as_posix()}",
        superusers_csv="",
        enabled_groups_csv="",
        ignored_bot_users_csv="",
    )
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    _add_identity_parser(sub)
    args = parser.parse_args(
        ["identity", "backfill", "--dry-run", "--database-url", settings.database_url]
    )
    assert _identity_command(settings, args) == EXIT_ERROR
    captured = capsys.readouterr()
    assert not missing.exists()
    assert "failed" in captured.out
    assert "database_missing" in captured.out
    assert "typo.db" not in captured.out
    assert str(tmp_path) not in captured.out
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_v2_runtime_state_refuses_apply_without_writes(tmp_path: Path) -> None:
    path = tmp_path / "repro-d-v2-test.db"
    _create_schema(path)
    with _open(path) as connection:
        _seed_standard(connection)
        connection.execute(
            "UPDATE identity_runtime_state SET state = 'v2', "
            "cutover_id = ?, source_fingerprint = 'cutover-fingerprint', "
            "completed_at = ? WHERE id = 1",
            ("550e8400-e29b-41d4-a716-446655440000", _NOW),
        )
        connection.commit()
    report = _service(path, _settings(ignored_bots=("7777",))).apply()
    assert report.status == "failed"
    assert report.error_category == "identity_runtime_state"
    assert report.run_recorded is False
    with _open(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity_backfill_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity_conflicts").fetchone()[0] == 0


def test_incomplete_schema_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "missing-column-test.db"
    _create_schema(path)
    with _open(path) as connection:
        _seed_standard(connection)
        connection.execute("DROP TABLE plugin_agent_messages")
        connection.commit()
    report = _service(path, _settings(ignored_bots=("7777",))).dry_run()
    assert report.status == "failed"
    assert report.error_category == "incomplete_schema"
    apply_report = _service(path, _settings(ignored_bots=("7777",))).apply()
    assert apply_report.status == "failed"
    assert apply_report.error_category == "incomplete_schema"
    with _open(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity_backfill_runs").fetchone()[0] == 0


def test_dirty_shadows_conflict_instead_of_being_skipped(tmp_path: Path) -> None:
    path = tmp_path / "shadow-conflict-test.db"
    _create_schema(path)
    person_id = "550e8400-e29b-41d4-a716-446655440000"
    other_id = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
    with _open(path) as connection:
        _insert_people(connection, "7777", is_bot=1)
        _insert_people(connection, "8000", is_bot=1)
        _insert_people(connection, "1001")
        _insert_group(connection, "2001")
        _insert_membership(connection, "1001", "2001")
        _insert_event(connection, bot="8000", sender="1001", group="2001")
        _insert_person(connection, person_id)
        _insert_person(connection, other_id)
        connection.execute(
            "UPDATE people SET canonical_person_id = ? WHERE user_id = '7777'",
            (person_id,),
        )
        connection.execute(
            "UPDATE people SET canonical_person_id = ? WHERE user_id = '8000'",
            (person_id,),
        )
        connection.execute(
            "UPDATE people SET canonical_person_id = ? WHERE user_id = '1001'",
            (person_id,),
        )
        connection.execute(
            "UPDATE memberships SET canonical_person_id = ? "
            "WHERE user_id = '1001' AND group_id = '2001'",
            (other_id,),
        )
        connection.commit()
    report = _service(path, _settings(ignored_bots=("7777",))).dry_run()
    assert report.status == "conflicted"
    categories = {item.error_category for item in report.conflicts}
    assert categories & {"canonical_kind_mismatch", "canonical_owner_mismatch"}


def test_consistent_preconfigured_shadow_is_reused(tmp_path: Path) -> None:
    path = tmp_path / "shadow-reuse-test.db"
    _create_schema(path)
    person_id = "550e8400-e29b-41d4-a716-446655440000"
    binding_id = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
    with _open(path) as connection:
        _insert_people(connection, "1001", nickname="Ada")
        _insert_person(connection, person_id)
        _insert_binding(
            connection,
            binding_id=binding_id,
            person_id=person_id,
            external_id="1001",
            display_name="Ada",
        )
        connection.execute(
            "UPDATE people SET canonical_person_id = ? WHERE user_id = '1001'",
            (person_id,),
        )
        connection.commit()
    report = _service(path, _settings()).apply()
    assert report.status == "succeeded"
    with _open(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT person_id FROM identity_bindings WHERE external_account_id = '1001'"
            ).fetchone()[0]
            == person_id
        )


def test_conflict_upsert_reopens_resolved_rows(tmp_path: Path) -> None:
    path = tmp_path / "conflict-upsert-test.db"
    _create_schema(path)
    with _open(path) as connection:
        _insert_people(connection, "8000", is_bot=0)
        _insert_event(connection, bot="8000", sender="8000")
        connection.commit()
    first = _service(path, _settings()).apply()
    assert first.status == "conflicted"
    assert first.counts.processed >= 1
    with _open(path) as connection:
        connection.execute(
            "UPDATE identity_conflicts SET status = 'resolved', "
            "error_category = 'stale_category', resolved_at = ? WHERE id = 1",
            (_NOW,),
        )
        connection.commit()
    second = _service(path, _settings()).apply()
    assert second.status == "conflicted"
    assert second.counts.processed >= 1
    with _open(path) as connection:
        row = connection.execute(
            "SELECT status, error_category, resolved_at, COUNT(*) FROM identity_conflicts"
        ).fetchone()
        assert row[0] == "open"
        assert row[1] == "yuki_and_person"
        assert row[2] is None
        assert connection.execute("SELECT COUNT(*) FROM identity_conflicts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0


def test_plugin_assistant_role_is_not_person(tmp_path: Path) -> None:
    path = tmp_path / "plugin-role-test.db"
    _create_schema(path)
    with _open(path) as connection:
        _insert_people(connection, "1001")
        _insert_people(connection, "7777", is_bot=1)
        connection.execute(
            "INSERT INTO plugin_installations("
            "plugin_id, name, version, plugin_api, yuki_requires, manifest_hash, "
            "entrypoint, status, enabled, approved_permissions_json, "
            "requested_permissions_json, failure_count, discovered_at, updated_at"
            ") VALUES ('demo', 'demo', '1.0.0', '2.0', '3.7.0', ?, "
            "'main:plugin', 'approved', 1, '[]', '[]', 0, ?, ?)",
            ("a" * 64, _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO plugin_agent_sessions("
            "session_id, plugin_id, owner_user_id, scope_type, scope_id, name, "
            "model, instructions, persistence, context_profile, "
            "allowed_capabilities_json, status, next_sequence, turn_count, "
            "created_at, updated_at, last_active_at"
            ") VALUES ('s1', 'demo', '1001', 'user', '1001', 'sess', '', "
            "'stay', 'durable', 'none', '[]', 'active', 1, 0, ?, ?, ?)",
            (_NOW, _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO plugin_agent_messages("
            "session_id, sequence, role, sender_user_id, content, metadata_json, created_at"
            ") VALUES ('s1', 1, 'user', '1001', 'hello', '{}', ?), "
            "('s1', 2, 'assistant', '7777', 'hi', '{}', ?), "
            "('s1', 3, 'tool', '7777', 'tool', '{}', ?)",
            (_NOW, _NOW, _NOW),
        )
        connection.commit()
    report = _service(path, _settings(ignored_bots=("7777",))).apply()
    assert report.status == "succeeded"
    with _open(path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM identity_bindings WHERE external_account_id = '7777'"
            ).fetchone()[0]
            == 0
        )
        rows = {
            str(row[0]): row[1]
            for row in connection.execute(
                "SELECT role, canonical_sender_person_id FROM plugin_agent_messages"
            )
        }
        assert rows["user"] is not None
        assert rows["assistant"] is None
        assert rows["tool"] is None


def test_apply_rejects_same_connection_v2_flip_after_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "toctou-monkeypatch-test.db"
    _create_schema(path)
    with _open(path) as connection:
        _seed_standard(connection)
        connection.commit()
    original = IdentityBackfillRepository.require_c7_ready

    def flip_after_gate(self: IdentityBackfillRepository, connection: sqlite3.Connection) -> None:
        original(self, connection)
        _flip_runtime_v2(connection)

    monkeypatch.setattr(IdentityBackfillRepository, "require_c7_ready", flip_after_gate)
    report = _service(path, _settings(ignored_bots=("7777",))).apply()
    assert report.status == "failed"
    assert report.error_category == "identity_runtime_state"
    assert report.run_recorded is False
    with _open(path) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity_backfill_runs").fetchone()[0] == 0


def test_begin_immediate_serializes_v2_writer(tmp_path: Path) -> None:
    path = tmp_path / "toctou-lock-test.db"
    _create_schema(path)
    with _open(path) as connection:
        _seed_standard(connection)
        connection.commit()
    blocked: list[str] = []

    def after_ready(name: str) -> None:
        if name != "after_c7_ready":
            return
        rival = sqlite3.connect(str(path), isolation_level=None, timeout=0.05)
        try:
            rival.execute("PRAGMA busy_timeout=50")
            rival.execute("BEGIN IMMEDIATE")
            _flip_runtime_v2(rival)
            rival.execute("COMMIT")
            blocked.append("wrote")
        except sqlite3.OperationalError:
            blocked.append("busy")
        finally:
            rival.close()

    report = _service(path, _settings(ignored_bots=("7777",)), failpoint=after_ready).apply()
    assert report.status == "succeeded"
    assert blocked == ["busy"]
    with _open(path) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 1


def test_conflict_audit_regates_and_skips_on_v2(tmp_path: Path) -> None:
    path = tmp_path / "conflict-audit-v2-test.db"
    _create_schema(path)
    with _open(path) as connection:
        _insert_people(connection, "8000", is_bot=0)
        _insert_event(connection, bot="8000", sender="8000")
        connection.commit()

    def flip_before_conflict(name: str) -> None:
        if name != "before_conflict_audit":
            return
        with _open(path) as connection:
            _flip_runtime_v2(connection)

    conflicted = _service(path, _settings(), failpoint=flip_before_conflict).apply()
    assert conflicted.status == "failed"
    assert conflicted.error_category == "identity_runtime_state"
    with _open(path) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v2"
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity_backfill_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity_conflicts").fetchone()[0] == 0


def test_aborted_run_does_not_write_on_v2(tmp_path: Path) -> None:
    path = tmp_path / "aborted-audit-v2-test.db"
    _create_schema(path)
    with _open(path) as connection:
        _seed_standard(connection)
        connection.commit()

    def flip_before_abort(name: str) -> None:
        if name == "after_foundation_writes":
            raise RuntimeError("failpoint")
        if name == "before_aborted_audit":
            with _open(path) as connection:
                _flip_runtime_v2(connection)

    with pytest.raises(RuntimeError, match="failpoint"):
        _service(path, _settings(ignored_bots=("7777",)), failpoint=flip_before_abort).apply()
    with _open(path) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v2"
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity_backfill_runs").fetchone()[0] == 0


def test_foreign_key_check_fail_closes(tmp_path: Path) -> None:
    path = tmp_path / "fk-check-test.db"
    _create_schema(path)
    with _open(path) as connection:
        _seed_standard(connection)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "UPDATE people SET canonical_person_id = ? WHERE user_id = '1001'",
            ("550e8400-e29b-41d4-a716-446655440000",),
        )
        connection.commit()
    dry = _service(path, _settings(ignored_bots=("7777",))).dry_run()
    assert dry.status == "failed"
    assert dry.error_category == "foreign_key_check"
    applied = _service(path, _settings(ignored_bots=("7777",))).apply()
    assert applied.status == "failed"
    assert applied.error_category == "foreign_key_check"
    with _open(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity_backfill_runs").fetchone()[0] == 0


def test_shadow_fingerprint_uses_hashed_row_key_and_stable_order(tmp_path: Path) -> None:
    first = tmp_path / "fp-order-a-test.db"
    second = tmp_path / "fp-order-b-test.db"
    extra = tmp_path / "fp-extra-pk-test.db"
    for path, group_order, memberships in (
        (first, ("2001", "2002"), (("1001", "2001"), ("1001", "2002"))),
        (second, ("2002", "2001"), (("1001", "2002"), ("1001", "2001"))),
        (extra, ("2001", "2002"), (("1001", "2001"),)),
    ):
        _create_schema(path)
        with _open(path) as connection:
            _insert_people(connection, "1001")
            for group_id in group_order:
                _insert_group(connection, group_id)
            for user_id, group_id in memberships:
                _insert_membership(connection, user_id, group_id)
            connection.commit()
    left = _service(first, _settings()).dry_run()
    right = _service(second, _settings()).dry_run()
    other = _service(extra, _settings()).dry_run()
    assert left.status == "succeeded"
    assert right.status == "succeeded"
    assert other.status == "succeeded"
    assert left.source_fingerprint == right.source_fingerprint
    assert other.source_fingerprint != left.source_fingerprint


def test_shadow_conflict_uses_source_external_id_not_row_key(tmp_path: Path) -> None:
    path = tmp_path / "shadow-subject-test.db"
    _create_schema(path)
    person_id = "550e8400-e29b-41d4-a716-446655440000"
    with _open(path) as connection:
        _insert_people(connection, "7777", is_bot=1)
        _insert_person(connection, person_id)
        connection.execute(
            "INSERT INTO plugin_installations("
            "plugin_id, name, version, plugin_api, yuki_requires, manifest_hash, "
            "entrypoint, status, enabled, approved_permissions_json, "
            "requested_permissions_json, failure_count, discovered_at, updated_at"
            ") VALUES ('demo', 'demo', '1.0.0', '2.0', '3.7.0', ?, "
            "'main:plugin', 'approved', 1, '[]', '[]', 0, ?, ?)",
            ("a" * 64, _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO plugin_agent_sessions("
            "session_id, plugin_id, owner_user_id, scope_type, scope_id, name, "
            "model, instructions, persistence, context_profile, "
            "allowed_capabilities_json, status, next_sequence, turn_count, "
            "created_at, updated_at, last_active_at, canonical_owner_person_id"
            ") VALUES ('4242', 'demo', '7777', 'user', '7777', 'sess', '', "
            "'stay', 'durable', 'none', '[]', 'active', 1, 0, ?, ?, ?, ?)",
            (_NOW, _NOW, _NOW, person_id),
        )
        connection.commit()
    report = _service(path, _settings(ignored_bots=("7777",))).apply()
    assert report.status == "conflicted"
    with _open(path) as connection:
        external_id = connection.execute("SELECT external_id FROM identity_conflicts").fetchone()[0]
        assert external_id == "7777"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM identity_conflicts WHERE external_id = '4242'"
            ).fetchone()[0]
            == 0
        )


def test_cli_unexpected_exception_is_sanitized(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cli-unexpected-test.db"
    _create_schema(path)
    leak = str(path.resolve())

    def boom(self: IdentityBackfillService) -> object:
        raise RuntimeError(f"failed at {leak}")

    monkeypatch.setattr(IdentityBackfillService, "dry_run", boom)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{path.as_posix()}",
        superusers_csv="",
        enabled_groups_csv="",
        ignored_bot_users_csv="",
    )
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    _add_identity_parser(sub)
    args = parser.parse_args(
        ["identity", "backfill", "--dry-run", "--database-url", settings.database_url]
    )
    assert _identity_command(settings, args) == EXIT_ERROR
    captured = capsys.readouterr()
    assert "operational_error" in captured.out
    assert "failed" in captured.out
    assert leak not in captured.out
    assert leak not in captured.err
    assert str(path) not in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
