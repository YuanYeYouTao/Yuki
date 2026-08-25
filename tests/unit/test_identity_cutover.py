"""Identity-cutover --plan/--apply, failpoints, snapshot rollback, binary epoch."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from tests.unit.test_migration_0043 import _upgrade

from qq_ai_bot.cli import _add_identity_cutover_parser
from qq_ai_bot.identity.backfill_repository import connect_sqlite
from qq_ai_bot.identity.binary_epoch import IDENTITY_BINARY_EPOCH, refuse_identity_binary_epoch
from qq_ai_bot.identity.cutover_reporting import render_cutover_report
from qq_ai_bot.identity.cutover_repository import (
    MIGRATION_SUMMARY_MAX_CHARACTERS,
    IdentityCutoverRepository,
    _assemble_migration_summary,
    _fair_allocate,
    _MigrationScopeMaterial,
    downtime_token_digest,
    restore_sqlite_snapshot,
)
from qq_ai_bot.identity.cutover_service import EXIT_BLOCKED, EXIT_OK, IdentityCutoverService
from qq_ai_bot.identity.cutover_source import (
    applied_business_tables,
    is_excluded_source_table,
    live_source_manifest,
    manifests_equal,
)
from qq_ai_bot.identity.cutover_types import CutoverSettingsInput, is_sha256_hex
from qq_ai_bot.identity.errors import (
    IdentityCutoverError,
    IdentityCutoverPreconditionError,
    IdentityDualWriteError,
)
from qq_ai_bot.identity.inventory import (
    EVENT_AUTHOR_KINDS,
    SHADOW_FILL_SPECS,
    SHAPE_ONLY_OPTIONAL_SHADOWS,
    shadow_inventory_drift,
    shadow_spec_policy_errors,
)
from qq_ai_bot.identity.sanitize import looks_like_secret_or_path
from qq_ai_bot.persistence.instance_lock import SQLiteApplicationLock
from qq_ai_bot.persistence.models import Base

_NOW = "2026-08-24T00:00:00+00:00"
_REVISION = "f9552229a6b375a8492795d7461f4a0023af2c79"
_FAILPOINTS = (
    "after_schema_ready",
    "after_revalidate",
    "after_conversations",
    "after_aliases",
    "after_routes",
    "after_event_mapping",
    "before_scope_carrier_retirement",
    "after_scope_carrier_retirement",
    "after_baselines",
    "after_preconfig",
    "after_c21_owners",
    "before_flip",
    "before_commit",
)


def _settings(snapshot_dir: Path) -> CutoverSettingsInput:
    return CutoverSettingsInput(
        expected_git_revision=_REVISION,
        git_revision=_REVISION,
        downtime_token="downtime-ok",
        snapshot_db=str(snapshot_dir / "qq.db"),
        snapshot_wal=str(snapshot_dir / "qq.db-wal"),
        snapshot_shm=str(snapshot_dir / "qq.db-shm"),
    )


def _prepare_snapshots(live: Path, snapshot_dir: Path) -> CutoverSettingsInput:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    target = snapshot_dir / "qq.db"
    shutil.copy2(live, target)
    (snapshot_dir / "qq.db-wal").write_bytes(b"")
    (snapshot_dir / "qq.db-shm").write_bytes(b"")
    return _settings(snapshot_dir)


def _service(
    path: Path, settings: CutoverSettingsInput, failpoint: object | None = None
) -> IdentityCutoverService:
    return IdentityCutoverService(path, settings, failpoint=failpoint)


@contextmanager
def _open(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
    finally:
        connection.close()


def _seed_identity(connection: sqlite3.Connection) -> dict[str, str]:
    ids = {
        "person": str(uuid4()),
        "space": str(uuid4()),
        "binding": str(uuid4()),
        "space_binding": str(uuid4()),
        "presence": str(uuid4()),
    }
    connection.execute(
        "INSERT INTO persons(id, enabled, revision, created_at, updated_at) VALUES (?, 1, 1, ?, ?)",
        (ids["person"], _NOW, _NOW),
    )
    connection.execute(
        "INSERT INTO spaces(id, name, enabled, autonomous_enabled, require_mention, "
        "revision, created_at, updated_at) VALUES (?, '', 1, 1, 1, 1, ?, ?)",
        (ids["space"], _NOW, _NOW),
    )
    connection.execute(
        "INSERT INTO identity_bindings("
        "id, person_id, platform, external_account_id, display_name, status, "
        "revision, created_at, updated_at"
        ") VALUES (?, ?, 'qq', '1001', '', 'active', 1, ?, ?)",
        (ids["binding"], ids["person"], _NOW, _NOW),
    )
    connection.execute(
        "INSERT INTO space_bindings("
        "id, space_id, platform, external_space_id, display_name, status, "
        "revision, created_at, updated_at"
        ") VALUES (?, ?, 'qq', '2001', '', 'active', 1, ?, ?)",
        (ids["space_binding"], ids["space"], _NOW, _NOW),
    )
    connection.execute(
        "INSERT INTO presences("
        "id, platform, external_account_id, enabled, ingest_eligible, "
        "revision, created_at, updated_at"
        ") VALUES (?, 'qq', '8000', 1, 1, 1, ?, ?)",
        (ids["presence"], _NOW, _NOW),
    )
    return ids


def _insert_event(
    connection: sqlite3.Connection,
    *,
    bot_user_id: str,
    platform_message_id: str,
    author_person_id: str | None = None,
    sender_user_id: str = "1001",
    group_id: str | None = "2001",
    private_peer_user_id: str | None = None,
    content: str = "hello",
    segments_json: str = "[]",
    occurred_at: str = _NOW,
    author_kind: str | None = "person",
    author_presence_id: str | None = None,
    canonical_event_id: str | None = None,
) -> int:
    peer = None if group_id else (private_peer_user_id or sender_user_id)
    connection.execute(
        "INSERT INTO chat_events("
        "bot_user_id, platform_message_id, scope_type, group_id, private_peer_user_id, "
        "sender_user_id, direction, event_kind, content, visual_summary, segments_json, "
        "origin, occurred_at, observed_at, author_kind, author_person_id, author_presence_id, "
        "canonical_event_id"
        ") VALUES (?, ?, ?, ?, ?, ?, 'inbound', 'message', ?, '', ?, "
        "'user_message', ?, ?, ?, ?, ?, ?)",
        (
            bot_user_id,
            platform_message_id,
            "group" if group_id else "private",
            group_id,
            peer,
            sender_user_id,
            content,
            segments_json,
            occurred_at,
            _NOW,
            author_kind,
            author_person_id,
            author_presence_id,
            canonical_event_id,
        ),
    )
    return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _insert_memory_job(
    connection: sqlite3.Connection,
    event_id: int,
    *,
    status: str = "failed",
    conversation_key: str = "group:8000:2001",
) -> None:
    connection.execute(
        "INSERT INTO memory_jobs("
        "event_id, conversation_key, status, attempts, next_attempt_at, "
        "created_at, updated_at, processing_source"
        ") VALUES (?, ?, ?, 0, ?, ?, ?, 'live')",
        (event_id, conversation_key, status, _NOW, _NOW, _NOW),
    )


def _insert_presence(
    connection: sqlite3.Connection, presence_id: str, external_account_id: str
) -> None:
    connection.execute(
        "INSERT INTO presences("
        "id, platform, external_account_id, enabled, ingest_eligible, "
        "revision, created_at, updated_at"
        ") VALUES (?, 'qq', ?, 1, 1, 1, ?, ?)",
        (presence_id, external_account_id, _NOW, _NOW),
    )


def _insert_scope(
    connection: sqlite3.Connection,
    *,
    scope_key: str,
    bot_user_id: str,
    last_event_id: int,
    group_id: str | None = None,
    private_peer_user_id: str | None = None,
    starts_after_event_id: int = 0,
    generation: int = 1,
    last_generation_change_event_id: int = 0,
) -> int:
    if private_peer_user_id:
        scope_type = "private"
        group_id = None
    else:
        scope_type = "group"
        if group_id is None:
            raise ValueError("group scope requires group_id")
    connection.execute(
        "INSERT INTO conversation_scopes("
        "scope_key, bot_user_id, scope_type, group_id, private_peer_user_id, generation, "
        "starts_after_event_id, last_event_id, last_generation_change_event_id, "
        "uncovered_event_count, uncovered_character_count, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
        (
            scope_key,
            bot_user_id,
            scope_type,
            group_id,
            private_peer_user_id,
            generation,
            starts_after_event_id,
            last_event_id,
            last_generation_change_event_id,
            _NOW,
            _NOW,
        ),
    )
    return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _suppress_event_ids(
    repository: IdentityCutoverRepository, connection: sqlite3.Connection
) -> frozenset[int]:
    return frozenset(
        event_id
        for item in repository.classify_duplicates(connection)
        if item.kind == "suppress" and item.keeper_event_id is not None
        for event_id in item.event_ids
        if event_id != item.keeper_event_id
    )


def _insert_legacy_rollup(
    connection: sqlite3.Connection,
    *,
    scope_id: int,
    covered_through_event_id: int,
    summary_text: str,
    fingerprint: str,
    generation: int = 1,
) -> None:
    connection.execute(
        "INSERT INTO conversation_rollups("
        "scope_id, generation, covered_through_event_id, summary_text, "
        "summary_kind, source_fingerprint, revision, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, 'extractive', ?, 1, ?, ?)",
        (scope_id, generation, covered_through_event_id, summary_text, fingerprint, _NOW, _NOW),
    )


def _insert_emergency_overlay(
    connection: sqlite3.Connection,
    *,
    scope_id: int,
    covered_through_event_id: int,
    summary_text: str,
    fingerprint: str,
    generation: int = 1,
) -> None:
    connection.execute(
        "INSERT INTO conversation_rollup_emergency_overlays("
        "scope_id, generation, covered_through_event_id, summary_text, "
        "source_fingerprint, base_semantic_revision, revision, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, 0, 1, ?, ?)",
        (
            scope_id,
            generation,
            covered_through_event_id,
            summary_text,
            fingerprint,
            _NOW,
            _NOW,
        ),
    )


def _legacy_carrier_counts(connection: sqlite3.Connection) -> tuple[int, int, int]:
    return (
        int(connection.execute("SELECT COUNT(*) FROM conversation_scopes").fetchone()[0]),
        int(connection.execute("SELECT COUNT(*) FROM conversation_rollups").fetchone()[0]),
        int(
            connection.execute(
                "SELECT COUNT(*) FROM conversation_rollup_emergency_overlays"
            ).fetchone()[0]
        ),
    )


def _insert_canonical_conversation(
    connection: sqlite3.Connection,
    *,
    kind: str,
    owner_id: str,
    primary_scope_key: str,
    generation: int = 1,
    starts_after_event_id: int = 0,
    last_event_id: int = 0,
    last_generation_change_event_id: int = 0,
    covered_through_event_id: int = 0,
    uncovered_event_count: int = 0,
    uncovered_character_count: int = 0,
) -> str:
    conversation_id = str(uuid4())
    alias_id = str(uuid4())
    connection.execute(
        "INSERT INTO canonical_conversations("
        "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
        "starts_after_event_id, last_event_id, last_generation_change_event_id, "
        "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
        "revision, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (
            conversation_id,
            kind,
            owner_id if kind == "private" else None,
            owner_id if kind == "space" else None,
            alias_id,
            generation,
            starts_after_event_id,
            last_event_id,
            last_generation_change_event_id,
            covered_through_event_id,
            uncovered_event_count,
            uncovered_character_count,
            _NOW,
            _NOW,
        ),
    )
    connection.execute(
        "INSERT INTO conversation_legacy_aliases("
        "id, conversation_id, scope_key, is_primary, created_at, updated_at"
        ") VALUES (?, ?, ?, 1, ?, ?)",
        (alias_id, conversation_id, primary_scope_key, _NOW, _NOW),
    )
    return conversation_id


def _insert_person_route(
    connection: sqlite3.Connection,
    *,
    person_id: str,
    binding_id: str,
    presence_id: str,
) -> None:
    connection.execute(
        "INSERT INTO person_active_routes("
        "person_id, identity_binding_id, presence_id, route_generation, "
        "paused, revision, created_at, updated_at"
        ") VALUES (?, ?, ?, 1, 0, 1, ?, ?)",
        (person_id, binding_id, presence_id, _NOW, _NOW),
    )


def _insert_ingest_route(
    connection: sqlite3.Connection,
    *,
    space_binding_id: str,
    presence_id: str,
    paused: int = 0,
) -> None:
    connection.execute(
        "INSERT INTO space_binding_ingest_routes("
        "space_binding_id, ingest_presence_id, route_generation, "
        "paused, revision, created_at, updated_at"
        ") VALUES (?, ?, 1, ?, 1, ?, ?)",
        (space_binding_id, presence_id, paused, _NOW, _NOW),
    )


def _insert_space_route(
    connection: sqlite3.Connection,
    *,
    space_id: str,
    space_binding_id: str,
    presence_id: str,
) -> None:
    connection.execute(
        "INSERT INTO space_active_routes("
        "space_id, space_binding_id, presence_id, route_generation, "
        "paused, revision, created_at, updated_at"
        ") VALUES (?, ?, ?, 1, 0, 1, ?, ?)",
        (space_id, space_binding_id, presence_id, _NOW, _NOW),
    )


def _pin_required_routes(connection: sqlite3.Connection, ids: dict[str, str]) -> None:
    _insert_person_route(
        connection,
        person_id=ids["person"],
        binding_id=ids["binding"],
        presence_id=ids["presence"],
    )
    _insert_ingest_route(
        connection, space_binding_id=ids["space_binding"], presence_id=ids["presence"]
    )
    _insert_space_route(
        connection,
        space_id=ids["space"],
        space_binding_id=ids["space_binding"],
        presence_id=ids["presence"],
    )


def _insert_admin_operation(connection: sqlite3.Connection, *, operation: str) -> int:
    connection.execute(
        "INSERT INTO admin_operation_events("
        "actor_user_id, trigger_message_id, conversation_key, capability, operation, "
        "target_type, target_id, before_json, after_json, success, error_category, "
        "duration_seconds, created_at"
        ") VALUES ('1', '', '', 'cap', ?, 'x', '', 'null', 'null', 1, NULL, 0, ?)",
        (operation, _NOW),
    )
    return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _insert_plugin_external(
    connection: sqlite3.Connection,
    *,
    bot_user_id: str,
    platform_message_id: str,
    group_id: str = "2001",
) -> int:
    connection.execute(
        "INSERT INTO chat_events("
        "bot_user_id, platform_message_id, scope_type, group_id, private_peer_user_id, "
        "sender_user_id, direction, event_kind, source_plugin_id, external_source, "
        "external_event_key, external_event_type, external_payload_json, external_target_id, "
        "content, visual_summary, segments_json, origin, occurred_at, observed_at, "
        "author_kind, author_person_id"
        ") VALUES (?, ?, 'group', ?, NULL, '1001', 'external', 'external_event', "
        "'plug', 'src', 'ext-key', 'notice', '{}', '2001', 'plugin-body', '', '[]', "
        "'plugin_background', ?, ?, 'system', NULL)",
        (bot_user_id, platform_message_id, group_id, _NOW, _NOW),
    )
    return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _insert_relationship(connection: sqlite3.Connection, *, user_id: str, person_id: str) -> None:
    connection.execute(
        "INSERT INTO person_relationships("
        "user_id, affection_score, trust_score, created_at, updated_at, canonical_person_id"
        ") VALUES (?, 50, 50, ?, ?, ?)",
        (user_id, _NOW, _NOW, person_id),
    )


def _insert_people(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    is_bot: int = 0,
    canonical_person_id: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO people(user_id, nickname, enabled, is_bot, first_seen_at, last_seen_at, "
        "canonical_person_id) VALUES (?, '', 1, ?, ?, ?, ?)",
        (user_id, is_bot, _NOW, _NOW, canonical_person_id),
    )


def _insert_group(
    connection: sqlite3.Connection,
    group_id: str,
    *,
    canonical_space_id: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO groups("
        "group_id, name, enabled, require_mention, autonomous_enabled, "
        "first_seen_at, last_seen_at, updated_at, canonical_space_id"
        ") VALUES (?, '', 1, 1, 1, ?, ?, ?, ?)",
        (group_id, _NOW, _NOW, _NOW, canonical_space_id),
    )


def _insert_person(
    connection: sqlite3.Connection,
    person_id: str,
) -> None:
    connection.execute(
        "INSERT INTO persons(id, enabled, revision, created_at, updated_at) VALUES (?, 1, 1, ?, ?)",
        (person_id, _NOW, _NOW),
    )


def _insert_space(connection: sqlite3.Connection, space_id: str) -> None:
    connection.execute(
        "INSERT INTO spaces(id, name, enabled, autonomous_enabled, require_mention, "
        "revision, created_at, updated_at) VALUES (?, '', 1, 1, 1, 1, ?, ?)",
        (space_id, _NOW, _NOW),
    )


def _seed_plugin_install(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO plugin_installations("
        "plugin_id, name, version, plugin_api, yuki_requires, manifest_hash, "
        "entrypoint, status, enabled, approved_permissions_json, "
        "requested_permissions_json, failure_count, discovered_at, updated_at"
        ") VALUES ('fixture', 'Fixture', '1.0.0', '2.0', '>=3.7.0', 'hash', "
        "'fixture:plugin', 'running', 1, '[]', '[]', 0, ?, ?)",
        (_NOW, _NOW),
    )


def _insert_automation_row(
    connection: sqlite3.Connection,
    *,
    creator_user_id: str = "1001",
    bot_user_id: str = "8000",
    canonical_creator_person_id: str | None = None,
    canonical_presence_id: str | None = None,
    status: str = "active",
) -> None:
    connection.execute(
        "INSERT INTO automations("
        "creator_user_id, bot_user_id, name, status, timezone, schedule_json, "
        "script_json, script_hash, required_capabilities_json, authority_snapshot_json, "
        "created_from_message_id, run_count, consecutive_failures, misfire_grace_seconds, "
        "created_at, updated_at, canonical_creator_person_id, canonical_presence_id"
        ") VALUES (?, ?, 'auto', ?, 'Asia/Shanghai', '{}', '{}', 'hash', '[]', '{}', "
        "'event-1', 0, 0, 1800, ?, ?, ?, ?)",
        (
            creator_user_id,
            bot_user_id,
            status,
            _NOW,
            _NOW,
            canonical_creator_person_id,
            canonical_presence_id,
        ),
    )


def _insert_memory_fact_person(
    connection: sqlite3.Connection,
    *,
    subject_user_id: str,
    canonical_subject_person_id: str | None,
) -> None:
    connection.execute(
        "INSERT INTO memory_facts("
        "scope_type, subject_user_id, kind, memory_key, category, content, "
        "normalized_content, importance, confidence, source_type, authority, status, "
        "conflict_state, created_at, updated_at, last_confirmed_at, validation_version, "
        "review_state, canonical_subject_person_id"
        ") VALUES ('person', ?, 'fact', 'k', 'cat', 'c', 'c', 3, 1.0, 'explicit', "
        "'self_report', 'active', 'clear', ?, ?, ?, 'memory-v2-quality-v1', 'verified', ?)",
        (subject_user_id, _NOW, _NOW, _NOW, canonical_subject_person_id),
    )


def _insert_emoji_usage(
    connection: sqlite3.Connection,
    *,
    actor_user_id: str,
    group_id: str,
    canonical_actor_person_id: str | None,
    canonical_space_id: str | None,
) -> None:
    emoji_id = str(uuid4())
    connection.execute(
        "INSERT INTO emoji_assets("
        "id, sha256, relative_path, image_format, mime_type, byte_size, width, height, "
        "frame_count, animated, status, description, emotion_tags_json, "
        "usage_scenarios_json, ocr_text, intensity, confidence, analysis_version, "
        "pinned, source_sub_type, source_emoji_id, source_package_id, seen_count, "
        "use_count, first_seen_at, last_seen_at, created_at, updated_at"
        ") VALUES (?, ?, ?, 'png', 'image/png', 12, 8, 8, 1, 0, 'candidate', '', "
        "'[]', '[]', '', 0.5, 0.0, '', 0, '', '', '', 1, 0, ?, ?, ?, ?)",
        (emoji_id, "c" * 64, f"emoji/{emoji_id}.png", _NOW, _NOW, _NOW, _NOW),
    )
    connection.execute(
        "INSERT INTO emoji_usage_events("
        "emoji_id, actor_user_id, group_id, trigger_message_id, source, created_at, "
        "canonical_actor_person_id, canonical_space_id"
        ") VALUES (?, ?, ?, '', 'chat', ?, ?, ?)",
        (
            emoji_id,
            actor_user_id,
            group_id,
            _NOW,
            canonical_actor_person_id,
            canonical_space_id,
        ),
    )


def test_cli_parser_exposes_plan_and_apply() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    _add_identity_cutover_parser(sub)
    planned = parser.parse_args(
        [
            "identity-cutover",
            "--plan",
            "--git-revision",
            _REVISION,
            "--downtime-token",
            "downtime-ok",
            "--snapshot-db",
            "db",
            "--snapshot-wal",
            "wal",
            "--snapshot-shm",
            "shm",
        ]
    )
    assert planned.plan is True
    applied = parser.parse_args(
        [
            "identity-cutover",
            "--apply",
            "a" * 64,
            "--git-revision",
            _REVISION,
            "--downtime-token",
            "downtime-ok",
            "--snapshot-db",
            "db",
            "--snapshot-wal",
            "wal",
            "--snapshot-shm",
            "shm",
        ]
    )
    assert applied.apply == "a" * 64


def test_empty_plan_is_stable_and_does_not_flip_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "live.db"
    _upgrade(live, monkeypatch, "head")
    settings = _prepare_snapshots(live, tmp_path / "snap")
    first = _service(live, settings).plan()
    second = _service(live, settings).plan()
    assert first.status == "succeeded"
    assert second.status == "succeeded"
    assert first.source_fingerprint == second.source_fingerprint
    assert len(first.source_fingerprint) == 64
    assert first.source_fingerprint == first.source_fingerprint.lower()
    assert all(char in "0123456789abcdef" for char in first.source_fingerprint)
    rendered = render_cutover_report(first, "json")
    assert first.source_fingerprint in rendered
    assert "downtime-ok" not in rendered
    assert not looks_like_secret_or_path(rendered)
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert (
            connection.execute("SELECT source_fingerprint FROM identity_runtime_state").fetchone()[
                0
            ]
            is None
        )
        assert connection.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conversation_scopes").fetchone()[0] == 0
        stored = connection.execute(
            "SELECT fingerprint FROM identity_cutover_manifests"
        ).fetchone()[0]
        assert stored == first.source_fingerprint
        payload = json.loads(
            connection.execute("SELECT payload_json FROM identity_cutover_manifests").fetchone()[0]
        )
        assert is_sha256_hex(payload["decision_digest"])
        assert payload["source_fingerprint"] == first.source_fingerprint
        assert (
            connection.execute("SELECT COUNT(*) FROM identity_cutover_manifests").fetchone()[0] == 1
        )
    assert IdentityCutoverService.exit_code(first) == EXIT_OK


def test_plan_blocks_on_revision_lease_conflict_and_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "blocked.db"
    _upgrade(live, monkeypatch, "head")
    settings = _prepare_snapshots(live, tmp_path / "snap")
    wrong = CutoverSettingsInput(
        expected_git_revision="deadbeef",
        git_revision=_REVISION,
        downtime_token="downtime-ok",
        snapshot_db=settings.snapshot_db,
        snapshot_wal=settings.snapshot_wal,
        snapshot_shm=settings.snapshot_shm,
    )
    report = _service(live, wrong).plan()
    assert report.status == "blocked"
    assert report.error_category == "git_revision"
    assert IdentityCutoverService.exit_code(report) == EXIT_BLOCKED

    with _open(live) as connection:
        ids = _seed_identity(connection)
        first = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="m1",
            author_person_id=ids["person"],
        )
        connection.execute(
            "INSERT INTO memory_jobs("
            "event_id, conversation_key, status, attempts, next_attempt_at, "
            "created_at, updated_at, processing_source"
            ") VALUES (?, 'group:8000:2001', 'pending', 0, ?, ?, ?, 'live')",
            (first, _NOW, _NOW, _NOW),
        )
        connection.commit()
    report = _service(live, settings).plan()
    assert report.error_category == "lease_not_drained"

    with _open(live) as connection:
        connection.execute("DELETE FROM memory_jobs")
        connection.execute(
            "INSERT INTO identity_conflicts("
            "platform, external_id, subject_kind, conflict_kind, status, created_at, updated_at"
            ") VALUES ('qq', '1001', 'account', 'ambiguous_identity', 'open', ?, ?)",
            (_NOW, _NOW),
        )
        connection.commit()
    report = _service(live, settings).plan()
    assert report.error_category == "identity_conflicts"

    with _open(live) as connection:
        connection.execute("DELETE FROM identity_conflicts")
        _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="m1",
            author_person_id=ids["person"],
            content="other",
        )
        connection.commit()
    report = _service(live, settings).plan()
    assert report.error_category == "duplicate_content_conflict"


def test_identical_cross_presence_duplicates_are_suppressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "dup.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="same",
            author_person_id=ids["person"],
        )
        _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="same",
            author_person_id=ids["person"],
        )
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="other",
            author_person_id=ids["person"],
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "snap")
    report = _service(live, settings).plan()
    assert report.status == "succeeded", report.error_category
    assert report.counts.suppressed_events == 1


def test_pending_cutover_receipt_blocks_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "pending.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        _seed_identity(connection)
        connection.execute(
            "INSERT INTO control_command_receipts("
            "principal_id, request_id, payload_hash, status, problem_code, "
            "created_at, updated_at"
            ") VALUES (?, ?, ?, 'failed', 'pending_cutover', ?, ?)",
            (str(uuid4()), str(uuid4()), "a" * 64, _NOW, _NOW),
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "snap")
    report = _service(live, settings).plan()
    assert report.error_category == "pending_preconfiguration"


def test_apply_empty_and_populated_then_snapshot_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = tmp_path / "empty.db"
    _upgrade(empty, monkeypatch, "head")
    empty_settings = _prepare_snapshots(empty, tmp_path / "empty-snap")
    planned = _service(empty, empty_settings).plan()
    assert planned.source_fingerprint
    assert len(planned.source_fingerprint) == 64
    assert all(char in "0123456789abcdef" for char in planned.source_fingerprint)
    mismatched = _service(empty, empty_settings).apply("0" * 64)
    assert mismatched.status == "failed"
    assert mismatched.error_category == "manifest_missing"
    with _open(empty) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert (
            connection.execute("SELECT source_fingerprint FROM identity_runtime_state").fetchone()[
                0
            ]
            is None
        )
    snapshot_bytes = Path(empty_settings.snapshot_db).read_bytes()
    Path(empty_settings.snapshot_db).write_bytes(snapshot_bytes + b"\x00")
    stale = _service(empty, empty_settings).apply(planned.source_fingerprint)
    assert stale.status == "failed"
    assert stale.error_category == "source_fingerprint"
    with _open(empty) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert (
            connection.execute("SELECT source_fingerprint FROM identity_runtime_state").fetchone()[
                0
            ]
            is None
        )
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0
    Path(empty_settings.snapshot_db).write_bytes(snapshot_bytes)
    applied = _service(empty, empty_settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded"
    assert applied.source_fingerprint == planned.source_fingerprint
    with _open(empty) as connection:
        state_row = connection.execute(
            "SELECT state, source_fingerprint FROM identity_runtime_state"
        ).fetchone()
        assert state_row[0] == "v2"
        assert state_row[1] == planned.source_fingerprint
        jobs = connection.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0]
        assert jobs == 0

    live = tmp_path / "pop.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        event_id = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="keep",
            author_person_id=ids["person"],
        )
        connection.execute(
            "INSERT INTO memory_jobs("
            "event_id, conversation_key, status, attempts, next_attempt_at, "
            "created_at, updated_at, processing_source"
            ") VALUES (?, 'group:8000:2001', 'pending', 0, ?, ?, ?, 'live')",
            (event_id, _NOW, _NOW, _NOW),
        )
        connection.execute("DELETE FROM memory_jobs")
        connection.commit()
        del ids
    settings = _prepare_snapshots(live, tmp_path / "pop-snap")
    with _open(live) as connection:
        people_before = connection.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        groups_before = connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        scopes_before = connection.execute("SELECT COUNT(*) FROM conversation_scopes").fetchone()[0]
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    assert planned.source_fingerprint
    assert all(char in "0123456789abcdef" for char in planned.source_fingerprint)
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    assert applied.source_fingerprint == planned.source_fingerprint
    with _open(live) as connection:
        state_row = connection.execute(
            "SELECT state, source_fingerprint FROM identity_runtime_state"
        ).fetchone()
        assert state_row[0] == "v2"
        assert state_row[1] == planned.source_fingerprint
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM people").fetchone()[0] == people_before
        assert connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == groups_before
        assert scopes_before == 0
        assert connection.execute("SELECT COUNT(*) FROM conversation_scopes").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    restore_sqlite_snapshot(
        live,
        Path(settings.snapshot_db),
        Path(settings.snapshot_wal),
        Path(settings.snapshot_shm),
    )
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0


@pytest.mark.parametrize("failpoint", _FAILPOINTS)
def test_apply_failpoint_rolls_back_database_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failpoint: str,
) -> None:
    live = tmp_path / f"{failpoint}.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="fp",
            author_person_id=ids["person"],
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / f"{failpoint}-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    assert planned.source_fingerprint
    repo = IdentityCutoverRepository(live)
    with repo.connect() as connection:
        before_schema = repo.schema_signature(connection)
        before_business = repo.business_signature(connection)
        before_state = connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0]
        before_people = connection.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        before_groups = connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        before_scopes = connection.execute("SELECT COUNT(*) FROM conversation_scopes").fetchone()[0]
        before_conversations = connection.execute(
            "SELECT COUNT(*) FROM canonical_conversations"
        ).fetchone()[0]

    def trip(name: str) -> None:
        if name == failpoint:
            raise RuntimeError(failpoint)

    report = IdentityCutoverService(live, settings, failpoint=trip).apply(
        planned.source_fingerprint
    )
    assert report.status == "failed"
    assert report.error_category == "operational_error"
    with repo.connect() as connection:
        assert repo.schema_signature(connection) == before_schema
        assert repo.business_signature(connection) == before_business
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == (
            before_state
        )
        assert (
            connection.execute("SELECT source_fingerprint FROM identity_runtime_state").fetchone()[
                0
            ]
            is None
        )
        assert connection.execute("SELECT COUNT(*) FROM people").fetchone()[0] == before_people
        assert connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == before_groups
        assert (
            connection.execute("SELECT COUNT(*) FROM conversation_scopes").fetchone()[0]
            == before_scopes
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0]
            == before_conversations
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _assert_apply_stale_v1(live: Path, settings: CutoverSettingsInput, fingerprint: str) -> None:
    report = _service(live, settings).apply(fingerprint)
    assert report.status == "failed"
    assert report.error_category == "source_fingerprint"
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert (
            connection.execute("SELECT source_fingerprint FROM identity_runtime_state").fetchone()[
                0
            ]
            is None
        )


def test_source_tamper_event_relationship_route_keeps_v1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "tamper.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="keep",
            author_person_id=ids["person"],
        )
        _insert_relationship(connection, user_id="1001", person_id=ids["person"])
        _insert_person_route(
            connection,
            person_id=ids["person"],
            binding_id=ids["binding"],
            presence_id=ids["presence"],
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "tamper-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category

    with _open(live) as connection:
        before = connection.execute("SELECT COUNT(*) FROM chat_events").fetchone()[0]
        connection.execute("UPDATE chat_events SET content = 'changed-in-place' WHERE id = 1")
        assert connection.execute("SELECT COUNT(*) FROM chat_events").fetchone()[0] == before
        connection.commit()
    _assert_apply_stale_v1(live, settings, planned.source_fingerprint)

    with _open(live) as connection:
        connection.execute("UPDATE chat_events SET content = 'hello' WHERE id = 1")
        connection.commit()
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    with _open(live) as connection:
        before = connection.execute("SELECT COUNT(*) FROM person_relationships").fetchone()[0]
        connection.execute(
            "UPDATE person_relationships SET affection_score = 80 WHERE user_id = '1001'"
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM person_relationships").fetchone()[0] == before
        )
        connection.commit()
    _assert_apply_stale_v1(live, settings, planned.source_fingerprint)

    with _open(live) as connection:
        connection.execute(
            "UPDATE person_relationships SET affection_score = 50 WHERE user_id = '1001'"
        )
        connection.commit()
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    with _open(live) as connection:
        before = connection.execute("SELECT COUNT(*) FROM person_active_routes").fetchone()[0]
        connection.execute(
            "UPDATE person_active_routes SET paused = 1 WHERE person_id = ?", (ids["person"],)
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM person_active_routes").fetchone()[0] == before
        )
        connection.commit()
    _assert_apply_stale_v1(live, settings, planned.source_fingerprint)


def test_canonical_owner_duplicates_normalize_and_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "dups.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        second_binding = str(uuid4())
        second_space_binding = str(uuid4())
        connection.execute(
            "INSERT INTO identity_bindings("
            "id, person_id, platform, external_account_id, display_name, status, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, 'qq', '1002', '', 'active', 1, ?, ?)",
            (second_binding, ids["person"], _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO space_bindings("
            "id, space_id, platform, external_space_id, display_name, status, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, 'qq', '2002', '', 'active', 1, ?, ?)",
            (second_space_binding, ids["space"], _NOW, _NOW),
        )
        _pin_required_routes(connection, ids)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="shared",
            author_person_id=ids["person"],
            group_id="2001",
            content="same body",
            segments_json='{"b": 1, "a": 2}',
        )
        _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="shared",
            author_person_id=ids["person"],
            group_id="2002",
            content="same body",
            segments_json='{"a":2,"b":1}',
        )
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="priv-shared",
            author_kind="yuki",
            author_presence_id=ids["presence"],
            sender_user_id="8000",
            group_id=None,
            private_peer_user_id="1001",
            content="private-same",
        )
        _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="priv-shared",
            author_kind="yuki",
            author_presence_id=ids["presence"],
            sender_user_id="8000",
            group_id=None,
            private_peer_user_id="1002",
            content="private-same",
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "dups-snap")
    report = _service(live, settings).plan()
    assert report.status == "succeeded", report.error_category
    assert report.counts.suppressed_events == 2

    with _open(live) as connection:
        _insert_event(
            connection,
            bot_user_id="8002",
            platform_message_id="shared",
            author_person_id=ids["person"],
            group_id="2001",
            content="conflicted",
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "dups-conflict-snap")
    blocked = _service(live, settings).plan()
    assert blocked.error_category == "duplicate_content_conflict"


def test_routes_group_by_person_and_reject_cross_person(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "routes.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        extra_binding = str(uuid4())
        extra_presence = str(uuid4())
        other_person = str(uuid4())
        other_binding = str(uuid4())
        connection.execute(
            "INSERT INTO identity_bindings("
            "id, person_id, platform, external_account_id, display_name, status, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, 'qq', '1003', '', 'active', 1, ?, ?)",
            (extra_binding, ids["person"], _NOW, _NOW),
        )
        _insert_presence(connection, extra_presence, "8001")
        _pin_required_routes(connection, ids)
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "routes-snap")
    ok = _service(live, settings).plan()
    assert ok.status == "succeeded", ok.error_category

    with _open(live) as connection:
        connection.execute(
            "INSERT INTO persons(id, enabled, revision, created_at, updated_at) "
            "VALUES (?, 1, 1, ?, ?)",
            (other_person, _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO identity_bindings("
            "id, person_id, platform, external_account_id, display_name, status, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, 'qq', '1099', '', 'active', 1, ?, ?)",
            (other_binding, other_person, _NOW, _NOW),
        )
        _insert_person_route(
            connection,
            person_id=other_person,
            binding_id=other_binding,
            presence_id=extra_presence,
        )
        connection.execute("DROP TRIGGER IF EXISTS trg_person_active_routes_consistency_update")
        connection.execute(
            "UPDATE person_active_routes SET identity_binding_id = ? WHERE person_id = ?",
            (other_binding, ids["person"]),
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "routes-cross-snap")
    blocked = _service(live, settings).plan()
    assert blocked.error_category == "route_ambiguity"


def test_canonical_merge_rollup_and_v2_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_ai_bot.conversation.rollup.models import RollupKind, RollupPolicyConfig
    from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.persistence.database import Database
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    live = tmp_path / "merge.db"
    _upgrade(live, monkeypatch, "head")
    extra_presence = str(uuid4())
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_presence(connection, extra_presence, "8001")
        _pin_required_routes(connection, ids)
        first = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="s1-covered",
            author_person_id=ids["person"],
            content="alpha-covered",
        )
        first_suffix = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="s1-suffix",
            author_person_id=ids["person"],
            content="alpha-suffix",
        )
        second = _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="s2-covered",
            author_person_id=ids["person"],
            content="beta-covered",
        )
        second_suffix = _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="s2-suffix",
            author_person_id=ids["person"],
            content="beta-suffix",
        )
        scope_a = _insert_scope(
            connection,
            scope_key="bot:8000:group:2001",
            bot_user_id="8000",
            group_id="2001",
            last_event_id=first_suffix,
            starts_after_event_id=0,
        )
        scope_b = _insert_scope(
            connection,
            scope_key="bot:8001:group:2001",
            bot_user_id="8001",
            group_id="2001",
            last_event_id=second_suffix,
            starts_after_event_id=0,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=scope_a,
            covered_through_event_id=first,
            summary_text="alpha-rollup-context",
            fingerprint="a" * 64,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=scope_b,
            covered_through_event_id=second,
            summary_text="beta-rollup-context",
            fingerprint="b" * 64,
        )
        jobs_before = connection.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0]
        scopes_before = connection.execute("SELECT COUNT(*) FROM conversation_scopes").fetchone()[0]
        rollups_before = connection.execute("SELECT COUNT(*) FROM conversation_rollups").fetchone()[
            0
        ]
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "merge-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    primary_key = "bot:8000:group:2001" if scope_a < scope_b else "bot:8001:group:2001"
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v2"
        conversations = connection.execute(
            "SELECT id, covered_through_event_id, last_event_id, starts_after_event_id, "
            "uncovered_event_count FROM canonical_conversations"
        ).fetchall()
        assert len(conversations) == 1
        conversation_id = str(conversations[0][0])
        assert conversations[0][1] == conversations[0][2]
        assert conversations[0][3] == conversations[0][2]
        assert conversations[0][4] == 0
        aliases = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT scope_key, is_primary FROM conversation_legacy_aliases"
            )
        }
        assert "bot:8000:group:2001" in aliases
        assert "bot:8001:group:2001" in aliases
        assert aliases[primary_key] == 1
        assert sum(aliases.values()) == 1
        assert not any(key.startswith("cutover:") and aliases[key] == 1 for key in aliases)
        summary = connection.execute(
            "SELECT summary_text, summary_kind FROM canonical_conversation_rollups "
            "WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        assert summary is not None
        assert summary[1] == "migration"
        assert len(summary[0]) <= MIGRATION_SUMMARY_MAX_CHARACTERS
        assert "alpha-rollup-context" in summary[0]
        assert "beta-rollup-context" in summary[0]
        assert "alpha-suffix" in summary[0]
        assert "beta-suffix" in summary[0]
        assert "scope:" not in summary[0]
        assert "bot:8000:group:2001" not in summary[0]
        assert "bot:8001:group:2001" not in summary[0]
        assert scopes_before > 0
        assert rollups_before > 0
        assert connection.execute("SELECT COUNT(*) FROM conversation_scopes").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conversation_rollups").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0] == jobs_before
        suffix = connection.execute(
            "SELECT COUNT(*) FROM chat_events WHERE canonical_conversation_id = ? AND id > ?",
            (conversation_id, conversations[0][1]),
        ).fetchone()[0]
        assert suffix == 0

    policy = RollupPolicyConfig(
        raw_tail_events=2,
        raw_tail_characters=100_000,
        trigger_events=2,
        trigger_characters=100_000,
        stop_events=0,
        stop_characters=0,
        batch_max_events=100,
        batch_max_characters=100_000,
        summary_max_characters=2_000,
    )

    async def _complete_canonical_checkpoint() -> None:
        database = Database(f"sqlite+aiosqlite:///{live.as_posix()}")
        try:
            uow = ScopedEventLedgerUnitOfWork(database, config=policy)
            repository = ConversationRollupRepository(database, policy)
            scope = ConversationScope.group("8000", "2001")
            appended = None
            for index in range(1, 5):
                appended = await uow.append(
                    scope=scope,
                    platform_message_id=f"v2-{index}",
                    sender_user_id="1001",
                    direction="inbound",
                    content=f"v2-event-{index}",
                    occurred_at=datetime(2026, 8, 24, 1, index, tzinfo=UTC),
                )
                assert appended.created
            assert appended is not None
            assert appended.job_signalled is True
            state, _rollup, job = await repository.status(scope)
            assert state is not None
            assert job is not None
            claim = await repository.claim_next_job(lease_owner="test", lease_seconds=30)
            assert claim is not None
            assert claim.conversation_id is not None
            candidate = await repository.candidate_for_claim(claim)
            assert candidate is not None
            committed = await repository.commit_candidate(
                claim,
                candidate,
                summary_text="canonical-checkpoint",
                summary_kind=RollupKind.EXTRACTIVE,
            )
            assert committed.rollup.summary_kind is RollupKind.EXTRACTIVE
            assert committed.rollup.summary_text == "canonical-checkpoint"
        finally:
            await database.close()

    asyncio.run(_complete_canonical_checkpoint())


def test_apply_retires_legacy_scope_carriers_then_v2_forgetme_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import func, select

    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel
    from qq_ai_bot.identity.db_models import (
        CanonicalPersonModel,
        CanonicalSpaceModel,
        PresenceModel,
    )
    from qq_ai_bot.persistence.database import Database
    from qq_ai_bot.persistence.models import (
        ChatEventModel,
        GroupModel,
        MemoryJobModel,
        PersonModel,
    )
    from qq_ai_bot.persistence.repositories import PeopleRepository

    live = tmp_path / "cutover-forgetme.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _pin_required_routes(connection, ids)
        _insert_people(connection, "1001", canonical_person_id=ids["person"])
        _insert_group(connection, "2001", canonical_space_id=ids["space"])
        private_event = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="private-history",
            author_person_id=ids["person"],
            group_id=None,
            private_peer_user_id="1001",
            content="private-history",
        )
        group_event = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="group-history",
            author_person_id=ids["person"],
            content="group-history",
        )
        yuki_event = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="yuki-unrelated",
            author_kind="yuki",
            author_person_id=None,
            author_presence_id=ids["presence"],
            sender_user_id="8000",
            content="yuki-unrelated",
        )
        private_scope = _insert_scope(
            connection,
            scope_key="bot:8000:private:1001",
            bot_user_id="8000",
            private_peer_user_id="1001",
            last_event_id=private_event,
        )
        group_scope = _insert_scope(
            connection,
            scope_key="bot:8000:group:2001",
            bot_user_id="8000",
            group_id="2001",
            last_event_id=max(group_event, yuki_event),
        )
        _insert_legacy_rollup(
            connection,
            scope_id=private_scope,
            covered_through_event_id=private_event,
            summary_text="private-legacy-rollup",
            fingerprint="p" * 64,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=group_scope,
            covered_through_event_id=max(group_event, yuki_event),
            summary_text="group-legacy-rollup",
            fingerprint="g" * 64,
        )
        _insert_memory_job(
            connection,
            private_event,
            status="failed",
            conversation_key="private:1001",
        )
        scopes_before = connection.execute("SELECT COUNT(*) FROM conversation_scopes").fetchone()[0]
        rollups_before = connection.execute("SELECT COUNT(*) FROM conversation_rollups").fetchone()[
            0
        ]
        people_before = connection.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        groups_before = connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        connection.commit()
    assert scopes_before == 2
    assert rollups_before == 2
    assert people_before == 1
    assert groups_before == 1
    settings = _prepare_snapshots(live, tmp_path / "cutover-forgetme-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v2"
        assert connection.execute("SELECT COUNT(*) FROM conversation_scopes").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conversation_rollups").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM conversation_rollup_jobs").fetchone()[0] == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM people").fetchone()[0] == people_before
        assert connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == groups_before
        aliases = {
            str(row[0])
            for row in connection.execute("SELECT scope_key FROM conversation_legacy_aliases")
        }
        assert "bot:8000:private:1001" in aliases
        assert "bot:8000:group:2001" in aliases
        kinds = {
            str(row[0]) for row in connection.execute("SELECT kind FROM canonical_conversations")
        }
        assert kinds == {"private", "space"}
        assert (
            connection.execute("SELECT COUNT(*) FROM canonical_conversation_rollups").fetchone()[0]
            == 2
        )
        mapped = connection.execute(
            "SELECT COUNT(*) FROM chat_events WHERE canonical_conversation_id IS NOT NULL"
        ).fetchone()[0]
        assert mapped == 3
        assert connection.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        space_id = ids["space"]
        person_id = ids["person"]
        presence_id = ids["presence"]

    async def _forget_after_cutover() -> None:
        database = Database(f"sqlite+aiosqlite:///{live.as_posix()}")
        try:
            async with database.sessions() as session:
                scopes_at_forget = int(
                    await session.scalar(select(func.count()).select_from(ConversationScopeModel))
                    or 0
                )
                assert scopes_at_forget == 0
            assert await PeopleRepository(database).delete_person("1001") is True
            async with database.sessions() as session:
                assert await session.get(CanonicalPersonModel, person_id) is None
                assert await session.get(PersonModel, "1001") is None
                assert await session.get(GroupModel, "2001") is not None
                assert await session.get(CanonicalSpaceModel, space_id) is not None
                assert await session.get(PresenceModel, presence_id) is not None
                private_left = list(
                    await session.scalars(
                        select(CanonicalConversationModel).where(
                            CanonicalConversationModel.kind == "private"
                        )
                    )
                )
                assert private_left == []
                space_left = list(
                    await session.scalars(
                        select(CanonicalConversationModel).where(
                            CanonicalConversationModel.kind == "space"
                        )
                    )
                )
                assert len(space_left) == 1
                assert space_left[0].space_id == space_id
                remaining_ids = set(
                    await session.scalars(select(ChatEventModel.platform_message_id))
                )
                assert remaining_ids == {"yuki-unrelated"}
                assert (
                    int(
                        await session.scalar(
                            select(func.count()).select_from(ConversationScopeModel)
                        )
                        or 0
                    )
                    == 0
                )
                pending = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(MemoryJobModel)
                        .where(MemoryJobModel.status == "pending")
                    )
                    or 0
                )
                assert pending == 0
            raw = sqlite3.connect(live)
            try:
                raw.execute("PRAGMA foreign_keys=ON")
                assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
            finally:
                raw.close()
        finally:
            await database.close()

    asyncio.run(_forget_after_cutover())


def test_orphan_scope_without_alias_blocks_apply_and_keeps_legacy_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "orphan-scope.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _pin_required_routes(connection, ids)
        event_id = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="mapped",
            author_person_id=ids["person"],
        )
        mapped_scope = _insert_scope(
            connection,
            scope_key="bot:8000:group:2001",
            bot_user_id="8000",
            group_id="2001",
            last_event_id=event_id,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=mapped_scope,
            covered_through_event_id=event_id,
            summary_text="mapped-semantic",
            fingerprint="a" * 64,
        )
        _insert_scope(
            connection,
            scope_key="bot:8000:group:2999",
            bot_user_id="8000",
            group_id="2999",
            last_event_id=0,
        )
        before = _legacy_carrier_counts(connection)
        connection.commit()
    assert before == (2, 1, 0)
    settings = _prepare_snapshots(live, tmp_path / "orphan-scope-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "failed"
    assert applied.error_category == "canonical_kind_mismatch"
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert _legacy_carrier_counts(connection) == before


def test_legacy_semantic_without_canonical_representation_blocks_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "missing-canonical-rollup.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _pin_required_routes(connection, ids)
        event_id = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="legacy-semantic",
            author_person_id=ids["person"],
            content="legacy-body",
        )
        scope_id = _insert_scope(
            connection,
            scope_key="bot:8000:group:2001",
            bot_user_id="8000",
            group_id="2001",
            last_event_id=event_id,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=scope_id,
            covered_through_event_id=event_id,
            summary_text="LEGACY_SEMANTIC_TOKEN",
            fingerprint="b" * 64,
        )
        before = _legacy_carrier_counts(connection)
        connection.commit()
    assert before[0] == 1 and before[1] == 1
    settings = _prepare_snapshots(live, tmp_path / "missing-canonical-rollup-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    original = IdentityCutoverRepository._persist_migration_rollup

    def persist_empty(
        self: IdentityCutoverRepository,
        connection: sqlite3.Connection,
        conversation_id: str,
        mark: object,
        now: str,
    ) -> None:
        original(
            self,
            connection,
            conversation_id,
            replace(mark, migration_summary="migration-empty", rollup_fingerprint="e" * 64),
            now,
        )

    monkeypatch.setattr(IdentityCutoverRepository, "_persist_migration_rollup", persist_empty)
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "failed"
    assert applied.error_category == "populated_merge_forbidden"
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert _legacy_carrier_counts(connection) == before


def test_issue51_overlay_is_not_semantic_source_and_ledger_fills_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "issue51-overlay.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _pin_required_routes(connection, ids)
        covered = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="semantic-covered",
            author_person_id=ids["person"],
            content="semantic-early",
        )
        after_x = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="ledger-after-x",
            author_person_id=ids["person"],
            content="LEDGER_AFTER_X",
        )
        last = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="ledger-last",
            author_person_id=ids["person"],
            content="LEDGER_LAST",
        )
        scope_id = _insert_scope(
            connection,
            scope_key="bot:8000:group:2001",
            bot_user_id="8000",
            group_id="2001",
            last_event_id=last,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=scope_id,
            covered_through_event_id=covered,
            summary_text="SEMANTIC_CHECKPOINT",
            fingerprint="c" * 64,
        )
        _insert_emergency_overlay(
            connection,
            scope_id=scope_id,
            covered_through_event_id=last,
            summary_text="OVERLAY_ONLY_TOKEN",
            fingerprint="d" * 64,
        )
        before = _legacy_carrier_counts(connection)
        connection.commit()
    assert before == (1, 1, 1)
    assert after_x < last
    settings = _prepare_snapshots(live, tmp_path / "issue51-overlay-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v2"
        assert _legacy_carrier_counts(connection) == (0, 0, 0)
        row = connection.execute(
            "SELECT summary_text, summary_kind, source_fingerprint, covered_through_event_id "
            "FROM canonical_conversation_rollups"
        ).fetchone()
        assert row is not None
        summary = str(row[0])
        assert str(row[1]) == "migration"
        assert is_sha256_hex(str(row[2]))
        assert int(row[3]) == last
        assert "SEMANTIC_CHECKPOINT" in summary
        assert "LEDGER_AFTER_X" in summary
        assert "LEDGER_LAST" in summary
        assert summary != "OVERLAY_ONLY_TOKEN"
        assert "OVERLAY_ONLY_TOKEN" not in summary
        conversation = connection.execute(
            "SELECT covered_through_event_id, last_event_id FROM canonical_conversations"
        ).fetchone()
        assert int(conversation[0]) == last
        assert int(conversation[1]) == last
        payload = str(
            connection.execute("SELECT payload_json FROM identity_cutover_manifests").fetchone()[0]
        )
        assert "SEMANTIC_CHECKPOINT" not in payload
        assert "OVERLAY_ONLY_TOKEN" not in payload
        assert "LEDGER_AFTER_X" not in payload


@pytest.mark.parametrize(
    "failpoint",
    ("before_scope_carrier_retirement", "after_scope_carrier_retirement"),
)
def test_scope_carrier_retirement_failpoints_restore_legacy_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failpoint: str,
) -> None:
    live = tmp_path / f"{failpoint}-carriers.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _pin_required_routes(connection, ids)
        event_id = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="fp-carrier",
            author_person_id=ids["person"],
            content="fp-body",
        )
        scope_id = _insert_scope(
            connection,
            scope_key="bot:8000:group:2001",
            bot_user_id="8000",
            group_id="2001",
            last_event_id=event_id,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=scope_id,
            covered_through_event_id=event_id,
            summary_text="fp-semantic",
            fingerprint="f" * 64,
        )
        _insert_emergency_overlay(
            connection,
            scope_id=scope_id,
            covered_through_event_id=event_id,
            summary_text="fp-overlay",
            fingerprint="1" * 64,
        )
        before = _legacy_carrier_counts(connection)
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / f"{failpoint}-carriers-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category

    def trip(name: str) -> None:
        if name == failpoint:
            raise RuntimeError(failpoint)

    report = IdentityCutoverService(live, settings, failpoint=trip).apply(
        planned.source_fingerprint
    )
    assert report.status == "failed"
    assert report.error_category == "operational_error"
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert _legacy_carrier_counts(connection) == before


def test_processing_rollup_job_blocks_scope_carrier_retirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "processing-retire.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _pin_required_routes(connection, ids)
        event_id = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="processing-retire",
            author_person_id=ids["person"],
        )
        scope_id = _insert_scope(
            connection,
            scope_key="bot:8000:group:2001",
            bot_user_id="8000",
            group_id="2001",
            last_event_id=event_id,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=scope_id,
            covered_through_event_id=event_id,
            summary_text="processing-semantic",
            fingerprint="2" * 64,
        )
        before = _legacy_carrier_counts(connection)
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "processing-retire-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    repo = IdentityCutoverRepository(live)
    with repo.connect() as connection:
        snapshot = repo.snapshot_evidence(settings)
        connection.execute("BEGIN")
        plan = repo.build_plan(connection, settings, snapshot)
        connection.execute(
            "INSERT INTO conversation_rollup_jobs("
            "scope_id, generation, signal_revision, status, failure_count, "
            "lease_owner, lease_token, lease_until, next_attempt_at, created_at, updated_at"
            ") VALUES (?, 1, 1, 'processing', 0, 'owner', 'token', ?, ?, ?, ?)",
            (scope_id, _NOW, _NOW, _NOW, _NOW),
        )
        with pytest.raises(IdentityCutoverPreconditionError) as exc:
            repo.apply_plan(connection, plan)
        assert exc.value.category == "lease_not_drained"
        connection.rollback()
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert _legacy_carrier_counts(connection) == before


def test_migration_summary_helpers_bound_fairly_without_metadata() -> None:
    assert _assemble_migration_summary(()) == "migration-empty"
    assert _assemble_migration_summary((_MigrationScopeMaterial("", ()),)) == "migration-empty"
    assert _fair_allocate((10_000, 20, 20), 2_400) == (2_360, 20, 20)

    huge_first = _MigrationScopeMaterial("SCOPE_A_TOKEN " + ("A" * 8_000), ("early-A",))
    later_b = _MigrationScopeMaterial("SCOPE_B_TOKEN", ("early-B", "recent-B"))
    later_c = _MigrationScopeMaterial("SCOPE_C_TOKEN", ("early-C", "recent-C"))
    suffix_only = _MigrationScopeMaterial(
        "",
        ("EARLY_ONLY_SCOPE", "M" * 4_000, "RECENT_ONLY_SCOPE"),
    )
    first = _assemble_migration_summary((huge_first, later_b, later_c, suffix_only))
    second = _assemble_migration_summary((huge_first, later_b, later_c, suffix_only))
    assert first == second
    assert len(first) <= MIGRATION_SUMMARY_MAX_CHARACTERS
    assert "SCOPE_A_TOKEN" in first
    assert "SCOPE_B_TOKEN" in first
    assert "SCOPE_C_TOKEN" in first
    assert "early-B" in first
    assert "recent-B" in first
    assert "EARLY_ONLY_SCOPE" in first
    assert "RECENT_ONLY_SCOPE" in first
    assert "scope:" not in first
    assert "bot:" not in first

    semantic_first = _assemble_migration_summary(
        (
            _MigrationScopeMaterial(
                "SEMANTIC_KEEP " + ("S" * 5_000),
                ("SUFFIX_SHOULD_NOT_REPLACE_SEMANTIC",),
            ),
        )
    )
    assert len(semantic_first) <= MIGRATION_SUMMARY_MAX_CHARACTERS
    assert "SEMANTIC_KEEP" in semantic_first
    assert "SUFFIX_SHOULD_NOT_REPLACE_SEMANTIC" not in semantic_first


def test_migration_checkpoint_bounded_fair_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "migration-bound.db"
    _upgrade(live, monkeypatch, "head")
    extra_presence = str(uuid4())
    bot_canary = "BOTCANARYZX51"
    group_canary = "GROUPCANARYZX51"
    peer_canary = "PEERCANARYZX51"
    account_canary = "ACCTCANARYZX51"
    scope_a = f"bot:{bot_canary}:group:{group_canary}"
    scope_b = f"bot:8001:group:{group_canary}"
    private_scope = f"bot:{bot_canary}:private:{peer_canary}"
    with _open(live) as connection:
        ids = _seed_identity(connection)
        connection.execute(
            "UPDATE presences SET external_account_id = ? WHERE id = ?",
            (bot_canary, ids["presence"]),
        )
        connection.execute(
            "UPDATE space_bindings SET external_space_id = ? WHERE id = ?",
            (group_canary, ids["space_binding"]),
        )
        connection.execute(
            "UPDATE identity_bindings SET external_account_id = ? WHERE id = ?",
            (account_canary, ids["binding"]),
        )
        connection.execute(
            "INSERT INTO identity_bindings("
            "id, person_id, platform, external_account_id, display_name, status, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, 'qq', ?, '', 'active', 1, ?, ?)",
            (str(uuid4()), ids["person"], peer_canary, _NOW, _NOW),
        )
        _insert_presence(connection, extra_presence, "8001")
        _pin_required_routes(connection, ids)
        first_covered = _insert_event(
            connection,
            bot_user_id=bot_canary,
            platform_message_id="a-covered",
            author_person_id=ids["person"],
            sender_user_id=account_canary,
            group_id=group_canary,
            content="alpha-covered-hidden",
        )
        second_covered = _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="b-covered",
            author_person_id=ids["person"],
            sender_user_id=account_canary,
            group_id=group_canary,
            content="beta-covered-hidden",
        )
        _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="b-early",
            author_person_id=ids["person"],
            sender_user_id=account_canary,
            group_id=group_canary,
            content="EARLY_EVENT_TOKEN",
        )
        for index in range(8):
            _insert_event(
                connection,
                bot_user_id="8001",
                platform_message_id=f"b-mid-{index}",
                author_person_id=ids["person"],
                sender_user_id=account_canary,
                group_id=group_canary,
                content="MIDDLE_PAD_" + ("X" * 800),
            )
        _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="b-recent",
            author_person_id=ids["person"],
            sender_user_id=account_canary,
            group_id=group_canary,
            content="RECENT_EVENT_TOKEN",
        )
        keeper = _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="dup-mid",
            author_person_id=ids["person"],
            sender_user_id=account_canary,
            group_id=group_canary,
            content="SUPPRESSED_DUP_BODY",
        )
        suppressed = _insert_event(
            connection,
            bot_user_id=bot_canary,
            platform_message_id="dup-mid",
            author_person_id=ids["person"],
            sender_user_id=account_canary,
            group_id=group_canary,
            content="SUPPRESSED_DUP_BODY",
        )
        private_event = _insert_event(
            connection,
            bot_user_id=bot_canary,
            platform_message_id="p-keep",
            author_person_id=ids["person"],
            sender_user_id=peer_canary,
            group_id=None,
            private_peer_user_id=peer_canary,
            content="PRIVATE_CONTENT_TOKEN",
        )
        scope_a_id = _insert_scope(
            connection,
            scope_key=scope_a,
            bot_user_id=bot_canary,
            group_id=group_canary,
            last_event_id=suppressed,
            starts_after_event_id=0,
        )
        scope_b_id = _insert_scope(
            connection,
            scope_key=scope_b,
            bot_user_id="8001",
            group_id=group_canary,
            last_event_id=keeper,
            starts_after_event_id=0,
        )
        private_scope_id = _insert_scope(
            connection,
            scope_key=private_scope,
            bot_user_id=bot_canary,
            private_peer_user_id=peer_canary,
            last_event_id=private_event,
            starts_after_event_id=0,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=scope_a_id,
            covered_through_event_id=first_covered,
            summary_text="SCOPE_A_SEMANTIC " + ("A" * 6_000),
            fingerprint="a" * 64,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=scope_b_id,
            covered_through_event_id=second_covered,
            summary_text="SCOPE_B_SEMANTIC",
            fingerprint="b" * 64,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=private_scope_id,
            covered_through_event_id=0,
            summary_text="PRIVATE_SEMANTIC_TOKEN",
            fingerprint="c" * 64,
        )
        jobs_before = connection.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0]
        scopes_before = connection.execute("SELECT COUNT(*) FROM conversation_scopes").fetchone()[0]
        rollups_before = connection.execute("SELECT COUNT(*) FROM conversation_rollups").fetchone()[
            0
        ]
        connection.commit()

    settings = _prepare_snapshots(live, tmp_path / "migration-bound-snap")
    repository = IdentityCutoverRepository(live)
    with repository.connect() as connection:
        suppress_ids = _suppress_event_ids(repository, connection)
        first_marks = repository.conversation_watermarks(
            connection, suppress_event_ids=suppress_ids
        )
        second_marks = repository.conversation_watermarks(
            connection, suppress_event_ids=suppress_ids
        )
    assert [item.migration_summary for item in first_marks] == [
        item.migration_summary for item in second_marks
    ]
    assert [item.rollup_fingerprint for item in first_marks] == [
        item.rollup_fingerprint for item in second_marks
    ]
    assert all(item.suffix_event_count == 0 for item in first_marks)
    assert all(item.covered_through_event_id == item.last_event_id for item in first_marks)

    first_plan = _service(live, settings).plan()
    second_plan = _service(live, settings).plan()
    assert first_plan.status == "succeeded", first_plan.error_category
    assert second_plan.status == "succeeded", second_plan.error_category
    assert first_plan.source_fingerprint == second_plan.source_fingerprint

    applied = _service(live, settings).apply(first_plan.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        payload = str(
            connection.execute("SELECT payload_json FROM identity_cutover_manifests").fetchone()[0]
        )
        assert "SCOPE_A_SEMANTIC" not in payload
        assert "PRIVATE_CONTENT_TOKEN" not in payload
        assert "migration_summary" not in payload
        assert "last_event_id" not in payload
        assert "keeper_event_id" not in payload
        assert "owner_id" not in payload
        assert "primary_scope_key" not in payload
        assert scope_a not in payload
        parsed = json.loads(payload)
        assert is_sha256_hex(parsed["decision_digest"])
        rows = connection.execute(
            "SELECT kind, last_event_id, covered_through_event_id, uncovered_event_count, id "
            "FROM canonical_conversations ORDER BY kind"
        ).fetchall()
        assert {str(row[0]) for row in rows} == {"space", "private"}
        summaries: list[tuple[str, str, str]] = []
        for row in rows:
            assert int(row[1]) == int(row[2])
            assert int(row[3]) == 0
            summary = connection.execute(
                "SELECT summary_text, summary_kind, source_fingerprint "
                "FROM canonical_conversation_rollups WHERE conversation_id = ?",
                (row[4],),
            ).fetchone()
            assert summary is not None
            summaries.append((str(summary[0]), str(summary[1]), str(summary[2])))
        space_summary = next(text for text, _kind, _fp in summaries if "SCOPE_B_SEMANTIC" in text)
        private_summary = next(
            text for text, _kind, _fp in summaries if "PRIVATE_SEMANTIC_TOKEN" in text
        )
        assert all(kind == "migration" for _text, kind, _fp in summaries)
        assert all(len(text) <= MIGRATION_SUMMARY_MAX_CHARACTERS for text, _kind, _fp in summaries)
        assert "SCOPE_A_SEMANTIC" in space_summary
        assert "SCOPE_B_SEMANTIC" in space_summary
        assert "EARLY_EVENT_TOKEN" in space_summary
        assert "RECENT_EVENT_TOKEN" in space_summary
        assert space_summary.count("SUPPRESSED_DUP_BODY") == 1
        assert "PRIVATE_CONTENT_TOKEN" in private_summary
        for text, _kind, _fp in summaries:
            assert "scope:" not in text
            assert scope_a not in text
            assert scope_b not in text
            assert private_scope not in text
            assert bot_canary not in text
            assert group_canary not in text
            assert peer_canary not in text
            assert account_canary not in text
            assert extra_presence not in text
            assert ids["person"] not in text
            assert ids["space"] not in text
            assert ids["presence"] not in text
        assert scopes_before > 0
        assert rollups_before > 0
        assert connection.execute("SELECT COUNT(*) FROM conversation_scopes").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conversation_rollups").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0] == jobs_before
        for row in rows:
            suffix = connection.execute(
                "SELECT COUNT(*) FROM chat_events WHERE canonical_conversation_id = ? AND id > ?",
                (row[4], row[2]),
            ).fetchone()[0]
            assert suffix == 0
        stored = {
            str(row[4]): (text, kind, fp)
            for row, (text, kind, fp) in zip(rows, summaries, strict=True)
        }

    with repository.connect() as connection:
        for conversation_id, (text, kind, fingerprint) in stored.items():
            again = connection.execute(
                "SELECT summary_text, summary_kind, source_fingerprint, "
                "covered_through_event_id FROM canonical_conversation_rollups "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            assert again is not None
            assert str(again[0]) == text
            assert str(again[1]) == kind == "migration"
            assert str(again[2]) == fingerprint
        assert connection.execute("SELECT COUNT(*) FROM conversation_scopes").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conversation_rollups").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0] == jobs_before


def test_live_source_manifest_covers_orm_and_applied_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "complete.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        applied = set(applied_business_tables(connection))
        manifest = live_source_manifest(connection)
    orm = {name for name in Base.metadata.tables if not is_excluded_source_table(name)}
    assert set(manifest) == applied
    assert orm <= applied
    assert "admin_operation_events" in manifest
    assert "emoji_descriptions" in manifest
    assert "identity_cutover_manifests" not in manifest
    assert "identity_cutover_runs" not in manifest
    assert "alembic_version" not in manifest
    assert not any(name.startswith("sqlite_") for name in manifest)
    assert not any(name.endswith("_fts") or "_fts_" in name for name in manifest)


def test_missed_business_table_update_keeps_apply_v1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "missed.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="keep",
            author_person_id=ids["person"],
        )
        _insert_admin_operation(connection, operation="before")
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "missed-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    with _open(live) as connection:
        before = connection.execute("SELECT COUNT(*) FROM admin_operation_events").fetchone()[0]
        connection.execute("UPDATE admin_operation_events SET operation = 'after' WHERE id = 1")
        assert connection.execute("SELECT COUNT(*) FROM admin_operation_events").fetchone()[0] == (
            before
        )
        connection.commit()
    _assert_apply_stale_v1(live, settings, planned.source_fingerprint)


def test_manifest_payload_excludes_canaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "canary.db"
    _upgrade(live, monkeypatch, "head")
    body = "CANARY_BODY_UNIQUE_ZX9"
    qq = "19876543210"
    scope_key = "bot:19876543210:group:CANARY_SCOPE_KEY"
    with _open(live) as connection:
        ids = _seed_identity(connection)
        connection.execute(
            "UPDATE identity_bindings SET external_account_id = ? WHERE id = ?",
            (qq, ids["binding"]),
        )
        connection.execute(
            "UPDATE space_bindings SET external_space_id = ? WHERE id = ?",
            ("CANARY_SPACE_EXT", ids["space_binding"]),
        )
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="canary-msg",
            author_person_id=ids["person"],
            sender_user_id=qq,
            group_id="CANARY_SPACE_EXT",
            content=body,
        )
        _insert_scope(
            connection,
            scope_key=scope_key,
            bot_user_id="8000",
            group_id="CANARY_SPACE_EXT",
            last_event_id=1,
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "canary-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    with _open(live) as connection:
        payload = str(
            connection.execute("SELECT payload_json FROM identity_cutover_manifests").fetchone()[0]
        )
    parsed = json.loads(payload)
    assert is_sha256_hex(parsed["decision_digest"])
    assert body not in payload
    assert qq not in payload
    assert scope_key not in payload
    assert "CANARY_SPACE_EXT" not in payload
    assert "migration_summary" not in payload
    assert "last_event_id" not in payload
    assert "keeper_event_id" not in payload
    assert "owner_id" not in payload
    assert "primary_scope_key" not in payload
    assert "covered_scope_keys" not in payload
    assert ids["person"] not in payload
    assert ids["space"] not in payload
    assert ids["presence"] not in payload
    assert ids["binding"] not in payload


def test_empty_message_id_never_suppresses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "empty-mid.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="",
            author_person_id=ids["person"],
            content="same-empty",
        )
        _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="",
            author_person_id=ids["person"],
            content="same-empty",
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "empty-mid-snap")
    report = _service(live, settings).plan()
    assert report.status == "succeeded", report.error_category
    assert report.counts.suppressed_events == 0


def test_segments_key_order_only_no_opaque_fold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "segments.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="seg-shared",
            author_person_id=ids["person"],
            content="same",
            segments_json='{"url":"a  b","text":"hello"}',
        )
        _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="seg-shared",
            author_person_id=ids["person"],
            content="same",
            segments_json='{"text":"hello","url":"a  b"}',
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "segments-snap")
    ok = _service(live, settings).plan()
    assert ok.status == "succeeded", ok.error_category
    assert ok.counts.suppressed_events == 1

    with _open(live) as connection:
        _insert_event(
            connection,
            bot_user_id="8002",
            platform_message_id="seg-fold",
            author_person_id=ids["person"],
            content="same",
            segments_json='{"url":"a  b"}',
        )
        _insert_event(
            connection,
            bot_user_id="8003",
            platform_message_id="seg-fold",
            author_person_id=ids["person"],
            content="same",
            segments_json='{"url":"a b"}',
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "segments-fold-snap")
    blocked = _service(live, settings).plan()
    assert blocked.error_category == "duplicate_content_conflict"


def test_suppressed_excluded_from_migration_and_canonical_queries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "suppress-e2e.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        keeper = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="dup-mid",
            author_person_id=ids["person"],
            content="shared-body",
        )
        suppressed = _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="dup-mid",
            author_person_id=ids["person"],
            content="shared-body",
        )
        suffix = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="suffix-only",
            author_person_id=ids["person"],
            content="visible-suffix",
        )
        scope_id = _insert_scope(
            connection,
            scope_key="bot:8000:group:2001",
            bot_user_id="8000",
            group_id="2001",
            last_event_id=suffix,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=scope_id,
            covered_through_event_id=0,
            summary_text="legacy-empty",
            fingerprint="c" * 64,
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "suppress-e2e-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        summary = str(
            connection.execute(
                "SELECT summary_text FROM canonical_conversation_rollups"
            ).fetchone()[0]
        )
        assert summary.count("shared-body") == 1
        assert "visible-suffix" in summary
        statuses = {
            int(row[0]): str(row[1])
            for row in connection.execute("SELECT id, suppression_status FROM chat_events")
        }
        assert statuses[keeper] == "keeper"
        assert statuses[suppressed] == "duplicate"
        canonical_ids = {
            int(row[0]): str(row[1])
            for row in connection.execute("SELECT id, canonical_event_id FROM chat_events")
        }
        assert canonical_ids[keeper] == canonical_ids[suppressed]
        assert canonical_ids[keeper] != canonical_ids[suffix]
        conversation_id = str(
            connection.execute("SELECT id FROM canonical_conversations").fetchone()[0]
        )
        connection.execute(
            "UPDATE canonical_conversation_rollups SET covered_through_event_id = 0 "
            "WHERE conversation_id = ?",
            (conversation_id,),
        )
        connection.execute(
            "UPDATE canonical_conversations SET starts_after_event_id = 0, "
            "covered_through_event_id = 0 WHERE id = ?",
            (conversation_id,),
        )
        connection.commit()

    async def _assert_queries() -> None:
        from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
        from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
        from qq_ai_bot.domain.conversations import ConversationScope
        from qq_ai_bot.persistence.database import Database

        database = Database(f"sqlite+aiosqlite:///{live.as_posix()}")
        try:
            repository = ConversationRollupRepository(database, RollupPolicyConfig())
            snapshot = await repository.load_prompt_snapshot(
                ConversationScope.group("8000", "2001")
            )
            raw_ids = {event.id for event in snapshot.raw_events}
            assert keeper in raw_ids
            assert suffix in raw_ids
            assert suppressed not in raw_ids
        finally:
            await database.close()

    asyncio.run(_assert_queries())


def test_apply_receipts_for_onebot_not_plugin_and_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "receipts.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="onebot-keep",
            author_person_id=ids["person"],
        )
        _insert_plugin_external(
            connection,
            bot_user_id="8000",
            platform_message_id="plugin-ext",
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "receipts-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        receipts = list(
            connection.execute(
                "SELECT platform_message_id, event_type FROM canonical_event_receipts"
            )
        )
        assert len(receipts) == 1
        assert receipts[0][0] == "onebot-keep"
        assert receipts[0][1] == "message"
        before = connection.execute("SELECT COUNT(*) FROM chat_events").fetchone()[0]

    async def _reuse() -> None:
        from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
        from qq_ai_bot.domain.conversations import ConversationScope
        from qq_ai_bot.persistence.database import Database
        from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

        database = Database(f"sqlite+aiosqlite:///{live.as_posix()}")
        try:
            uow = ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())
            reused = await uow.append(
                scope=ConversationScope.group("8000", "2001"),
                platform_message_id="onebot-keep",
                sender_user_id="1001",
                direction="inbound",
                content="hello",
                occurred_at=datetime.fromisoformat(_NOW),
            )
            assert reused.created is False
            fresh = await uow.append(
                scope=ConversationScope.group("8000", "2001"),
                platform_message_id="onebot-new",
                sender_user_id="1001",
                direction="inbound",
                content="fresh",
            )
            assert fresh.created is True
        finally:
            await database.close()

    asyncio.run(_reuse())
    with _open(live) as connection:
        after = connection.execute("SELECT COUNT(*) FROM chat_events").fetchone()[0]
        assert after == before + 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM canonical_event_receipts "
                "WHERE platform_message_id = 'plugin-ext'"
            ).fetchone()[0]
            == 0
        )


def test_snapshot_reads_nonempty_wal_trio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "wal-live.db"
    _upgrade(live, monkeypatch, "head")
    connection = sqlite3.connect(live)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    try:
        ids = _seed_identity(connection)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="wal-keep",
            author_person_id=ids["person"],
        )
        _insert_admin_operation(connection, operation="before-wal")
        connection.commit()
        connection.execute("UPDATE admin_operation_events SET operation = 'in-wal' WHERE id = 1")
        connection.commit()
        wal = Path(str(live) + "-wal")
        shm = Path(str(live) + "-shm")
        assert wal.is_file() and wal.stat().st_size > 0
        snap = tmp_path / "wal-snap"
        snap.mkdir()
        snapshot_db = snap / "copy.db"
        snapshot_wal = snap / "copy.wal"
        snapshot_shm = snap / "copy.shm"
        shutil.copy2(live, snapshot_db)
        shutil.copy2(wal, snapshot_wal)
        if shm.exists():
            shutil.copy2(shm, snapshot_shm)
        else:
            snapshot_shm.write_bytes(b"")
        assert snapshot_wal.stat().st_size > 0
    finally:
        connection.close()

    settings = CutoverSettingsInput(
        expected_git_revision=_REVISION,
        git_revision=_REVISION,
        downtime_token="downtime-ok",
        snapshot_db=str(snapshot_db),
        snapshot_wal=str(snapshot_wal),
        snapshot_shm=str(snapshot_shm),
    )
    repo = IdentityCutoverRepository(live)
    with repo.connect() as live_connection:
        live_manifest = live_source_manifest(live_connection)
    assembled = repo.snapshot_source_manifest(settings)
    assert manifests_equal(live_manifest, assembled)
    naive_dir = tmp_path / "naive"
    naive_dir.mkdir()
    naive_db = naive_dir / "only.db"
    shutil.copy2(snapshot_db, naive_db)
    naive_connection = connect_sqlite(naive_db, readonly=True)
    try:
        naive_manifest = live_source_manifest(naive_connection)
    finally:
        naive_connection.close()
    assert not manifests_equal(naive_manifest, live_manifest)
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category


@pytest.mark.parametrize("missing", ("person", "ingest", "space"))
def test_multi_presence_requires_exact_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    live = tmp_path / f"multi-missing-{missing}.db"
    _upgrade(live, monkeypatch, "head")
    extra_presence = str(uuid4())
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_presence(connection, extra_presence, "8001")
        if missing != "person":
            _insert_person_route(
                connection,
                person_id=ids["person"],
                binding_id=ids["binding"],
                presence_id=ids["presence"],
            )
        if missing != "ingest":
            _insert_ingest_route(
                connection, space_binding_id=ids["space_binding"], presence_id=ids["presence"]
            )
        if missing != "space":
            _insert_space_route(
                connection,
                space_id=ids["space"],
                space_binding_id=ids["space_binding"],
                presence_id=ids["presence"],
            )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / f"multi-missing-{missing}-snap")
    report = _service(live, settings).plan()
    assert report.error_category == "route_ambiguity"
    applied = _service(live, settings).apply("0" * 64)
    assert applied.status == "failed"
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        if missing == "person":
            assert (
                connection.execute("SELECT COUNT(*) FROM person_active_routes").fetchone()[0] == 0
            )
        if missing == "ingest":
            assert (
                connection.execute("SELECT COUNT(*) FROM space_binding_ingest_routes").fetchone()[0]
                == 0
            )
        if missing == "space":
            assert connection.execute("SELECT COUNT(*) FROM space_active_routes").fetchone()[0] == 0


def test_unpaused_ingest_requires_ingest_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "ingest-eligible.db"
    _upgrade(live, monkeypatch, "head")
    extra_presence = str(uuid4())
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_presence(connection, extra_presence, "8001")
        _pin_required_routes(connection, ids)
        connection.execute(
            "UPDATE presences SET ingest_eligible = 0 WHERE id = ?",
            (ids["presence"],),
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "ingest-eligible-snap")
    report = _service(live, settings).plan()
    assert report.error_category == "route_ambiguity"

    with _open(live) as connection:
        connection.execute(
            "UPDATE space_binding_ingest_routes SET paused = 1 WHERE space_binding_id = ?",
            (ids["space_binding"],),
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "ingest-eligible-paused-snap")
    ok = _service(live, settings).plan()
    assert ok.status == "succeeded", ok.error_category


def test_single_presence_defaults_pause_or_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "single-defaults.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        connection.execute(
            "UPDATE presences SET ingest_eligible = 0 WHERE id = ?",
            (ids["presence"],),
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "single-defaults-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        assert (
            connection.execute(
                "SELECT paused FROM space_binding_ingest_routes WHERE space_binding_id = ?",
                (ids["space_binding"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT paused FROM person_active_routes WHERE person_id = ?",
                (ids["person"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT paused FROM space_active_routes WHERE space_id = ?",
                (ids["space"],),
            ).fetchone()[0]
            == 0
        )

    disabled = tmp_path / "single-disabled.db"
    _upgrade(disabled, monkeypatch, "head")
    with _open(disabled) as connection:
        ids = _seed_identity(connection)
        connection.execute("UPDATE presences SET enabled = 0 WHERE id = ?", (ids["presence"],))
        connection.commit()
    disabled_settings = _prepare_snapshots(disabled, tmp_path / "single-disabled-snap")
    blocked = _service(disabled, disabled_settings).plan()
    assert blocked.error_category == "route_ambiguity"


def test_apply_receipts_for_suppressed_and_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "receipts-suppressed.db"
    _upgrade(live, monkeypatch, "head")
    extra_presence = str(uuid4())
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_presence(connection, extra_presence, "8001")
        _pin_required_routes(connection, ids)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="dup-keep",
            author_person_id=ids["person"],
            content="shared-body",
        )
        _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="dup-keep",
            author_person_id=ids["person"],
            content="shared-body",
        )
        _insert_plugin_external(
            connection,
            bot_user_id="8000",
            platform_message_id="plugin-ext",
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "receipts-suppressed-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        rows = list(
            connection.execute(
                "SELECT ingress_presence_id, platform_message_id, canonical_event_id "
                "FROM canonical_event_receipts"
            )
        )
        assert len(rows) == 2
        assert {str(row[0]) for row in rows} == {ids["presence"], extra_presence}
        assert {str(row[1]) for row in rows} == {"dup-keep"}
        event_canonical = {
            str(row[0])
            for row in connection.execute(
                "SELECT canonical_event_id FROM chat_events WHERE platform_message_id = 'dup-keep'"
            )
        }
        assert len(event_canonical) == 1
        assert {str(row[2]) for row in rows} == event_canonical
        before = connection.execute("SELECT COUNT(*) FROM chat_events").fetchone()[0]

    async def _replay_exact() -> None:
        from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
        from qq_ai_bot.domain.conversations import ConversationScope
        from qq_ai_bot.persistence.database import Database
        from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

        database = Database(f"sqlite+aiosqlite:///{live.as_posix()}")
        try:
            uow = ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())
            reused = await uow.append(
                scope=ConversationScope.group("8000", "2001"),
                platform_message_id="dup-keep",
                sender_user_id="1001",
                direction="inbound",
                content="shared-body",
                occurred_at=datetime.fromisoformat(_NOW),
            )
            assert reused.created is False
        finally:
            await database.close()

    asyncio.run(_replay_exact())
    with _open(live) as connection:
        after = connection.execute("SELECT COUNT(*) FROM chat_events").fetchone()[0]
        assert after == before
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM canonical_event_receipts "
                "WHERE platform_message_id = 'plugin-ext'"
            ).fetchone()[0]
            == 0
        )

    conflict = tmp_path / "receipts-conflict.db"
    _upgrade(conflict, monkeypatch, "head")
    with _open(conflict) as connection:
        ids = _seed_identity(connection)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="onebot-keep",
            author_person_id=ids["person"],
        )
        connection.execute(
            "INSERT INTO canonical_event_receipts("
            "ingress_presence_id, event_type, platform_message_id, "
            "canonical_event_id, created_at, observed_at"
            ") VALUES (?, 'message', ?, ?, ?, ?)",
            (ids["presence"], "onebot-keep", str(uuid4()), _NOW, _NOW),
        )
        connection.commit()
    conflict_settings = _prepare_snapshots(conflict, tmp_path / "receipts-conflict-snap")
    planned = _service(conflict, conflict_settings).plan()
    assert planned.status == "succeeded", planned.error_category
    blocked = _service(conflict, conflict_settings).apply(planned.source_fingerprint)
    assert blocked.status == "failed"
    assert blocked.error_category == "receipt_conflict"
    with _open(conflict) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"


def test_binary_epoch_v1_and_v2_refuse_each_other(monkeypatch: pytest.MonkeyPatch) -> None:
    assert IDENTITY_BINARY_EPOCH == "v2"
    refuse_identity_binary_epoch("v2")
    with pytest.raises(IdentityDualWriteError) as exc:
        refuse_identity_binary_epoch("v1")
    assert exc.value.category == "identity_binary_epoch"
    monkeypatch.setattr("qq_ai_bot.identity.binary_epoch.IDENTITY_BINARY_EPOCH", "v1")
    from qq_ai_bot.identity import binary_epoch

    with pytest.raises(IdentityDualWriteError):
        binary_epoch.refuse_identity_binary_epoch("v2")
    binary_epoch.refuse_identity_binary_epoch("v1")


def test_pending_rollup_jobs_block_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "rollup-pending.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        scope_id = _insert_scope(
            connection,
            scope_key="bot:8000:group:2001",
            bot_user_id="8000",
            last_event_id=0,
            group_id="2001",
        )
        connection.execute(
            "INSERT INTO conversation_rollup_jobs("
            "scope_id, generation, signal_revision, status, failure_count, "
            "next_attempt_at, created_at, updated_at"
            ") VALUES (?, 1, 1, 'pending', 0, ?, ?, ?)",
            (scope_id, _NOW, _NOW, _NOW),
        )
        conversation_id = str(uuid4())
        alias_id = str(uuid4())
        connection.execute(
            "INSERT INTO canonical_conversations("
            "id, kind, person_id, space_id, primary_alias_id, primary_marker, generation, "
            "starts_after_event_id, last_event_id, last_generation_change_event_id, "
            "covered_through_event_id, uncovered_event_count, uncovered_character_count, "
            "revision, created_at, updated_at"
            ") VALUES (?, 'space', NULL, ?, ?, 1, 1, 0, 0, 0, 0, 0, 0, 1, ?, ?)",
            (conversation_id, ids["space"], alias_id, _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO conversation_legacy_aliases("
            "id, conversation_id, scope_key, is_primary, created_at, updated_at"
            ") VALUES (?, ?, ?, 1, ?, ?)",
            (alias_id, conversation_id, f"scope:{alias_id}", _NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO canonical_conversation_rollup_jobs("
            "conversation_id, generation, signal_revision, status, failure_count, "
            "next_attempt_at, created_at, updated_at"
            ") VALUES (?, 1, 1, 'pending', 0, ?, ?, ?)",
            (conversation_id, _NOW, _NOW, _NOW),
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "rollup-pending-snap")
    report = _service(live, settings).plan()
    assert report.error_category == "lease_not_drained"


def test_expired_processing_rows_block_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "expired-processing.db"
    _upgrade(live, monkeypatch, "head")
    expired = "2020-01-01T00:00:00+00:00"
    with _open(live) as connection:
        ids = _seed_identity(connection)
        event_id = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="expired-job",
            author_person_id=ids["person"],
        )
        connection.execute(
            "INSERT INTO memory_jobs("
            "event_id, conversation_key, status, attempts, next_attempt_at, "
            "created_at, updated_at, processing_source"
            ") VALUES (?, 'group:8000:2001', 'processing', 1, ?, ?, ?, 'live')",
            (event_id, expired, _NOW, _NOW),
        )
        scope_id = _insert_scope(
            connection,
            scope_key="bot:8000:group:2001",
            bot_user_id="8000",
            last_event_id=event_id,
            group_id="2001",
        )
        connection.execute(
            "INSERT INTO conversation_rollup_jobs("
            "scope_id, generation, signal_revision, status, failure_count, "
            "lease_owner, lease_token, lease_until, next_attempt_at, created_at, updated_at"
            ") VALUES (?, 1, 1, 'processing', 0, 'owner', 'token', ?, ?, ?, ?)",
            (scope_id, expired, _NOW, _NOW, _NOW),
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "expired-processing-snap")
    report = _service(live, settings).plan()
    assert report.error_category == "lease_not_drained"


def test_yuki_people_row_does_not_require_person_shadow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "yuki-people.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_people(connection, "8000", is_bot=1)
        _insert_people(connection, "1001", canonical_person_id=ids["person"])
        _insert_group(connection, "2001", canonical_space_id=ids["space"])
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "yuki-people-snap")
    report = _service(live, settings).plan()
    assert report.status == "succeeded", report.error_category


def test_legacy_bot_people_with_person_shadow_blocks_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "bot-person-shadow.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_people(connection, "9000", is_bot=1, canonical_person_id=ids["person"])
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "bot-person-shadow-snap")
    report = _service(live, settings).plan()
    assert report.error_category == "shadows_incomplete"


@pytest.mark.parametrize(
    "kind",
    ("membership_null", "membership_mismatch", "memory_null", "automation_null", "plugin_null"),
)
def test_shadow_null_and_mismatch_block_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    live = tmp_path / f"shadow-{kind}.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        other_person = str(uuid4())
        _insert_person(connection, other_person)
        _insert_people(connection, "1001", canonical_person_id=ids["person"])
        _insert_group(connection, "2001", canonical_space_id=ids["space"])
        if kind == "membership_null":
            connection.execute(
                "INSERT INTO memberships(user_id, group_id, group_card, first_seen_at, "
                "last_seen_at) VALUES ('1001', '2001', '', ?, ?)",
                (_NOW, _NOW),
            )
        elif kind == "membership_mismatch":
            connection.execute(
                "INSERT INTO memberships(user_id, group_id, group_card, first_seen_at, "
                "last_seen_at, canonical_person_id, canonical_space_id) "
                "VALUES ('1001', '2001', '', ?, ?, ?, ?)",
                (_NOW, _NOW, other_person, ids["space"]),
            )
        elif kind == "memory_null":
            _insert_memory_fact_person(
                connection, subject_user_id="1001", canonical_subject_person_id=None
            )
        elif kind == "automation_null":
            _insert_automation_row(connection)
        else:
            _seed_plugin_install(connection)
            connection.execute(
                "INSERT INTO plugin_background_target_grants("
                "plugin_id, target_type, target_id, bot_user_id, enabled, "
                "created_by_user_id, created_at, updated_at"
                ") VALUES ('fixture', 'private', '1001', '8000', 1, '1001', ?, ?)",
                (_NOW, _NOW),
            )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / f"shadow-{kind}-snap")
    report = _service(live, settings).plan()
    assert report.error_category == "shadows_incomplete"


def test_shadow_mismatch_across_automation_plugin_emoji(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "shadow-mismatch-more.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        other_person = str(uuid4())
        other_space = str(uuid4())
        _insert_person(connection, other_person)
        _insert_space(connection, other_space)
        _insert_people(connection, "1001", canonical_person_id=ids["person"])
        _insert_group(connection, "2001", canonical_space_id=ids["space"])
        extra_presence = str(uuid4())
        _insert_presence(connection, extra_presence, "8001")
        _insert_automation_row(
            connection,
            canonical_creator_person_id=ids["person"],
            canonical_presence_id=extra_presence,
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "shadow-mismatch-auto-snap")
    report = _service(live, settings).plan()
    assert report.error_category == "shadows_incomplete"

    with _open(live) as connection:
        connection.execute("DELETE FROM automations")
        _seed_plugin_install(connection)
        connection.execute(
            "INSERT INTO plugin_background_target_grants("
            "plugin_id, target_type, target_id, bot_user_id, enabled, "
            "created_by_user_id, created_at, updated_at, canonical_target_person_id, "
            "canonical_created_by_person_id, canonical_presence_id"
            ") VALUES ('fixture', 'private', '1001', '8000', 1, '1001', ?, ?, ?, ?, ?)",
            (_NOW, _NOW, other_person, ids["person"], ids["presence"]),
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "shadow-mismatch-plugin-snap")
    report = _service(live, settings).plan()
    assert report.error_category == "shadows_incomplete"

    with _open(live) as connection:
        connection.execute("DELETE FROM plugin_background_target_grants")
        _insert_emoji_usage(
            connection,
            actor_user_id="1001",
            group_id="2001",
            canonical_actor_person_id=ids["person"],
            canonical_space_id=other_space,
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "shadow-mismatch-emoji-snap")
    report = _service(live, settings).plan()
    assert report.error_category == "shadows_incomplete"


def test_nullable_inapplicable_shadows_remain_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "shadow-inapplicable.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_people(connection, "1001", canonical_person_id=ids["person"])
        _insert_people(connection, "8000", is_bot=1)
        _insert_group(connection, "2001", canonical_space_id=ids["space"])
        connection.execute(
            "INSERT INTO memberships(user_id, group_id, group_card, first_seen_at, "
            "last_seen_at, canonical_person_id, canonical_space_id) "
            "VALUES ('8000', '2001', '', ?, ?, NULL, ?)",
            (_NOW, _NOW, ids["space"]),
        )
        _insert_automation_row(
            connection,
            canonical_creator_person_id=ids["person"],
            canonical_presence_id=ids["presence"],
            status="cancelled",
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "shadow-inapplicable-snap")
    report = _service(live, settings).plan()
    assert report.status == "succeeded", report.error_category


def test_cutover_shadow_inventory_has_no_drift() -> None:
    missing, extra = shadow_inventory_drift()
    assert not missing
    assert not extra
    assert {spec.dotted for spec in SHADOW_FILL_SPECS}
    assert shadow_spec_policy_errors() == ()
    assert SHAPE_ONLY_OPTIONAL_SHADOWS == {
        "automations.canonical_target_person_id",
        "automations.canonical_target_space_id",
        "runtime_turn_observations.canonical_person_id",
        "runtime_turn_observations.canonical_space_id",
    }
    by_name = {spec.dotted: spec for spec in SHADOW_FILL_SPECS}
    for dotted in SHAPE_ONLY_OPTIONAL_SHADOWS:
        spec = by_name[dotted]
        assert spec.completeness == "shape_only_optional"
        assert spec.source_column is None
    verified = [spec for spec in SHADOW_FILL_SPECS if spec.completeness == "verified_from_source"]
    assert verified
    assert all(spec.source_column is not None for spec in verified)
    assert EVENT_AUTHOR_KINDS == frozenset({"person", "yuki", "external_bot", "system"})


def test_null_author_kind_blocks_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "author-null.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        _seed_identity(connection)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="unclassified",
            author_kind=None,
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "author-null-snap")
    report = _service(live, settings).plan()
    assert report.error_category == "shadows_incomplete"


def test_invalid_author_kind_is_constraint_or_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "author-invalid.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        _seed_identity(connection)
        try:
            _insert_event(
                connection,
                bot_user_id="8000",
                platform_message_id="invalid-kind",
                author_kind="plugin",
            )
        except sqlite3.IntegrityError:
            connection.rollback()
            inserted = False
            triggers = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE '%chat_events_canonical_shadow%'"
            ).fetchall()
            assert triggers
        else:
            connection.commit()
            inserted = True
    if not inserted:
        return
    settings = _prepare_snapshots(live, tmp_path / "author-invalid-snap")
    report = _service(live, settings).plan()
    assert report.error_category == "shadows_incomplete"


def test_plugin_system_and_external_bot_authors_remain_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "author-valid-kinds.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="person-ok",
            author_person_id=ids["person"],
        )
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="yuki-ok",
            author_kind="yuki",
            author_presence_id=ids["presence"],
            sender_user_id="8000",
        )
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="external-bot-ok",
            author_kind="external_bot",
        )
        _insert_plugin_external(
            connection,
            bot_user_id="8000",
            platform_message_id="plugin-system-ok",
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "author-valid-kinds-snap")
    report = _service(live, settings).plan()
    assert report.status == "succeeded", report.error_category


def test_person_author_must_match_sender_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "author-person-mismatch.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        _seed_identity(connection)
        other_person = str(uuid4())
        _insert_person(connection, other_person)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="person-mismatch",
            author_person_id=other_person,
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "author-person-mismatch-snap")
    report = _service(live, settings).plan()
    assert report.error_category == "shadows_incomplete"


def test_yuki_author_must_match_sender_presence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "author-yuki-mismatch.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        _seed_identity(connection)
        extra_presence = str(uuid4())
        _insert_presence(connection, extra_presence, "8001")
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="yuki-mismatch",
            author_kind="yuki",
            author_presence_id=extra_presence,
            sender_user_id="8000",
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "author-yuki-mismatch-snap")
    report = _service(live, settings).plan()
    assert report.error_category == "shadows_incomplete"


def test_shape_only_optional_nulls_remain_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "shape-only-nulls.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        _insert_automation_row(
            connection,
            canonical_creator_person_id=ids["person"],
            canonical_presence_id=ids["presence"],
            status="cancelled",
        )
        connection.execute(
            "INSERT INTO runtime_turn_observations("
            "runtime_turn_id, origin, scope_type, handled, sent_messages, "
            "total_latency_ms, created_at, expires_at"
            ") VALUES ('turn-shape', 'user_message', 'group', 1, 0, 0, ?, ?)",
            (_NOW, _NOW),
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "shape-only-nulls-snap")
    report = _service(live, settings).plan()
    assert report.status == "succeeded", report.error_category


def test_shape_only_optional_resolved_ids_remain_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "shape-only-filled.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        connection.execute(
            "INSERT INTO automations("
            "creator_user_id, bot_user_id, name, status, timezone, schedule_json, "
            "script_json, script_hash, required_capabilities_json, authority_snapshot_json, "
            "created_from_message_id, run_count, consecutive_failures, misfire_grace_seconds, "
            "created_at, updated_at, canonical_creator_person_id, canonical_presence_id, "
            "canonical_target_person_id"
            ") VALUES ('1001', '8000', 'auto-target', 'active', 'Asia/Shanghai', '{}', '{}', "
            "'hash', '[]', '{}', 'event-1', 0, 0, 1800, ?, ?, ?, ?, ?)",
            (_NOW, _NOW, ids["person"], ids["presence"], ids["person"]),
        )
        connection.execute(
            "INSERT INTO runtime_turn_observations("
            "runtime_turn_id, origin, scope_type, handled, sent_messages, "
            "total_latency_ms, created_at, expires_at, canonical_person_id"
            ") VALUES ('turn-private', 'user_message', 'private', 1, 0, 0, ?, ?, ?)",
            (_NOW, _NOW, ids["person"]),
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "shape-only-filled-snap")
    report = _service(live, settings).plan()
    assert report.status == "succeeded", report.error_category


def test_plan_and_apply_block_when_application_lock_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "lock-held.db"
    _upgrade(live, monkeypatch, "head")
    settings = _prepare_snapshots(live, tmp_path / "lock-held-snap")
    holder = SQLiteApplicationLock(live)
    holder.acquire()
    try:
        planned = _service(live, settings).plan()
        assert planned.status == "blocked"
        assert planned.error_category == "downtime_evidence"
        applied = _service(live, settings).apply("0" * 64)
        assert applied.status == "blocked"
        assert applied.error_category == "downtime_evidence"
    finally:
        holder.release()


def test_snapshot_live_paths_are_downtime_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "live-path.db"
    _upgrade(live, monkeypatch, "head")
    snap = tmp_path / "live-path-snap"
    settings = _prepare_snapshots(live, snap)
    live_settings = CutoverSettingsInput(
        expected_git_revision=_REVISION,
        git_revision=_REVISION,
        downtime_token="downtime-ok",
        snapshot_db=str(live),
        snapshot_wal=settings.snapshot_wal,
        snapshot_shm=settings.snapshot_shm,
    )
    report = _service(live, live_settings).plan()
    assert report.error_category == "downtime_evidence"
    companion = CutoverSettingsInput(
        expected_git_revision=_REVISION,
        git_revision=_REVISION,
        downtime_token="downtime-ok",
        snapshot_db=settings.snapshot_db,
        snapshot_wal=str(Path(str(live) + "-wal")),
        snapshot_shm=settings.snapshot_shm,
    )
    (Path(str(live) + "-wal")).write_bytes(b"")
    report = _service(live, companion).plan()
    assert report.error_category == "downtime_evidence"


def test_run_manifest_and_report_omit_raw_token_and_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "privacy.db"
    _upgrade(live, monkeypatch, "head")
    settings = _prepare_snapshots(live, tmp_path / "privacy-snap")
    report = _service(live, settings).plan()
    assert report.status == "succeeded", report.error_category
    rendered = render_cutover_report(report, "json")
    assert settings.downtime_token not in rendered
    assert settings.snapshot_db not in rendered
    assert settings.snapshot_wal not in rendered
    assert settings.snapshot_shm not in rendered
    assert not looks_like_secret_or_path(rendered)
    with _open(live) as connection:
        row = connection.execute(
            "SELECT downtime_token, snapshot_db, snapshot_wal, snapshot_shm "
            "FROM identity_cutover_runs"
        ).fetchone()
        payload = str(
            connection.execute("SELECT payload_json FROM identity_cutover_manifests").fetchone()[0]
        )
        stored = (str(row[0]), str(row[1]), str(row[2]), str(row[3]), payload)
        joined = "\n".join(stored)
        assert settings.downtime_token not in joined
        assert settings.snapshot_db not in joined
        assert settings.snapshot_wal not in joined
        assert settings.snapshot_shm not in joined
        assert str(live) not in joined
        assert row[0] == downtime_token_digest(settings.downtime_token)
        assert ":" in str(row[1])
        assert not looks_like_secret_or_path(str(row[1]))
        assert not looks_like_secret_or_path(payload)


def test_apply_retires_only_per_conversation_memory_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "memory-baseline.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        private_covered = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="private-covered",
            author_person_id=ids["person"],
            group_id=None,
            private_peer_user_id="1001",
            content="private-covered",
        )
        space_early = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="space-early",
            author_person_id=ids["person"],
            content="space-early",
        )
        private_uncovered = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="private-uncovered",
            author_person_id=ids["person"],
            group_id=None,
            private_peer_user_id="1001",
            content="private-uncovered",
        )
        space_late = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="space-late",
            author_person_id=ids["person"],
            content="space-late",
        )
        assert private_uncovered < space_late
        private_scope = _insert_scope(
            connection,
            scope_key="bot:8000:private:1001",
            bot_user_id="8000",
            private_peer_user_id="1001",
            last_event_id=private_covered,
        )
        space_scope = _insert_scope(
            connection,
            scope_key="bot:8000:group:2001",
            bot_user_id="8000",
            group_id="2001",
            last_event_id=space_late,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=private_scope,
            covered_through_event_id=private_covered,
            summary_text="private-migration",
            fingerprint="p" * 64,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=space_scope,
            covered_through_event_id=space_late,
            summary_text="space-migration",
            fingerprint="s" * 64,
        )
        _insert_memory_job(
            connection,
            private_covered,
            conversation_key="private:1001",
        )
        _insert_memory_job(
            connection,
            space_early,
            status="done",
            conversation_key="group:8000:2001",
        )
        _insert_memory_job(
            connection,
            private_uncovered,
            conversation_key="private:1001",
        )
        _insert_memory_job(
            connection,
            space_late,
            conversation_key="group:8000:2001",
        )
        jobs_before = {
            int(row[0]): str(row[1])
            for row in connection.execute("SELECT event_id, status FROM memory_jobs")
        }
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "memory-baseline-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        remaining = {
            int(row[0]): str(row[1])
            for row in connection.execute("SELECT event_id, status FROM memory_jobs")
        }
        assert remaining == {
            space_early: "done",
            private_uncovered: "failed",
        }
        assert set(remaining) < set(jobs_before)
        starts_after = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT kind, starts_after_event_id FROM canonical_conversations"
            )
        }
        assert starts_after["private"] == private_covered
        assert starts_after["space"] == space_late
        mapped = {
            int(row[0]): str(row[1])
            for row in connection.execute("SELECT id, canonical_conversation_id FROM chat_events")
        }
        private_conversation = connection.execute(
            "SELECT id FROM canonical_conversations WHERE kind = 'private'"
        ).fetchone()[0]
        space_conversation = connection.execute(
            "SELECT id FROM canonical_conversations WHERE kind = 'space'"
        ).fetchone()[0]
        assert mapped[private_uncovered] == str(private_conversation)
        assert mapped[space_late] == str(space_conversation)
        assert connection.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM canonical_conversation_rollups "
                "WHERE summary_kind = 'migration'"
            ).fetchone()[0]
            == 2
        )


def test_apply_failpoint_preserves_memory_jobs_and_baselines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "memory-failpoint.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        low = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="fp-private",
            author_person_id=ids["person"],
            group_id=None,
            private_peer_user_id="1001",
        )
        high = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="fp-space",
            author_person_id=ids["person"],
        )
        _insert_scope(
            connection,
            scope_key="bot:8000:private:1001",
            bot_user_id="8000",
            private_peer_user_id="1001",
            last_event_id=low,
        )
        _insert_scope(
            connection,
            scope_key="bot:8000:group:2001",
            bot_user_id="8000",
            group_id="2001",
            last_event_id=high,
        )
        _insert_memory_job(connection, low, conversation_key="private:1001")
        _insert_memory_job(connection, high, conversation_key="group:8000:2001")
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "memory-failpoint-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category

    def trip(name: str) -> None:
        if name == "after_baselines":
            raise RuntimeError("after_baselines")

    report = IdentityCutoverService(live, settings, failpoint=trip).apply(
        planned.source_fingerprint
    )
    assert report.status == "failed"
    assert report.error_category == "operational_error"
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0
        jobs = {
            int(row[0]): str(row[1])
            for row in connection.execute("SELECT event_id, status FROM memory_jobs")
        }
        assert jobs == {low: "failed", high: "failed"}
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chat_events WHERE canonical_event_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


def test_apply_same_presence_and_receipt_duplicate_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "same-presence.db"
    _upgrade(live, monkeypatch, "head")
    shared = str(uuid4())
    with _open(live) as connection:
        ids = _seed_identity(connection)
        keeper = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="same-mid",
            author_person_id=ids["person"],
            content="shared-body",
            canonical_event_id=shared,
        )
        duplicate = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="same-mid",
            author_person_id=ids["person"],
            content="shared-body",
        )
        connection.execute(
            "INSERT INTO canonical_event_receipts("
            "ingress_presence_id, event_type, platform_message_id, "
            "canonical_event_id, created_at, observed_at"
            ") VALUES (?, 'message', ?, ?, ?, ?)",
            (ids["presence"], "same-mid", shared, _NOW, _NOW),
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "same-presence-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        rows = list(
            connection.execute(
                "SELECT id, canonical_event_id, suppression_status FROM chat_events ORDER BY id"
            )
        )
        assert [int(row[0]) for row in rows] == [keeper, duplicate]
        assert {str(row[1]) for row in rows} == {shared}
        assert {str(row[2]) for row in rows} == {"keeper", "duplicate"}
        receipts = list(
            connection.execute(
                "SELECT ingress_presence_id, canonical_event_id "
                "FROM canonical_event_receipts WHERE platform_message_id = 'same-mid'"
            )
        )
        assert len(receipts) == 1
        assert str(receipts[0][0]) == ids["presence"]
        assert str(receipts[0][1]) == shared
        assert connection.execute("SELECT COUNT(*) FROM chat_events").fetchone()[0] == 2


def test_apply_conflicting_duplicate_ids_and_receipt_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "dup-id-conflict.db"
    _upgrade(live, monkeypatch, "head")
    first_id = str(uuid4())
    second_id = str(uuid4())
    with _open(live) as connection:
        ids = _seed_identity(connection)
        extra_presence = str(uuid4())
        _insert_presence(connection, extra_presence, "8001")
        _pin_required_routes(connection, ids)
        first = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="dup-conflict",
            author_person_id=ids["person"],
            content="shared-body",
            canonical_event_id=first_id,
        )
        second = _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="dup-conflict",
            author_person_id=ids["person"],
            content="shared-body",
            canonical_event_id=second_id,
        )
        _insert_memory_job(connection, first, conversation_key="group:8000:2001")
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "dup-id-conflict-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    blocked = _service(live, settings).apply(planned.source_fingerprint)
    assert blocked.status == "failed"
    assert blocked.error_category == "receipt_conflict"
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0
        stored = {
            int(row[0]): str(row[1])
            for row in connection.execute("SELECT id, canonical_event_id FROM chat_events")
        }
        assert stored == {first: first_id, second: second_id}
        assert connection.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM canonical_event_receipts").fetchone()[0] == 0
        )

    matching = tmp_path / "receipt-match.db"
    _upgrade(matching, monkeypatch, "head")
    kept = str(uuid4())
    with _open(matching) as connection:
        ids = _seed_identity(connection)
        _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="onebot-keep",
            author_person_id=ids["person"],
            canonical_event_id=kept,
        )
        connection.execute(
            "INSERT INTO canonical_event_receipts("
            "ingress_presence_id, event_type, platform_message_id, "
            "canonical_event_id, created_at, observed_at"
            ") VALUES (?, 'message', ?, ?, ?, ?)",
            (ids["presence"], "onebot-keep", kept, _NOW, _NOW),
        )
        connection.commit()
    matching_settings = _prepare_snapshots(matching, tmp_path / "receipt-match-snap")
    planned = _service(matching, matching_settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(matching, matching_settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(matching) as connection:
        receipts = list(
            connection.execute(
                "SELECT canonical_event_id FROM canonical_event_receipts "
                "WHERE platform_message_id = 'onebot-keep'"
            )
        )
        assert len(receipts) == 1
        assert str(receipts[0][0]) == kept
        assert (
            connection.execute(
                "SELECT canonical_event_id FROM chat_events "
                "WHERE platform_message_id = 'onebot-keep'"
            ).fetchone()[0]
            == kept
        )


def _manifest_payload(path: Path) -> dict[str, object]:
    with _open(path) as connection:
        raw = connection.execute("SELECT payload_json FROM identity_cutover_manifests").fetchone()[
            0
        ]
    parsed = json.loads(str(raw))
    assert isinstance(parsed, dict)
    return parsed


def _assert_apply_stays_v1(
    live: Path, settings: CutoverSettingsInput, fingerprint: str, category: str
) -> None:
    report = _service(live, settings).apply(fingerprint)
    assert report.status == "failed"
    assert report.error_category == category
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert (
            connection.execute("SELECT source_fingerprint FROM identity_runtime_state").fetchone()[
                0
            ]
            is None
        )
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0


def _seed_duplicate_events(connection: sqlite3.Connection) -> dict[str, str]:
    ids = _seed_identity(connection)
    _insert_event(
        connection,
        bot_user_id="8000",
        platform_message_id="same",
        author_person_id=ids["person"],
    )
    _insert_event(
        connection,
        bot_user_id="8001",
        platform_message_id="same",
        author_person_id=ids["person"],
    )
    return ids


def test_repeated_plan_decision_digest_is_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "digest-stable.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        _seed_duplicate_events(connection)
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "digest-stable-snap")
    repository = IdentityCutoverRepository(live)
    with repository.connect() as connection:
        snapshot = repository.snapshot_evidence(settings)
        first = repository.build_plan(connection, settings, snapshot)
        second = repository.build_plan(connection, settings, snapshot)
    assert first.source_fingerprint == second.source_fingerprint
    assert first.decision_digest == second.decision_digest
    assert is_sha256_hex(first.decision_digest)
    first_report = _service(live, settings).plan()
    second_report = _service(live, settings).plan()
    assert first_report.status == "succeeded", first_report.error_category
    assert second_report.status == "succeeded", second_report.error_category
    assert first_report.source_fingerprint == second_report.source_fingerprint
    payload = _manifest_payload(live)
    assert payload["decision_digest"] == first.decision_digest
    assert payload["source_fingerprint"] == first.source_fingerprint
    with _open(live) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM identity_cutover_manifests").fetchone()[0] == 1
        )


def test_persist_manifest_same_payload_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "persist-idem.db"
    _upgrade(live, monkeypatch, "head")
    settings = _prepare_snapshots(live, tmp_path / "persist-idem-snap")
    repository = IdentityCutoverRepository(live)
    with repository.connect() as connection:
        snapshot = repository.snapshot_evidence(settings)
        plan = repository.build_plan(connection, settings, snapshot)
        repository.persist_manifest(connection, plan)
        first = connection.execute(
            "SELECT payload_json, created_at FROM identity_cutover_manifests"
        ).fetchone()
        repository.persist_manifest(connection, plan)
        second = connection.execute(
            "SELECT payload_json, created_at FROM identity_cutover_manifests"
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM identity_cutover_manifests").fetchone()[0]
    assert str(first[0]) == str(second[0])
    assert str(first[1]) == str(second[1])
    assert int(count) == 1
    parsed = json.loads(str(first[0]))
    assert parsed["decision_digest"] == plan.decision_digest


def test_conflicting_same_key_payload_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "persist-conflict.db"
    _upgrade(live, monkeypatch, "head")
    settings = _prepare_snapshots(live, tmp_path / "persist-conflict-snap")
    repository = IdentityCutoverRepository(live)
    with repository.connect() as connection:
        snapshot = repository.snapshot_evidence(settings)
        plan = repository.build_plan(connection, settings, snapshot)
        repository.persist_manifest(connection, plan)
        with pytest.raises(IdentityCutoverError) as digest_exc:
            repository.persist_manifest(connection, replace(plan, decision_digest="0" * 64))
        assert digest_exc.value.category == "decision_digest"
        with pytest.raises(IdentityCutoverError) as payload_exc:
            repository.persist_manifest(connection, replace(plan, git_revision="deadbeef"))
        assert payload_exc.value.category == "decision_digest"
        assert (
            connection.execute("SELECT COUNT(*) FROM identity_cutover_manifests").fetchone()[0] == 1
        )
        stored = json.loads(
            str(
                connection.execute(
                    "SELECT payload_json FROM identity_cutover_manifests"
                ).fetchone()[0]
            )
        )
    assert stored["decision_digest"] == plan.decision_digest
    assert stored["git_revision"] == plan.git_revision


def test_tampered_decision_digest_blocks_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "tamper-digest.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        _seed_duplicate_events(connection)
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "tamper-digest-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    with _open(live) as connection:
        payload = json.loads(
            str(
                connection.execute(
                    "SELECT payload_json FROM identity_cutover_manifests"
                ).fetchone()[0]
            )
        )
        original = str(payload["decision_digest"])
        payload["decision_digest"] = "0" * 64
        connection.execute(
            "UPDATE identity_cutover_manifests SET payload_json = ?",
            (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),),
        )
        connection.commit()
    assert original != "0" * 64
    _assert_apply_stays_v1(live, settings, planned.source_fingerprint, "decision_digest")


def test_monkeypatched_watermark_decision_blocks_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "drift-watermark.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        _seed_duplicate_events(connection)
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "drift-watermark-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    stored = _manifest_payload(live)
    original = IdentityCutoverRepository.conversation_watermarks

    def flipped(
        self: IdentityCutoverRepository,
        connection: sqlite3.Connection,
        *,
        suppress_event_ids: frozenset[int] = frozenset(),
    ) -> object:
        marks = original(self, connection, suppress_event_ids=suppress_event_ids)
        first = marks[0]
        return (replace(first, last_event_id=first.last_event_id + 1), *marks[1:])

    monkeypatch.setattr(IdentityCutoverRepository, "conversation_watermarks", flipped)
    _assert_apply_stays_v1(live, settings, planned.source_fingerprint, "decision_digest")
    again = _manifest_payload(live)
    assert again["source_fingerprint"] == stored["source_fingerprint"]
    assert again["decision_digest"] == stored["decision_digest"]


def test_monkeypatched_duplicate_decision_blocks_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "drift-duplicate.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        _seed_duplicate_events(connection)
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "drift-duplicate-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    stored_fingerprint = planned.source_fingerprint
    original = IdentityCutoverRepository.classify_duplicates

    def flipped(self: IdentityCutoverRepository, connection: sqlite3.Connection) -> object:
        decisions = original(self, connection)
        first = decisions[0]
        return (replace(first, keeper_event_id=(first.keeper_event_id or 0) + 99), *decisions[1:])

    monkeypatch.setattr(IdentityCutoverRepository, "classify_duplicates", flipped)
    _assert_apply_stays_v1(live, settings, stored_fingerprint, "decision_digest")
    assert _manifest_payload(live)["source_fingerprint"] == stored_fingerprint


def test_legacy_generation_5_over_existing_gen1_aligns_and_hydrate_sees_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_ai_bot.conversation.rollup.models import RollupKind, RollupPolicyConfig
    from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.persistence.database import Database

    live = tmp_path / "align-gen5.db"
    _upgrade(live, monkeypatch, "head")
    scope_key = "bot:8000:group:2001"
    with _open(live) as connection:
        ids = _seed_identity(connection)
        event_id = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="legacy-new",
            author_person_id=ids["person"],
            content="generation-five-boundary",
        )
        scope_id = _insert_scope(
            connection,
            scope_key=scope_key,
            bot_user_id="8000",
            group_id="2001",
            last_event_id=event_id,
            starts_after_event_id=event_id,
            generation=5,
            last_generation_change_event_id=event_id,
        )
        _insert_legacy_rollup(
            connection,
            scope_id=scope_id,
            covered_through_event_id=event_id,
            summary_text="legacy-generation-five-summary",
            fingerprint="d" * 64,
            generation=5,
        )
        conversation_id = _insert_canonical_conversation(
            connection,
            kind="space",
            owner_id=ids["space"],
            primary_scope_key=scope_key,
            generation=1,
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "align-gen5-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        row = connection.execute(
            "SELECT generation, starts_after_event_id, last_event_id, "
            "last_generation_change_event_id, covered_through_event_id, "
            "uncovered_event_count, uncovered_character_count "
            "FROM canonical_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        assert row is not None
        assert int(row[0]) == 5
        assert int(row[1]) == event_id
        assert int(row[2]) == event_id
        assert int(row[3]) == event_id
        assert int(row[4]) == event_id
        assert int(row[5]) == 0
        assert int(row[6]) == 0
        rollup = connection.execute(
            "SELECT generation, covered_through_event_id, summary_text, summary_kind "
            "FROM canonical_conversation_rollups WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        assert rollup is not None
        assert int(rollup[0]) == 5
        assert int(rollup[1]) == event_id
        assert "legacy-generation-five-summary" in str(rollup[2])
        assert str(rollup[3]) == "migration"
        assert int(row[0]) == int(rollup[0])
        primary = connection.execute(
            "SELECT scope_key FROM conversation_legacy_aliases "
            "WHERE conversation_id = ? AND is_primary = 1",
            (conversation_id,),
        ).fetchone()
        assert primary is not None
        assert str(primary[0]) == scope_key
        assert not str(primary[0]).startswith("cutover:")

    async def _hydrate_migration() -> None:
        database = Database(f"sqlite+aiosqlite:///{live.as_posix()}")
        try:
            repository = ConversationRollupRepository(database, RollupPolicyConfig())
            state, rollup_state, _job = await repository.status(
                ConversationScope.group("8000", "2001")
            )
            assert state is not None
            assert state.generation == 5
            assert state.last_event_id == event_id
            assert state.starts_after_event_id == event_id
            assert rollup_state is not None
            assert rollup_state.generation == 5
            assert rollup_state.summary_kind is RollupKind.MIGRATION
            assert "legacy-generation-five-summary" in rollup_state.summary_text
        finally:
            await database.close()

    asyncio.run(_hydrate_migration())


def test_incompatible_ahead_canonical_blocks_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "ahead-block.db"
    _upgrade(live, monkeypatch, "head")
    scope_key = "bot:8000:group:2001"
    with _open(live) as connection:
        ids = _seed_identity(connection)
        event_id = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="v1-only",
            author_person_id=ids["person"],
        )
        _insert_scope(
            connection,
            scope_key=scope_key,
            bot_user_id="8000",
            group_id="2001",
            last_event_id=event_id,
        )
        conversation_id = _insert_canonical_conversation(
            connection,
            kind="space",
            owner_id=ids["space"],
            primary_scope_key=scope_key,
            generation=9,
            starts_after_event_id=99,
            last_event_id=99,
            last_generation_change_event_id=99,
            covered_through_event_id=99,
            uncovered_event_count=3,
            uncovered_character_count=12,
        )
        connection.execute(
            "INSERT INTO canonical_conversation_rollups("
            "conversation_id, generation, covered_through_event_id, summary_text, "
            "summary_kind, source_fingerprint, revision, created_at, updated_at"
            ") VALUES (?, 9, 99, 'v2-only-extractive', 'extractive', ?, 1, ?, ?)",
            (conversation_id, "e" * 64, _NOW, _NOW),
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "ahead-block-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "blocked"
    assert planned.error_category == "populated_merge_forbidden"
    applied = _service(live, settings).apply("0" * 64)
    assert applied.status == "failed"
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        row = connection.execute(
            "SELECT generation, last_event_id, covered_through_event_id, "
            "uncovered_event_count FROM canonical_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        assert row is not None
        assert int(row[0]) == 9
        assert int(row[1]) == 99
        assert int(row[2]) == 99
        assert int(row[3]) == 3
        rollup = connection.execute(
            "SELECT generation, summary_kind, summary_text FROM canonical_conversation_rollups "
            "WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        assert rollup is not None
        assert int(rollup[0]) == 9
        assert str(rollup[1]) == "extractive"
        assert str(rollup[2]) == "v2-only-extractive"
        assert connection.execute("SELECT COUNT(*) FROM person_active_routes").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM space_active_routes").fetchone()[0] == 0


def test_align_failpoint_rolls_back_existing_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "align-failpoint.db"
    _upgrade(live, monkeypatch, "head")
    scope_key = "bot:8000:group:2001"
    with _open(live) as connection:
        ids = _seed_identity(connection)
        event_id = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="fp-align",
            author_person_id=ids["person"],
        )
        _insert_scope(
            connection,
            scope_key=scope_key,
            bot_user_id="8000",
            group_id="2001",
            last_event_id=event_id,
            starts_after_event_id=event_id,
            generation=5,
            last_generation_change_event_id=event_id,
        )
        conversation_id = _insert_canonical_conversation(
            connection,
            kind="space",
            owner_id=ids["space"],
            primary_scope_key=scope_key,
            generation=1,
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "align-failpoint-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category

    def trip(name: str) -> None:
        if name == "after_conversations":
            raise RuntimeError("after_conversations")

    report = IdentityCutoverService(live, settings, failpoint=trip).apply(
        planned.source_fingerprint
    )
    assert report.status == "failed"
    assert report.error_category == "operational_error"
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        row = connection.execute(
            "SELECT generation, last_event_id, covered_through_event_id "
            "FROM canonical_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        assert row is not None
        assert int(row[0]) == 1
        assert int(row[1]) == 0
        assert int(row[2]) == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM canonical_conversation_rollups WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM person_active_routes").fetchone()[0] == 0


def test_primary_alias_is_minimum_conversation_scope_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "primary-min-scope.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        event_8000 = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="from-8000",
            author_person_id=ids["person"],
        )
        event_8001 = _insert_event(
            connection,
            bot_user_id="8001",
            platform_message_id="from-8001",
            author_person_id=ids["person"],
        )
        first_scope = _insert_scope(
            connection,
            scope_key="bot:8001:group:2001",
            bot_user_id="8001",
            group_id="2001",
            last_event_id=event_8001,
        )
        second_scope = _insert_scope(
            connection,
            scope_key="bot:8000:group:2001",
            bot_user_id="8000",
            group_id="2001",
            last_event_id=event_8000,
        )
        assert first_scope < second_scope
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "primary-min-scope-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "succeeded", planned.error_category
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        aliases = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT scope_key, is_primary FROM conversation_legacy_aliases"
            )
        }
        assert aliases["bot:8001:group:2001"] == 1
        assert "bot:8000:group:2001" in aliases
        assert sum(value for value in aliases.values()) == 1
        assert not any(key.startswith("cutover:") for key in aliases)


def test_historical_events_without_proven_alias_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "unproven-alias.db"
    _upgrade(live, monkeypatch, "head")
    with _open(live) as connection:
        ids = _seed_identity(connection)
        event_id = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="has-history",
            author_person_id=ids["person"],
        )
        _insert_scope(
            connection,
            scope_key="cutover:space:unproven",
            bot_user_id="8000",
            group_id="2001",
            last_event_id=event_id,
        )
        connection.commit()
    settings = _prepare_snapshots(live, tmp_path / "unproven-alias-snap")
    planned = _service(live, settings).plan()
    assert planned.status == "blocked"
    assert planned.error_category == "canonical_kind_mismatch"
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0
        assert not list(
            connection.execute(
                "SELECT 1 FROM conversation_legacy_aliases WHERE scope_key LIKE 'cutover:%'"
            )
        )
