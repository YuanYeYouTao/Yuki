"""C23b-2c: plugin canonical backfill, cutover gate, and Person forget."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from tests.unit.test_complete_v2_memory_evidence import _add_conversation, _event, _seed_person
from tests.unit.test_identity_backfill import _create_schema, _insert_people, _open
from tests.unit.test_identity_backfill import (
    _service as _backfill_service,
)
from tests.unit.test_identity_backfill import (
    _settings as _backfill_settings,
)
from tests.unit.test_identity_cutover import (
    _insert_event,
    _prepare_snapshots,
    _seed_identity,
)
from tests.unit.test_identity_cutover import (
    _open as _cutover_open,
)
from tests.unit.test_identity_cutover import (
    _service as _cutover_service,
)
from tests.unit.test_migration_0043 import _upgrade
from tests.unit.test_user_profiles import _flip_complete_v2

from qq_ai_bot.identity.backfill_repository import IdentityBackfillRepository
from qq_ai_bot.identity.c23_plugin import (
    C23_PLUGIN_INCOMPLETE,
    plan_c23_plugin_owners,
    require_c23_plugin_targets,
)
from qq_ai_bot.identity.canonical_memory_owners import merge_source_fingerprint
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_presence_preconfig,
    ensure_v2_space,
    set_identity_failpoint,
)
from qq_ai_bot.identity.errors import IdentityCutoverPreconditionError
from qq_ai_bot.identity.inventory import DEFERRED_SHADOWS
from qq_ai_bot.identity.reporting import render_report
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.plugin_host.db_models import (
    PluginAgentMessageModel,
    PluginAgentSessionModel,
    PluginBackgroundTargetGrantModel,
    PluginBackgroundTurnJobModel,
    PluginConfigValueModel,
    PluginInstallationModel,
    PluginNotificationOutboxModel,
    PluginStateModel,
)

_NOW = "2026-08-24T00:00:00+00:00"
_NOW_DT = datetime(2026, 8, 24, tzinfo=UTC)
_SRC = Path("src/qq_ai_bot")
_PLUGIN = "com.example.c23"
_PLUGIN_Q = "com.example.c23-q"


def _seed_plugin(connection, plugin_id: str = _PLUGIN) -> None:
    connection.execute(
        "INSERT INTO plugin_installations("
        "plugin_id, name, version, plugin_api, yuki_requires, manifest_hash, "
        "entrypoint, status, enabled, approved_permissions_json, "
        "requested_permissions_json, failure_count, discovered_at, updated_at"
        ") VALUES (?, 'C23', '1.0.0', '2.0', '3.7.0', ?, "
        "'main:plugin', 'approved', 1, '[]', '[]', 0, ?, ?)",
        (plugin_id, "a" * 64, _NOW, _NOW),
    )


def _insert_person_binding(connection, *, person_id: str, external_id: str) -> None:
    connection.execute(
        "INSERT INTO persons(id, enabled, revision, created_at, updated_at) VALUES (?, 1, 1, ?, ?)",
        (person_id, _NOW, _NOW),
    )
    connection.execute(
        "INSERT INTO identity_bindings("
        "id, person_id, platform, external_account_id, display_name, "
        "status, revision, created_at, updated_at"
        ") VALUES (?, ?, 'qq', ?, '', 'active', 1, ?, ?)",
        (str(uuid4()), person_id, external_id, _NOW, _NOW),
    )


def _insert_conversation(
    connection,
    *,
    conversation_id: str,
    kind: str,
    owner_id: str,
) -> None:
    alias_id = str(uuid4())
    person_id = owner_id if kind == "private" else None
    space_id = owner_id if kind == "space" else None
    started = False
    if not connection.in_transaction:
        connection.execute("BEGIN")
        started = True
    connection.execute(
        "INSERT INTO canonical_conversations("
        "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
        "starts_after_event_id, last_event_id, last_generation_change_event_id, "
        "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
        "revision, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, 1, 1, 0, 0, 0, 0, 0, 0, 1, ?, ?)",
        (conversation_id, kind, person_id, space_id, alias_id, _NOW, _NOW),
    )
    connection.execute(
        "INSERT INTO conversation_legacy_aliases("
        "id, conversation_id, scope_key, is_primary, created_at, updated_at"
        ") VALUES (?, ?, ?, 1, ?, ?)",
        (alias_id, conversation_id, f"scope:{alias_id}", _NOW, _NOW),
    )
    if started:
        connection.commit()


def _insert_state(
    connection,
    *,
    key: str,
    subject: str | None,
    person_id: str | None = None,
    plugin_id: str = _PLUGIN,
) -> int:
    cursor = connection.execute(
        "INSERT INTO plugin_state("
        "plugin_id, namespace, key, value_json, version, subject_user_id, updated_at, "
        "canonical_person_id"
        ") VALUES (?, 'notes', ?, '{\"secret\":true}', 1, ?, ?, ?)",
        (plugin_id, key, subject, _NOW, person_id),
    )
    return int(cursor.lastrowid)


def _insert_config(
    connection,
    *,
    scope_type: str,
    scope_id: str,
    key: str = "theme",
    person_id: str | None = None,
    space_id: str | None = None,
    plugin_id: str = _PLUGIN,
) -> int:
    cursor = connection.execute(
        "INSERT INTO plugin_config_values("
        "plugin_id, scope_type, scope_id, key, value_json, version, updated_at, "
        "canonical_person_id, canonical_space_id"
        ") VALUES (?, ?, ?, ?, '{\"v\":1}', 1, ?, ?, ?)",
        (plugin_id, scope_type, scope_id, key, _NOW, person_id, space_id),
    )
    return int(cursor.lastrowid)


def _insert_session(
    connection,
    *,
    session_id: str,
    scope_type: str,
    scope_id: str,
    owner_user_id: str | None,
    owner_person_id: str | None = None,
    space_id: str | None = None,
    plugin_id: str = _PLUGIN,
) -> None:
    connection.execute(
        "INSERT INTO plugin_agent_sessions("
        "session_id, plugin_id, owner_user_id, scope_type, scope_id, name, "
        "model, instructions, persistence, context_profile, "
        "allowed_capabilities_json, status, next_sequence, turn_count, "
        "created_at, updated_at, last_active_at, canonical_owner_person_id, "
        "canonical_space_id"
        ") VALUES (?, ?, ?, ?, ?, 'sess', '', 'stay', 'durable', 'none', "
        "'[]', 'active', 1, 0, ?, ?, ?, ?, ?)",
        (
            session_id,
            plugin_id,
            owner_user_id,
            scope_type,
            scope_id,
            _NOW,
            _NOW,
            _NOW,
            owner_person_id,
            space_id,
        ),
    )


def _insert_message(
    connection,
    *,
    session_id: str,
    sequence: int,
    role: str,
    sender_user_id: str | None,
    person_id: str | None = None,
    content: str = "secret-body",
) -> int:
    cursor = connection.execute(
        "INSERT INTO plugin_agent_messages("
        "session_id, sequence, role, sender_user_id, content, metadata_json, "
        "created_at, canonical_sender_person_id"
        ") VALUES (?, ?, ?, ?, ?, '{}', ?, ?)",
        (session_id, sequence, role, sender_user_id, content, _NOW, person_id),
    )
    return int(cursor.lastrowid)


def _insert_grant(
    connection,
    *,
    target_type: str,
    target_id: str,
    created_by: str,
    enabled: int = 1,
    plugin_id: str = _PLUGIN,
    bot_user_id: str = "8000",
    target_person: str | None = None,
    target_space: str | None = None,
    creator_person: str | None = None,
    presence_id: str | None = None,
) -> int:
    cursor = connection.execute(
        "INSERT INTO plugin_background_target_grants("
        "plugin_id, target_type, target_id, bot_user_id, enabled, created_by_user_id, "
        "created_at, updated_at, canonical_target_person_id, canonical_target_space_id, "
        "canonical_created_by_person_id, canonical_presence_id"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            plugin_id,
            target_type,
            target_id,
            bot_user_id,
            enabled,
            created_by,
            _NOW,
            _NOW,
            target_person,
            target_space,
            creator_person,
            presence_id,
        ),
    )
    return int(cursor.lastrowid)


def _insert_outbox(
    connection,
    *,
    event_id: int,
    target_type: str,
    target_id: str,
    status: str = "pending",
    notification_id: str = "n1",
    plugin_id: str = _PLUGIN,
    bot_user_id: str = "8000",
    target_person: str | None = None,
    target_space: str | None = None,
    conversation_id: str | None = None,
    presence_id: str | None = None,
    body: str = "secret-outbox",
) -> int:
    cursor = connection.execute(
        "INSERT INTO plugin_notification_outbox("
        "notification_id, part_key, source_event_id, plugin_id, target_type, target_id, "
        "bot_user_id, part_type, text, status, attempts, max_attempts, next_attempt_at, "
        "created_at, updated_at, canonical_target_person_id, canonical_target_space_id, "
        "canonical_conversation_id, canonical_presence_id"
        ") VALUES (?, 'text', ?, ?, ?, ?, ?, 'text', ?, ?, 0, 5, ?, ?, ?, ?, ?, ?, ?)",
        (
            notification_id,
            event_id,
            plugin_id,
            target_type,
            target_id,
            bot_user_id,
            body,
            status,
            _NOW,
            _NOW,
            _NOW,
            target_person,
            target_space,
            conversation_id,
            presence_id,
        ),
    )
    return int(cursor.lastrowid)


def _insert_job(
    connection,
    *,
    event_id: int,
    target_type: str,
    target_id: str,
    status: str = "pending",
    plugin_id: str = _PLUGIN,
    bot_user_id: str = "8000",
    target_person: str | None = None,
    target_space: str | None = None,
    conversation_id: str | None = None,
    presence_id: str | None = None,
) -> int:
    cursor = connection.execute(
        "INSERT INTO plugin_background_turn_jobs("
        "source_event_id, plugin_id, target_type, target_id, bot_user_id, agent_intent, "
        "status, attempts, max_attempts, next_attempt_at, generated_text, tool_calls_used, "
        "model_requests, created_at, updated_at, canonical_target_person_id, "
        "canonical_target_space_id, canonical_conversation_id, canonical_presence_id"
        ") VALUES (?, ?, ?, ?, ?, 'secret-intent', ?, 0, 3, ?, '', 0, 0, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            plugin_id,
            target_type,
            target_id,
            bot_user_id,
            status,
            _NOW,
            _NOW,
            _NOW,
            target_person,
            target_space,
            conversation_id,
            presence_id,
        ),
    )
    return int(cursor.lastrowid)


def _backfill_event(connection, *, message_id: str, sender: str = "1001") -> int:
    cursor = connection.execute(
        "INSERT INTO chat_events("
        "bot_user_id, platform_message_id, scope_type, group_id, private_peer_user_id, "
        "sender_user_id, direction, event_kind, content, visual_summary, segments_json, "
        "origin, occurred_at, observed_at"
        ") VALUES ('8000', ?, 'private', NULL, ?, ?, 'inbound', 'message', 'hi', '', "
        "'[]', 'user_message', ?, ?)",
        (message_id, sender, sender, _NOW, _NOW),
    )
    return int(cursor.lastrowid)


async def _install(session, plugin_id: str) -> None:
    session.add(
        PluginInstallationModel(
            plugin_id=plugin_id,
            name="C23",
            version="1.0.0",
            plugin_api="2.0",
            yuki_requires="3.7.0",
            manifest_hash="a" * 64,
            entrypoint="main:plugin",
            status="approved",
            enabled=True,
            approved_permissions_json="[]",
            requested_permissions_json="[]",
            failure_count=0,
            discovered_at=_NOW_DT,
            updated_at=_NOW_DT,
        )
    )
    await session.flush()


async def _assert_fk_clean(session) -> None:
    rows = (await session.execute(text("PRAGMA foreign_key_check"))).all()
    assert rows == []


_RAW_IDS = ("1001", "1002", "1003", "8000")


def _c23_material_fingerprint(connection) -> tuple[str, tuple[tuple[object, ...], ...]]:
    _assignments, _conflicts, _filled, material = plan_c23_plugin_owners(connection)
    return merge_source_fingerprint("", material), material


def _assert_no_raw_ids(report, material: tuple[tuple[object, ...], ...] | None = None) -> None:
    rendered = render_report(report, "json")
    for raw in (*_RAW_IDS, "secret"):
        assert raw not in rendered
        assert raw not in report.source_fingerprint
        for item in report.conflicts:
            assert raw not in item.fingerprint
    if material is None:
        return
    classified = [
        row
        for row in material
        if row
        and row[0]
        in {
            "conversation_lookup",
            "plugin_event_links",
            "plugin_agent_messages",
            "plugin_notification_outbox",
            "plugin_background_turn_jobs",
        }
    ]
    blob = repr(classified)
    for raw in (*_RAW_IDS, "secret"):
        assert raw not in blob


def test_plugin_conversation_is_not_deferred() -> None:
    deferred = " ".join(key for key, _reason in DEFERRED_SHADOWS)
    assert "plugin_notification_outbox.canonical_conversation_id" not in deferred
    assert "plugin_background_turn_jobs.canonical_conversation_id" not in deferred


def test_presence_state_stays_null_and_prefilled_person_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "c23-presence.db"
    _create_schema(path)
    with _open(path) as connection:
        _insert_people(connection, "8000", is_bot=1)
        _insert_people(connection, "1001")
        _backfill_event(connection, message_id="yuki-self")
        _seed_plugin(connection)
        state_id = _insert_state(connection, key="bot-kv", subject="8000")
        connection.commit()
    first = _backfill_service(path, _backfill_settings(superusers=("1001",))).apply()
    assert first.status == "succeeded", first.conflicts
    with _open(path) as connection:
        assert (
            connection.execute(
                "SELECT canonical_person_id FROM plugin_state WHERE id = ?",
                (state_id,),
            ).fetchone()[0]
            is None
        )
        person_id = connection.execute("SELECT id FROM persons").fetchone()[0]
        connection.execute(
            "UPDATE plugin_state SET canonical_person_id = ? WHERE id = ?",
            (person_id, state_id),
        )
        connection.commit()
        before = IdentityBackfillRepository(path).natural_key_projection(connection)
    report = _backfill_service(path, _backfill_settings(superusers=("1001",))).apply()
    assert report.status == "conflicted"
    assert report.business_diff == 0
    rendered = render_report(report, "json")
    assert "8000" not in rendered
    assert "1001" not in rendered
    assert "secret" not in rendered
    for item in report.conflicts:
        assert "8000" not in item.fingerprint
        assert "1001" not in item.fingerprint
    with _open(path) as connection:
        assert IdentityBackfillRepository(path).natural_key_projection(connection) == before
        assert (
            connection.execute(
                "SELECT canonical_person_id FROM plugin_state WHERE id = ?",
                (state_id,),
            ).fetchone()[0]
            == person_id
        )


def test_ambiguity_blocks_and_does_not_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "c23-ambiguous.db"
    _create_schema(path)
    person_a = str(uuid4())
    person_b = str(uuid4())
    conversation_a = str(uuid4())
    conversation_b = str(uuid4())
    with _open(path) as connection:
        _insert_people(connection, "8000", is_bot=1)
        _insert_people(connection, "1001")
        _insert_people(connection, "1002")
        _insert_person_binding(connection, person_id=person_a, external_id="1001")
        _insert_person_binding(connection, person_id=person_b, external_id="1002")
        _insert_conversation(
            connection, conversation_id=conversation_a, kind="private", owner_id=person_a
        )
        _insert_conversation(
            connection, conversation_id=conversation_b, kind="private", owner_id=person_b
        )
        _seed_plugin(connection)
        event_id = connection.execute(
            "INSERT INTO chat_events("
            "bot_user_id, platform_message_id, scope_type, group_id, private_peer_user_id, "
            "sender_user_id, direction, event_kind, content, visual_summary, segments_json, "
            "origin, occurred_at, observed_at, canonical_conversation_id"
            ") VALUES ('8000', 'amb-1', 'private', NULL, '1001', '1001', 'inbound', "
            "'message', 'hi', '', '[]', 'user_message', ?, ?, ?)",
            (_NOW, _NOW, conversation_a),
        ).lastrowid
        outbox_id = _insert_outbox(
            connection,
            event_id=int(event_id),
            target_type="private",
            target_id="1001",
            target_person=person_a,
            conversation_id=conversation_b,
        )
        connection.commit()
        before = list(
            connection.execute(
                "SELECT id, canonical_target_person_id, canonical_conversation_id "
                "FROM plugin_notification_outbox WHERE id = ?",
                (outbox_id,),
            )
        )
    report = _backfill_service(path, _backfill_settings(superusers=("1001",))).apply()
    assert report.status == "conflicted"
    assert report.business_diff == 0
    assert report.conflicts
    for item in report.conflicts:
        assert item.error_category in {"ambiguous_owner", "missing_owner"}
        assert "1001" not in item.fingerprint
        assert "1002" not in item.fingerprint
    with _open(path) as connection:
        assert (
            list(
                connection.execute(
                    "SELECT id, canonical_target_person_id, canonical_conversation_id "
                    "FROM plugin_notification_outbox WHERE id = ?",
                    (outbox_id,),
                )
            )
            == before
        )
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 2


def test_idempotent_apply_fills_conversation_and_second_apply_is_zero(tmp_path: Path) -> None:
    path = tmp_path / "c23-apply.db"
    _create_schema(path)
    person_id = str(uuid4())
    conversation_id = str(uuid4())
    with _open(path) as connection:
        _insert_people(connection, "8000", is_bot=1)
        _insert_people(connection, "1001")
        connection.execute(
            "INSERT INTO persons(id, enabled, revision, created_at, updated_at) "
            "VALUES (?, 1, 1, ?, ?)",
            (person_id, _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO identity_bindings("
            "id, person_id, platform, external_account_id, display_name, "
            "status, revision, created_at, updated_at"
            ") VALUES (?, ?, 'qq', '1001', '', 'active', 1, ?, ?)",
            (str(uuid4()), person_id, _NOW, _NOW),
        )
        _insert_conversation(
            connection, conversation_id=conversation_id, kind="private", owner_id=person_id
        )
        _seed_plugin(connection)
        event_id = connection.execute(
            "INSERT INTO chat_events("
            "bot_user_id, platform_message_id, scope_type, group_id, private_peer_user_id, "
            "sender_user_id, direction, event_kind, content, visual_summary, segments_json, "
            "origin, occurred_at, observed_at, canonical_conversation_id"
            ") VALUES ('8000', 'p-1', 'private', NULL, '1001', '1001', 'inbound', "
            "'message', 'hi', '', '[]', 'user_message', ?, ?, ?)",
            (_NOW, _NOW, conversation_id),
        ).lastrowid
        outbox_id = _insert_outbox(
            connection,
            event_id=int(event_id),
            target_type="private",
            target_id="1001",
        )
        _insert_grant(connection, target_type="private", target_id="1001", created_by="1001")
        connection.commit()
        before = IdentityBackfillRepository(path).business_signature(connection)
        conversations = connection.execute(
            "SELECT COUNT(*) FROM canonical_conversations"
        ).fetchone()[0]
    first = _backfill_service(path, _backfill_settings(superusers=("1001",))).apply()
    assert first.status == "succeeded", first.conflicts
    assert first.business_diff == 1
    assert first.counts.plugin_targets >= 1
    with _open(path) as connection:
        row = connection.execute(
            "SELECT canonical_target_person_id, canonical_target_space_id, "
            "canonical_conversation_id FROM plugin_notification_outbox WHERE id = ?",
            (outbox_id,),
        ).fetchone()
        assert row[0] == person_id
        assert row[1] is None
        assert row[2] == conversation_id
        assert (
            connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0]
            == conversations
        )
        after = IdentityBackfillRepository(path).business_signature(connection)
        assert after != before
    second = _backfill_service(path, _backfill_settings(superusers=("1001",))).apply()
    assert second.status == "succeeded"
    assert second.business_diff == 0
    with _open(path) as connection:
        assert IdentityBackfillRepository(path).business_signature(connection) == after


def test_fingerprint_covers_plugin_inputs_and_drifts(tmp_path: Path) -> None:
    path = tmp_path / "c23-fp.db"
    _create_schema(path)
    with _open(path) as connection:
        _insert_people(connection, "1001")
        _seed_plugin(connection)
        state_id = _insert_state(connection, key="sticky", subject="1001")
        connection.commit()
        first_c23, first_material = _c23_material_fingerprint(connection)
    first = _backfill_service(path, _backfill_settings(superusers=("1001",))).dry_run()
    assert first.status == "succeeded"
    _assert_no_raw_ids(first, first_material)
    with _open(path) as connection:
        connection.execute(
            "UPDATE plugin_state SET value_json = '{\"secret\":false}' WHERE id = ?",
            (state_id,),
        )
        connection.commit()
        second_c23, _material = _c23_material_fingerprint(connection)
    second = _backfill_service(path, _backfill_settings(superusers=("1001",))).dry_run()
    assert second.source_fingerprint != first.source_fingerprint
    assert second_c23 != first_c23
    _assert_no_raw_ids(second)


def test_fingerprint_drifts_when_conversation_appears(tmp_path: Path) -> None:
    path = tmp_path / "c23-fp-conv.db"
    _create_schema(path)
    person_id = str(uuid4())
    space_id = str(uuid4())
    conversation_id = str(uuid4())
    space_conversation_id = str(uuid4())
    with _open(path) as connection:
        _insert_people(connection, "8000", is_bot=1)
        _insert_people(connection, "1001")
        _insert_person_binding(connection, person_id=person_id, external_id="1001")
        connection.execute(
            "INSERT INTO spaces(id, name, enabled, autonomous_enabled, require_mention, "
            "revision, created_at, updated_at) VALUES (?, '', 1, 1, 1, 1, ?, ?)",
            (space_id, _NOW, _NOW),
        )
        _seed_plugin(connection)
        event_id = _backfill_event(connection, message_id="fp-conv")
        _insert_outbox(
            connection,
            event_id=event_id,
            target_type="private",
            target_id="1001",
        )
        connection.commit()
        empty_c23, empty_material = _c23_material_fingerprint(connection)
    empty = _backfill_service(path, _backfill_settings(superusers=("1001",))).dry_run()
    _assert_no_raw_ids(empty, empty_material)
    with _open(path) as connection:
        _insert_conversation(
            connection, conversation_id=conversation_id, kind="private", owner_id=person_id
        )
        connection.commit()
        one_c23, one_material = _c23_material_fingerprint(connection)
    one = _backfill_service(path, _backfill_settings(superusers=("1001",))).dry_run()
    assert one_c23 != empty_c23
    assert one.source_fingerprint != empty.source_fingerprint
    _assert_no_raw_ids(one, one_material)
    with _open(path) as connection:
        _insert_conversation(
            connection,
            conversation_id=space_conversation_id,
            kind="space",
            owner_id=space_id,
        )
        connection.commit()
        two_c23, two_material = _c23_material_fingerprint(connection)
    two = _backfill_service(path, _backfill_settings(superusers=("1001",))).dry_run()
    assert two_c23 != one_c23
    assert two.source_fingerprint != one.source_fingerprint
    _assert_no_raw_ids(two, two_material)


def test_fingerprint_drifts_when_event_conversation_fills(tmp_path: Path) -> None:
    path = tmp_path / "c23-fp-event.db"
    _create_schema(path)
    person_id = str(uuid4())
    conversation_a = str(uuid4())
    with _open(path) as connection:
        _insert_people(connection, "8000", is_bot=1)
        _insert_people(connection, "1001")
        _insert_person_binding(connection, person_id=person_id, external_id="1001")
        _insert_conversation(
            connection, conversation_id=conversation_a, kind="private", owner_id=person_id
        )
        _seed_plugin(connection)
        event_id = _backfill_event(connection, message_id="fp-event")
        _insert_outbox(
            connection,
            event_id=event_id,
            target_type="private",
            target_id="1001",
        )
        _insert_job(
            connection,
            event_id=event_id,
            target_type="private",
            target_id="1001",
        )
        connection.commit()
        null_c23, null_material = _c23_material_fingerprint(connection)
    null_report = _backfill_service(path, _backfill_settings(superusers=("1001",))).dry_run()
    _assert_no_raw_ids(null_report, null_material)
    with _open(path) as connection:
        connection.execute(
            "UPDATE chat_events SET canonical_conversation_id = ? WHERE id = ?",
            (conversation_a, event_id),
        )
        connection.commit()
        filled_c23, filled_material = _c23_material_fingerprint(connection)
    filled = _backfill_service(path, _backfill_settings(superusers=("1001",))).dry_run()
    assert filled_c23 != null_c23
    assert filled.source_fingerprint != null_report.source_fingerprint
    _assert_no_raw_ids(filled, filled_material)


def test_fingerprint_drifts_when_message_reparents_or_session_kind_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "c23-fp-msg.db"
    _create_schema(path)
    person_id = str(uuid4())
    with _open(path) as connection:
        _insert_people(connection, "1001")
        _insert_person_binding(connection, person_id=person_id, external_id="1001")
        _seed_plugin(connection)
        _insert_session(
            connection,
            session_id="user-p",
            scope_type="user",
            scope_id="1001",
            owner_user_id="1001",
        )
        _insert_session(
            connection,
            session_id="plugin-g",
            scope_type="plugin",
            scope_id="",
            owner_user_id=None,
        )
        message_id = _insert_message(
            connection,
            session_id="user-p",
            sequence=1,
            role="user",
            sender_user_id="1001",
        )
        connection.commit()
        first_c23, first_material = _c23_material_fingerprint(connection)
    first = _backfill_service(path, _backfill_settings(superusers=("1001",))).dry_run()
    _assert_no_raw_ids(first, first_material)
    with _open(path) as connection:
        connection.execute(
            "UPDATE plugin_agent_messages SET session_id = 'plugin-g' WHERE id = ?",
            (message_id,),
        )
        connection.commit()
        reparent_c23, reparent_material = _c23_material_fingerprint(connection)
    reparent = _backfill_service(path, _backfill_settings(superusers=("1001",))).dry_run()
    assert reparent_c23 != first_c23
    assert reparent.source_fingerprint != first.source_fingerprint
    _assert_no_raw_ids(reparent, reparent_material)
    with _open(path) as connection:
        connection.execute(
            "UPDATE plugin_agent_messages SET session_id = 'user-p' WHERE id = ?",
            (message_id,),
        )
        connection.execute(
            "UPDATE plugin_agent_sessions SET scope_type = 'plugin', scope_id = '', "
            "owner_user_id = NULL WHERE session_id = 'user-p'"
        )
        connection.commit()
        kind_c23, kind_material = _c23_material_fingerprint(connection)
    kind = _backfill_service(path, _backfill_settings(superusers=("1001",))).dry_run()
    assert kind_c23 != first_c23
    assert kind.source_fingerprint != first.source_fingerprint
    _assert_no_raw_ids(kind, kind_material)


def test_runnable_incomplete_blocks_backfill_and_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "c23-incomplete.db"
    _create_schema(path)
    with _open(path) as connection:
        _insert_people(connection, "8000", is_bot=1)
        _insert_people(connection, "1001")
        _seed_plugin(connection)
        event_id = _backfill_event(connection, message_id="p-incomplete")
        _insert_outbox(
            connection,
            event_id=event_id,
            target_type="private",
            target_id="1001",
        )
        connection.commit()
    report = _backfill_service(path, _backfill_settings(superusers=("1001",))).apply()
    assert report.status == "conflicted"
    assert report.business_diff == 0

    live = tmp_path / "c23-cutover.db"
    _upgrade(live, monkeypatch, "head")
    with _cutover_open(live) as connection:
        ids = _seed_identity(connection)
        _seed_plugin(connection)
        event_id = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="cutover-1",
            author_person_id=ids["person"],
            group_id=None,
            author_kind="person",
        )
        outbox_id = _insert_outbox(
            connection,
            event_id=event_id,
            target_type="private",
            target_id="1001",
            target_person=ids["person"],
            presence_id=ids["presence"],
        )
        _insert_grant(
            connection,
            target_type="private",
            target_id="1001",
            created_by="1001",
            target_person=ids["person"],
            creator_person=ids["person"],
            presence_id=ids["presence"],
        )
        connection.commit()
        with pytest.raises(IdentityCutoverPreconditionError) as incomplete:
            require_c23_plugin_targets(connection)
        assert incomplete.value.category == C23_PLUGIN_INCOMPLETE
    settings = _prepare_snapshots(live, tmp_path / "c23-null-snap")
    assert _cutover_service(live, settings).plan().error_category == C23_PLUGIN_INCOMPLETE

    with _cutover_open(live) as connection:
        conversation_id = str(uuid4())
        _insert_conversation(
            connection, conversation_id=conversation_id, kind="private", owner_id=ids["person"]
        )
        connection.execute(
            "UPDATE plugin_notification_outbox SET canonical_conversation_id = ? WHERE id = ?",
            (conversation_id, outbox_id),
        )
        connection.commit()
        require_c23_plugin_targets(connection)
    settings = _prepare_snapshots(live, tmp_path / "c23-ok-snap")
    planned = _cutover_service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    with _cutover_open(live) as connection:
        connection.execute(
            "UPDATE plugin_notification_outbox SET text = 'drift-body' WHERE id = ?",
            (outbox_id,),
        )
        connection.commit()
    drifted = _cutover_service(live, settings).apply(planned.source_fingerprint)
    assert drifted.status != "succeeded"
    assert drifted.error_category == "source_fingerprint"

    with _cutover_open(live) as connection:
        connection.execute(
            "UPDATE plugin_notification_outbox SET text = 'secret-outbox' WHERE id = ?",
            (outbox_id,),
        )
        connection.execute(
            "UPDATE chat_events SET canonical_conversation_id = ? WHERE id = ?",
            (conversation_id, event_id),
        )
        connection.commit()
    event_filled = _cutover_service(live, settings).apply(planned.source_fingerprint)
    assert event_filled.status != "succeeded"
    assert event_filled.error_category == "source_fingerprint"

    with _cutover_open(live) as connection:
        connection.execute(
            "UPDATE chat_events SET canonical_conversation_id = NULL WHERE id = ?",
            (event_id,),
        )
        extra_conversation = str(uuid4())
        _insert_conversation(
            connection, conversation_id=extra_conversation, kind="space", owner_id=ids["space"]
        )
        connection.commit()
    extra_conv = _cutover_service(live, settings).apply(planned.source_fingerprint)
    assert extra_conv.status != "succeeded"
    assert extra_conv.error_category == "source_fingerprint"

    with _cutover_open(live) as connection:
        _insert_session(
            connection,
            session_id="user-p",
            scope_type="user",
            scope_id="1001",
            owner_user_id="1001",
            owner_person_id=ids["person"],
        )
        _insert_session(
            connection,
            session_id="plugin-g",
            scope_type="plugin",
            scope_id="",
            owner_user_id=None,
        )
        message_id = _insert_message(
            connection,
            session_id="user-p",
            sequence=1,
            role="user",
            sender_user_id="1001",
            person_id=ids["person"],
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "c23-msg-snap")
    planned = _cutover_service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    with _cutover_open(live) as connection:
        connection.execute(
            "UPDATE plugin_agent_messages SET session_id = 'plugin-g' WHERE id = ?",
            (message_id,),
        )
        connection.commit()
    reparented = _cutover_service(live, settings).apply(planned.source_fingerprint)
    assert reparented.status != "succeeded"
    assert reparented.error_category == "source_fingerprint"


def test_c23_ast_does_not_invent_carriers_or_expose_raw_ids() -> None:
    tree = ast.parse((_SRC / "identity" / "c23_plugin.py").read_text(encoding="utf-8"))
    inserts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if "INSERT INTO" in text.upper():
                inserts.append(text)
    joined = "\n".join(inserts).lower()
    assert "persons" not in joined
    assert "spaces" not in joined
    assert "canonical_conversations" not in joined
    source = (_SRC / "persistence" / "people_repository.py").read_text(encoding="utf-8")
    v2_body = source[source.index("async def _delete_person_complete_v2") :]
    forget_idx = v2_body.index("await self._forget_c23_person_plugin")
    canonical_idx = v2_body.index("await forget_canonical_for_external_account")
    assert forget_idx < canonical_idx


@pytest.mark.asyncio
async def test_forget_p_created_vs_q_created_same_space(database: Database) -> None:
    await _flip_complete_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_presence_preconfig(session, "8000")
        person_p = await _seed_person(session, external_id="1001", display_name="P")
        person_q = await _seed_person(session, external_id="1003", display_name="Q")
        space_id = await ensure_v2_space(session, "2001")
        await _install(session, _PLUGIN)
        await _install(session, _PLUGIN_Q)
        conversation_id = str(uuid4())
        await _add_conversation(
            session,
            conversation_id=conversation_id,
            kind="space",
            space_id=space_id,
            scope_key="bot:8000:group:2001",
        )
        p_event = _event(
            message_id="g-p",
            sender_user_id="1001",
            author_person_id=person_p.person_id,
            conversation_id=conversation_id,
            canonical_event_id=str(uuid4()),
            group_id="2001",
        )
        q_event = _event(
            message_id="g-q",
            sender_user_id="1003",
            author_person_id=person_q.person_id,
            conversation_id=conversation_id,
            canonical_event_id=str(uuid4()),
            group_id="2001",
        )
        session.add(p_event)
        session.add(q_event)
        await session.flush()
        p_event_id = int(p_event.id)
        q_event_id = int(q_event.id)
        session.add(
            PluginBackgroundTargetGrantModel(
                plugin_id=_PLUGIN,
                target_type="group",
                target_id="2001",
                bot_user_id="8000",
                enabled=True,
                created_by_user_id="1001",
                created_at=_NOW_DT,
                updated_at=_NOW_DT,
                canonical_target_space_id=space_id,
                canonical_created_by_person_id=person_p.person_id,
            )
        )
        session.add(
            PluginBackgroundTargetGrantModel(
                plugin_id=_PLUGIN_Q,
                target_type="group",
                target_id="2001",
                bot_user_id="8000",
                enabled=True,
                created_by_user_id="1003",
                created_at=_NOW_DT,
                updated_at=_NOW_DT,
                canonical_target_space_id=space_id,
                canonical_created_by_person_id=person_q.person_id,
            )
        )
        session.add(
            PluginNotificationOutboxModel(
                notification_id="p-space",
                part_key="text",
                source_event_id=p_event_id,
                plugin_id=_PLUGIN,
                target_type="group",
                target_id="2001",
                bot_user_id="8000",
                part_type="text",
                text="p-space",
                status="sent",
                attempts=1,
                max_attempts=5,
                next_attempt_at=_NOW_DT,
                created_at=_NOW_DT,
                updated_at=_NOW_DT,
                canonical_target_space_id=space_id,
            )
        )
        session.add(
            PluginNotificationOutboxModel(
                notification_id="q-space",
                part_key="text",
                source_event_id=q_event_id,
                plugin_id=_PLUGIN_Q,
                target_type="group",
                target_id="2001",
                bot_user_id="8000",
                part_type="text",
                text="q-space",
                status="sent",
                attempts=1,
                max_attempts=5,
                next_attempt_at=_NOW_DT,
                created_at=_NOW_DT,
                updated_at=_NOW_DT,
                canonical_target_space_id=space_id,
            )
        )
        p_id = person_p.person_id
        q_id = person_q.person_id
        s_id = space_id
    assert await PeopleRepository(database).delete_person("1001") is True
    async with database.sessions() as session:
        grants = list(await session.scalars(select(PluginBackgroundTargetGrantModel)))
        assert [row.plugin_id for row in grants] == [_PLUGIN_Q]
        assert grants[0].canonical_created_by_person_id == q_id
        assert grants[0].canonical_target_space_id == s_id
        outbox = list(await session.scalars(select(PluginNotificationOutboxModel)))
        assert [row.notification_id for row in outbox] == ["q-space"]
        leftover = await session.scalar(
            select(func.count())
            .select_from(PluginBackgroundTargetGrantModel)
            .where(PluginBackgroundTargetGrantModel.canonical_created_by_person_id == p_id)
        )
        assert leftover == 0
        await _assert_fk_clean(session)


@pytest.mark.asyncio
async def test_forget_preserves_global_space_and_anonymizes_p_messages(
    database: Database,
) -> None:
    await _flip_complete_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_presence_preconfig(session, "8000")
        person_p = await _seed_person(session, external_id="1001", display_name="P")
        person_q = await _seed_person(session, external_id="1003", display_name="Q")
        space_id = await ensure_v2_space(session, "2001")
        await _install(session, _PLUGIN)
        session.add(
            PluginStateModel(
                plugin_id=_PLUGIN,
                namespace="notes",
                key="p-private",
                value_json="{}",
                version=1,
                subject_user_id="1001",
                updated_at=_NOW_DT,
                canonical_person_id=person_p.person_id,
            )
        )
        session.add(
            PluginStateModel(
                plugin_id=_PLUGIN,
                namespace="notes",
                key="global",
                value_json="{}",
                version=1,
                subject_user_id=None,
                updated_at=_NOW_DT,
            )
        )
        session.add(
            PluginConfigValueModel(
                plugin_id=_PLUGIN,
                scope_type="user",
                scope_id="1001",
                key="theme",
                value_json="{}",
                version=1,
                updated_at=_NOW_DT,
                canonical_person_id=person_p.person_id,
            )
        )
        session.add(
            PluginConfigValueModel(
                plugin_id=_PLUGIN,
                scope_type="global",
                scope_id="",
                key="theme",
                value_json="{}",
                version=1,
                updated_at=_NOW_DT,
            )
        )
        session.add(
            PluginConfigValueModel(
                plugin_id=_PLUGIN,
                scope_type="group",
                scope_id="2001",
                key="theme",
                value_json="{}",
                version=1,
                updated_at=_NOW_DT,
                canonical_space_id=space_id,
            )
        )
        session.add(
            PluginAgentSessionModel(
                session_id="user-p",
                plugin_id=_PLUGIN,
                owner_user_id="1001",
                scope_type="user",
                scope_id="1001",
                name="p",
                instructions="stay",
                status="active",
                created_at=_NOW_DT,
                updated_at=_NOW_DT,
                last_active_at=_NOW_DT,
                canonical_owner_person_id=person_p.person_id,
            )
        )
        session.add(
            PluginAgentSessionModel(
                session_id="group-s",
                plugin_id=_PLUGIN,
                owner_user_id="1001",
                scope_type="group",
                scope_id="2001",
                name="g",
                instructions="stay",
                status="active",
                created_at=_NOW_DT,
                updated_at=_NOW_DT,
                last_active_at=_NOW_DT,
                canonical_owner_person_id=person_p.person_id,
                canonical_space_id=space_id,
            )
        )
        session.add(
            PluginAgentSessionModel(
                session_id="plugin-g",
                plugin_id=_PLUGIN,
                owner_user_id=None,
                scope_type="plugin",
                scope_id="",
                name="plug",
                instructions="stay",
                status="active",
                created_at=_NOW_DT,
                updated_at=_NOW_DT,
                last_active_at=_NOW_DT,
            )
        )
        await session.flush()
        session.add(
            PluginAgentMessageModel(
                session_id="user-p",
                sequence=1,
                role="user",
                sender_user_id="1001",
                content="p-private",
                created_at=_NOW_DT,
                canonical_sender_person_id=person_p.person_id,
            )
        )
        session.add(
            PluginAgentMessageModel(
                session_id="group-s",
                sequence=1,
                role="user",
                sender_user_id="1001",
                content="p-in-space",
                created_at=_NOW_DT,
                canonical_sender_person_id=person_p.person_id,
            )
        )
        session.add(
            PluginAgentMessageModel(
                session_id="group-s",
                sequence=2,
                role="user",
                sender_user_id="1003",
                content="q-in-space",
                created_at=_NOW_DT,
                canonical_sender_person_id=person_q.person_id,
            )
        )
        session.add(
            PluginBackgroundTargetGrantModel(
                plugin_id=_PLUGIN,
                target_type="private",
                target_id="1001",
                bot_user_id="8000",
                enabled=True,
                created_by_user_id="1001",
                created_at=_NOW_DT,
                updated_at=_NOW_DT,
                canonical_target_person_id=person_p.person_id,
                canonical_created_by_person_id=person_p.person_id,
            )
        )
        p_id = person_p.person_id
        q_id = person_q.person_id
        s_id = space_id
    assert await PeopleRepository(database).delete_person("1001") is True
    async with database.sessions() as session:
        states = list(await session.scalars(select(PluginStateModel)))
        assert [row.key for row in states] == ["global"]
        configs = {
            (row.scope_type, row.scope_id)
            for row in await session.scalars(select(PluginConfigValueModel))
        }
        assert configs == {("global", ""), ("group", "2001")}
        sessions = list(await session.scalars(select(PluginAgentSessionModel)))
        assert {row.session_id for row in sessions} == {"group-s", "plugin-g"}
        group = next(row for row in sessions if row.session_id == "group-s")
        assert group.canonical_space_id == s_id
        assert group.canonical_owner_person_id is None
        messages = list(await session.scalars(select(PluginAgentMessageModel)))
        assert [row.content for row in messages] == ["q-in-space"]
        assert messages[0].canonical_sender_person_id == q_id
        assert (
            await session.scalar(select(func.count()).select_from(PluginBackgroundTargetGrantModel))
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PluginAgentMessageModel)
                .where(PluginAgentMessageModel.canonical_sender_person_id == p_id)
            )
            == 0
        )
        await _assert_fk_clean(session)


@pytest.mark.asyncio
async def test_forget_removes_terminal_null_private_outbox_and_jobs(
    database: Database,
) -> None:
    await _flip_complete_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_presence_preconfig(session, "8000")
        person_p = await _seed_person(session, external_id="1001", display_name="P")
        person_q = await _seed_person(session, external_id="1003", display_name="Q")
        space_id = await ensure_v2_space(session, "2001")
        await _install(session, _PLUGIN)
        await _install(session, _PLUGIN_Q)
        private_q = str(uuid4())
        space_conversation = str(uuid4())
        await _add_conversation(
            session,
            conversation_id=private_q,
            kind="private",
            person_id=person_q.person_id,
            scope_key="bot:8000:private:1003",
        )
        await _add_conversation(
            session,
            conversation_id=space_conversation,
            kind="space",
            space_id=space_id,
            scope_key="bot:8000:group:2001",
        )
        q_event = _event(
            message_id="q-private",
            sender_user_id="1003",
            author_person_id=person_q.person_id,
            conversation_id=private_q,
            canonical_event_id=str(uuid4()),
        )
        q_event_job = _event(
            message_id="q-private-job",
            sender_user_id="1003",
            author_person_id=person_q.person_id,
            conversation_id=private_q,
            canonical_event_id=str(uuid4()),
        )
        space_event = _event(
            message_id="q-space",
            sender_user_id="1003",
            author_person_id=person_q.person_id,
            conversation_id=space_conversation,
            canonical_event_id=str(uuid4()),
            group_id="2001",
        )
        session.add(q_event)
        session.add(q_event_job)
        session.add(space_event)
        await session.flush()
        session.add(
            PluginNotificationOutboxModel(
                notification_id="p-null-private",
                part_key="text",
                source_event_id=int(q_event.id),
                plugin_id=_PLUGIN,
                target_type="private",
                target_id="1001",
                bot_user_id="8000",
                part_type="text",
                text="p-terminal-null",
                status="sent",
                attempts=1,
                max_attempts=5,
                next_attempt_at=_NOW_DT,
                created_at=_NOW_DT,
                updated_at=_NOW_DT,
            )
        )
        session.add(
            PluginBackgroundTurnJobModel(
                source_event_id=int(space_event.id),
                plugin_id=_PLUGIN,
                target_type="private",
                target_id="1001",
                bot_user_id="8000",
                agent_intent="p-terminal-null",
                status="completed",
                attempts=1,
                max_attempts=3,
                next_attempt_at=_NOW_DT,
                created_at=_NOW_DT,
                updated_at=_NOW_DT,
            )
        )
        session.add(
            PluginNotificationOutboxModel(
                notification_id="q-private",
                part_key="text",
                source_event_id=int(q_event.id),
                plugin_id=_PLUGIN_Q,
                target_type="private",
                target_id="1003",
                bot_user_id="8000",
                part_type="text",
                text="q-private",
                status="sent",
                attempts=1,
                max_attempts=5,
                next_attempt_at=_NOW_DT,
                created_at=_NOW_DT,
                updated_at=_NOW_DT,
                canonical_target_person_id=person_q.person_id,
                canonical_conversation_id=private_q,
            )
        )
        session.add(
            PluginBackgroundTurnJobModel(
                source_event_id=int(q_event_job.id),
                plugin_id=_PLUGIN_Q,
                target_type="private",
                target_id="1003",
                bot_user_id="8000",
                agent_intent="q-private",
                status="completed",
                attempts=1,
                max_attempts=3,
                next_attempt_at=_NOW_DT,
                created_at=_NOW_DT,
                updated_at=_NOW_DT,
                canonical_target_person_id=person_q.person_id,
                canonical_conversation_id=private_q,
            )
        )
        session.add(
            PluginNotificationOutboxModel(
                notification_id="q-space",
                part_key="text",
                source_event_id=int(space_event.id),
                plugin_id=_PLUGIN_Q,
                target_type="group",
                target_id="2001",
                bot_user_id="8000",
                part_type="text",
                text="q-space",
                status="sent",
                attempts=1,
                max_attempts=5,
                next_attempt_at=_NOW_DT,
                created_at=_NOW_DT,
                updated_at=_NOW_DT,
                canonical_target_space_id=space_id,
                canonical_conversation_id=space_conversation,
            )
        )
        session.add(
            PluginConfigValueModel(
                plugin_id=_PLUGIN,
                scope_type="global",
                scope_id="",
                key="theme",
                value_json="{}",
                version=1,
                updated_at=_NOW_DT,
            )
        )
        session.add(
            PluginConfigValueModel(
                plugin_id=_PLUGIN,
                scope_type="group",
                scope_id="2001",
                key="theme",
                value_json="{}",
                version=1,
                updated_at=_NOW_DT,
                canonical_space_id=space_id,
            )
        )
        p_id = person_p.person_id
        q_id = person_q.person_id
        s_id = space_id
    assert await PeopleRepository(database).delete_person("1001") is True
    async with database.sessions() as session:
        outbox = list(await session.scalars(select(PluginNotificationOutboxModel)))
        assert {row.notification_id for row in outbox} == {"q-private", "q-space"}
        assert all(row.target_id != "1001" for row in outbox)
        jobs = list(await session.scalars(select(PluginBackgroundTurnJobModel)))
        assert [row.target_id for row in jobs] == ["1003"]
        assert jobs[0].canonical_target_person_id == q_id
        configs = {
            (row.scope_type, row.scope_id)
            for row in await session.scalars(select(PluginConfigValueModel))
        }
        assert configs == {("global", ""), ("group", "2001")}
        leftover_space = await session.scalar(
            select(func.count())
            .select_from(PluginNotificationOutboxModel)
            .where(PluginNotificationOutboxModel.canonical_target_space_id == s_id)
        )
        assert leftover_space == 1
        leftover_p = await session.scalar(
            select(func.count())
            .select_from(PluginNotificationOutboxModel)
            .where(PluginNotificationOutboxModel.canonical_target_person_id == p_id)
        )
        leftover_p_jobs = await session.scalar(
            select(func.count())
            .select_from(PluginBackgroundTurnJobModel)
            .where(PluginBackgroundTurnJobModel.canonical_target_person_id == p_id)
        )
        assert leftover_p == 0
        assert leftover_p_jobs == 0
        await _assert_fk_clean(session)


@pytest.mark.asyncio
async def test_v1_forgetme_does_not_use_c23_plugin_deletes(database: Database) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="P")
    await people.observe(user_id="1003", nickname="Q")
    async with database.sessions() as session, session.begin():
        await _install(session, _PLUGIN)
        session.add(
            PluginStateModel(
                plugin_id=_PLUGIN,
                namespace="notes",
                key="q-v1",
                value_json="{}",
                version=1,
                subject_user_id="1003",
                updated_at=_NOW_DT,
            )
        )
        session.add(
            PluginConfigValueModel(
                plugin_id=_PLUGIN,
                scope_type="global",
                scope_id="",
                key="theme",
                value_json="{}",
                version=1,
                updated_at=_NOW_DT,
            )
        )

    def boom(name: str) -> None:
        if name == "after_c23_forget_plugin":
            raise RuntimeError("c23-should-not-run")

    set_identity_failpoint(boom)
    try:
        assert await people.delete_person("1001") is True
    finally:
        set_identity_failpoint(None)
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PluginStateModel)) == 1
        assert await session.scalar(select(func.count()).select_from(PluginConfigValueModel)) == 1
