"""C21 slice 2B1: canonical Memory owner backfill dry-run/apply."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from tests.unit.test_identity_backfill import (
    _create_schema,
    _open,
    _service,
    _settings,
)
from tests.unit.test_migration_0043 import _upgrade

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.identity.backfill_repository import IdentityBackfillRepository
from qq_ai_bot.identity.canonical_memory_owners import (
    AMBIGUOUS_OWNER,
    CANONICAL_DUPLICATE,
    INCOMPLETE_DREAM_SHAPE,
    MISSING_OWNER,
    MIXED_DREAM_SOURCE,
    REFLECTION_OWNER_UNIQUE,
    STATE_RUN_AMBIGUOUS,
)
from qq_ai_bot.identity.inventory import SHADOW_FILL_SPECS
from qq_ai_bot.identity.reporting import render_report
from qq_ai_bot.memory.repository import MemoryJobRepository
from qq_ai_bot.memory.self_reflection.repository import conversation_key_hash
from qq_ai_bot.persistence.database import Database

_NOW = "2026-08-24T00:00:00+00:00"
_PERSON = "11111111-1111-4111-8111-111111111111"
_PERSON_B = "11111111-1111-4111-8111-111111111112"
_SPACE = "22222222-2222-4222-8222-222222222222"
_CONV_PRIVATE = "33333333-3333-4333-8333-333333333333"
_CONV_SPACE = "33333333-3333-4333-8333-333333333334"
_ALIAS_PRIVATE = "44444444-4444-4444-8444-444444444444"
_ALIAS_SPACE = "44444444-4444-4444-8444-444444444445"
_PRESENCE = "55555555-5555-4555-8555-555555555555"
_BIND_PERSON = "66666666-6666-4666-8666-666666666666"
_BIND_PERSON_B = "66666666-6666-4666-8666-666666666667"
_BIND_SPACE = "77777777-7777-4777-8777-777777777777"
_SRC = Path("src/qq_ai_bot")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _insert_person(connection, person_id: str) -> None:
    connection.execute(
        "INSERT INTO persons(id, enabled, revision, created_at, updated_at) VALUES (?, 1, 1, ?, ?)",
        (person_id, _NOW, _NOW),
    )


def _insert_space(connection, space_id: str) -> None:
    connection.execute(
        "INSERT INTO spaces("
        "id, name, enabled, autonomous_enabled, require_mention, revision, created_at, updated_at"
        ") VALUES (?, '', 1, 1, 1, 1, ?, ?)",
        (space_id, _NOW, _NOW),
    )


def _insert_binding(
    connection,
    *,
    binding_id: str,
    person_id: str,
    external_id: str,
    status: str = "active",
) -> None:
    connection.execute(
        "INSERT INTO identity_bindings("
        "id, person_id, platform, external_account_id, display_name, "
        "status, revision, created_at, updated_at"
        ") VALUES (?, ?, 'qq', ?, '', ?, 1, ?, ?)",
        (binding_id, person_id, external_id, status, _NOW, _NOW),
    )


def _insert_space_binding(connection, *, binding_id: str, space_id: str, external_id: str) -> None:
    connection.execute(
        "INSERT INTO space_bindings("
        "id, space_id, platform, external_space_id, display_name, "
        "status, revision, created_at, updated_at"
        ") VALUES (?, ?, 'qq', ?, '', 'active', 1, ?, ?)",
        (binding_id, space_id, external_id, _NOW, _NOW),
    )


def _insert_presence(connection, *, presence_id: str, external_id: str) -> None:
    connection.execute(
        "INSERT INTO presences("
        "id, platform, external_account_id, enabled, ingest_eligible, "
        "revision, created_at, updated_at"
        ") VALUES (?, 'qq', ?, 1, 1, 1, ?, ?)",
        (presence_id, external_id, _NOW, _NOW),
    )


def _insert_people(
    connection, user_id: str, *, is_bot: int = 0, person_id: str | None = None
) -> None:
    connection.execute(
        "INSERT INTO people("
        "user_id, nickname, enabled, is_bot, first_seen_at, last_seen_at, canonical_person_id"
        ") VALUES (?, '', 1, ?, ?, ?, ?)",
        (user_id, is_bot, _NOW, _NOW, person_id),
    )


def _insert_membership(connection, user_id: str, group_id: str) -> None:
    connection.execute(
        "INSERT INTO memberships(user_id, group_id, group_card, first_seen_at, last_seen_at) "
        "VALUES (?, ?, '', ?, ?)",
        (user_id, group_id, _NOW, _NOW),
    )


def _insert_group(connection, group_id: str, *, space_id: str | None = None) -> None:
    connection.execute(
        "INSERT INTO groups("
        "group_id, name, enabled, require_mention, autonomous_enabled, "
        "first_seen_at, last_seen_at, updated_at, canonical_space_id"
        ") VALUES (?, 'hall', 1, 1, 1, ?, ?, ?, ?)",
        (group_id, _NOW, _NOW, _NOW, space_id),
    )


def _insert_conversation(
    connection,
    *,
    conversation_id: str,
    alias_id: str,
    kind: str,
    owner_id: str,
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
        ") VALUES (?, ?, ?, ?, ?, 1, 1, 0, 0, 0, 0, 0, 0, 1, ?, ?)",
        (conversation_id, kind, person_id, space_id, alias_id, _NOW, _NOW),
    )
    connection.execute(
        "INSERT INTO conversation_legacy_aliases("
        "id, conversation_id, scope_key, is_primary, created_at, updated_at"
        ") VALUES (?, ?, ?, 1, ?, ?)",
        (alias_id, conversation_id, f"scope:{alias_id}", _NOW, _NOW),
    )
    connection.execute("COMMIT")


def _insert_event(
    connection,
    *,
    message_id: str,
    sender: str,
    bot: str = "8000",
    peer: str | None = None,
    group: str | None = None,
    conversation_id: str | None = None,
    content: str = "secret-body",
) -> int:
    scope = "group" if group else "private"
    cursor = connection.execute(
        "INSERT INTO chat_events("
        "bot_user_id, platform_message_id, scope_type, group_id, private_peer_user_id, "
        "sender_user_id, direction, content, visual_summary, segments_json, "
        "origin, occurred_at, observed_at, canonical_conversation_id"
        ") VALUES (?, ?, ?, ?, ?, ?, 'inbound', ?, '', '[]', 'user_message', ?, ?, ?)",
        (bot, message_id, scope, group, peer, sender, content, _NOW, _NOW, conversation_id),
    )
    return int(cursor.lastrowid)


def _insert_job(
    connection,
    *,
    event_id: int,
    conversation_key: str,
    processing_source: str = "live",
    status: str = "pending",
) -> int:
    cursor = connection.execute(
        "INSERT INTO memory_jobs("
        "event_id, conversation_key, status, attempts, next_attempt_at, "
        "created_at, updated_at, processing_source"
        ") VALUES (?, ?, ?, 0, ?, ?, ?, ?)",
        (event_id, conversation_key, status, _NOW, _NOW, _NOW, processing_source),
    )
    return int(cursor.lastrowid)


def _insert_receipt(connection, *, event_id: int, conversation_key: str, bot: str = "8000") -> int:
    cursor = connection.execute(
        "INSERT INTO memory_tool_receipts("
        "conversation_key_hash, trigger_event_id, bot_user_id, provider_id, tool_name, "
        "success, result_excerpt, result_characters, created_at, expires_at"
        ") VALUES (?, ?, ?, 'test', 'web_search', 1, 'ok', 2, ?, ?)",
        (_hash(conversation_key), event_id, bot, _NOW, _NOW),
    )
    return int(cursor.lastrowid)


def _insert_state(
    connection,
    *,
    peer: str | None,
    group: str | None,
    bot: str = "8000",
    event_id: int = 1,
) -> int:
    scope = "group" if group else "private"
    key_hash = conversation_key_hash(
        ScopeType.GROUP if group else ScopeType.PRIVATE,
        group_id=group,
        private_peer_user_id=peer,
    )
    cursor = connection.execute(
        "INSERT INTO memory_self_reflection_states("
        "conversation_key_hash, bot_user_id, scope_type, group_id, private_peer_user_id, "
        "last_event_id, latest_event_id, pending_events, pending_characters, "
        "has_yuki_reply, has_tool_result, high_value_signal, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 0, 0, 0, ?)",
        (key_hash, bot, scope, group, peer, event_id, event_id, _NOW),
    )
    return int(cursor.lastrowid)


def _insert_run(
    connection,
    *,
    peer: str | None,
    group: str | None,
    first_event_id: int,
    last_event_id: int,
    bot: str = "8000",
    slot: str = "2026-08-24:04",
) -> int:
    key_hash = conversation_key_hash(
        ScopeType.GROUP if group else ScopeType.PRIVATE,
        group_id=group,
        private_peer_user_id=peer,
    )
    cursor = connection.execute(
        "INSERT INTO memory_self_reflection_runs("
        "conversation_key_hash, bot_user_id, scheduled_slot, trigger_reason, "
        "first_event_id, last_event_id, status, proposal_count, committed_count, started_at"
        ") VALUES (?, ?, ?, 'manual', ?, ?, 'completed', 0, 0, ?)",
        (key_hash, bot, slot, first_event_id, last_event_id, _NOW),
    )
    return int(cursor.lastrowid)


def _insert_fact(
    connection,
    *,
    user_id: str,
    person_id: str | None,
    memory_key: str = "likes",
    content: str = "secret-fact",
) -> int:
    cursor = connection.execute(
        "INSERT INTO memory_facts("
        "scope_type, subject_user_id, kind, memory_key, category, content, "
        "normalized_content, importance, confidence, source_type, authority, status, "
        "conflict_state, created_at, updated_at, last_confirmed_at, validation_version, "
        "review_state, canonical_subject_person_id"
        ") VALUES ('person', ?, 'fact', ?, 'cat', ?, ?, 3, 1.0, 'explicit', "
        "'self_report', 'active', 'clear', ?, ?, ?, 'memory-v2-quality-v1', 'verified', ?)",
        (user_id, memory_key, content, content, _NOW, _NOW, _NOW, person_id),
    )
    return int(cursor.lastrowid)


def _insert_dream_cluster(connection, fact_ids: tuple[int, ...]) -> int:
    public_id = str(uuid4())
    connection.execute(
        "INSERT INTO memory_dream_runs("
        "public_id, mode, status, snapshot_max_fact_id, snapshot_created_at, "
        "statistics_json, model_calls, completed_clusters, failed_clusters, "
        "created_at, updated_at"
        ") VALUES (?, 'incremental', 'planned', 1, ?, '{}', 0, 0, 0, ?, ?)",
        (public_id, _NOW, _NOW, _NOW),
    )
    run_id = int(connection.execute("SELECT id FROM memory_dream_runs").fetchone()[0])
    cursor = connection.execute(
        "INSERT INTO memory_dream_clusters("
        "run_id, cluster_key, partition_key, bot_user_id, kind, status, "
        "fact_ids_json, fingerprint, attempts, model_calls, operation_count, "
        "created_at, updated_at"
        ") VALUES (?, 'cluster-a', 'legacy-partition', '8000', 'fact', 'pending', "
        "?, ?, 0, 0, 0, ?, ?)",
        (run_id, str(list(fact_ids)), "f" * 64, _NOW, _NOW),
    )
    return int(cursor.lastrowid)


def _seed_identity(connection, *, person_status: str = "disabled") -> None:
    _insert_person(connection, _PERSON)
    _insert_space(connection, _SPACE)
    _insert_binding(
        connection,
        binding_id=_BIND_PERSON,
        person_id=_PERSON,
        external_id="1001",
        status=person_status,
    )
    _insert_space_binding(connection, binding_id=_BIND_SPACE, space_id=_SPACE, external_id="2001")
    _insert_presence(connection, presence_id=_PRESENCE, external_id="8000")
    _insert_people(connection, "8000", is_bot=1)
    _insert_people(connection, "1001", person_id=_PERSON)
    _insert_group(connection, "2001", space_id=_SPACE)
    _insert_conversation(
        connection,
        conversation_id=_CONV_PRIVATE,
        alias_id=_ALIAS_PRIVATE,
        kind="private",
        owner_id=_PERSON,
    )
    _insert_conversation(
        connection,
        conversation_id=_CONV_SPACE,
        alias_id=_ALIAS_SPACE,
        kind="space",
        owner_id=_SPACE,
    )


def _seed_happy_path(connection) -> dict[str, int]:
    _seed_identity(connection)
    private_event = _insert_event(
        connection,
        message_id="p-1",
        sender="1001",
        peer="1001",
        conversation_id=_CONV_PRIVATE,
    )
    group_event = _insert_event(
        connection,
        message_id="g-1",
        sender="1001",
        group="2001",
        conversation_id=_CONV_SPACE,
    )
    live_job = _insert_job(connection, event_id=private_event, conversation_key="private:1001")
    rebuild_job = _insert_job(
        connection,
        event_id=group_event,
        conversation_key="rebuild:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        processing_source="rebuild",
        status="done",
    )
    receipt = _insert_receipt(connection, event_id=private_event, conversation_key="private:1001")
    state = _insert_state(connection, peer="1001", group=None, event_id=private_event)
    run = _insert_run(
        connection,
        peer="1001",
        group=None,
        first_event_id=private_event,
        last_event_id=private_event,
    )
    fact = _insert_fact(connection, user_id="1001", person_id=_PERSON)
    cluster = _insert_dream_cluster(connection, (fact,))
    return {
        "private_event": private_event,
        "group_event": group_event,
        "live_job": live_job,
        "rebuild_job": rebuild_job,
        "receipt": receipt,
        "state": state,
        "run": run,
        "fact": fact,
        "cluster": cluster,
    }


def test_c21_owners_are_not_in_shadow_fill_specs() -> None:
    dotted = {spec.dotted for spec in SHADOW_FILL_SPECS}
    assert "memory_jobs.canonical_person_id" not in dotted
    assert "memory_tool_receipts.canonical_person_id" not in dotted
    assert "memory_self_reflection_states.canonical_person_id" not in dotted
    assert "memory_self_reflection_runs.canonical_person_id" not in dotted
    assert "memory_dream_clusters.canonical_subject_person_id" not in dotted


def test_dry_run_reports_c21_counts_and_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "c21-dry.db"
    _create_schema(path)
    with _open(path) as connection:
        ids = _seed_happy_path(connection)
        connection.commit()
        repo = IdentityBackfillRepository(path)
        before_schema = repo.schema_signature(connection)
        before_business = repo.business_signature(connection)
        before_runs = repo.count_rows(connection, "identity_backfill_runs")
        before_conflicts = repo.count_rows(connection, "identity_conflicts")
    report = _service(path, _settings()).dry_run()
    assert report.status == "succeeded"
    assert report.run_recorded is False
    assert report.business_diff == 0
    assert report.counts.memory_job_owners == 2
    assert report.counts.memory_receipt_owners == 1
    assert report.counts.memory_reflection_state_owners == 1
    assert report.counts.memory_reflection_run_owners == 1
    assert report.counts.memory_dream_cluster_owners == 1
    rendered = render_report(report, "json")
    assert "secret-body" not in rendered
    assert "secret-fact" not in rendered
    assert "1001" not in rendered
    with _open(path) as connection:
        repo = IdentityBackfillRepository(path)
        assert repo.schema_signature(connection) == before_schema
        assert repo.business_signature(connection) == before_business
        assert repo.count_rows(connection, "identity_backfill_runs") == before_runs
        assert repo.count_rows(connection, "identity_conflicts") == before_conflicts
        job = connection.execute(
            "SELECT canonical_person_id, conversation_key FROM memory_jobs WHERE id = ?",
            (ids["live_job"],),
        ).fetchone()
        assert job[0] is None
        assert job[1] == "private:1001"


def test_apply_fills_five_owner_kinds_keeps_legacy_keys_and_second_apply_is_zero(
    tmp_path: Path,
) -> None:
    path = tmp_path / "c21-apply.db"
    _create_schema(path)
    with _open(path) as connection:
        ids = _seed_happy_path(connection)
        connection.commit()
        before = IdentityBackfillRepository(path).business_signature(connection)
        first_fp = _service(path, _settings()).dry_run().source_fingerprint
    first = _service(path, _settings()).apply()
    assert first.status == "succeeded"
    assert first.business_diff == 1
    with _open(path) as connection:
        after = IdentityBackfillRepository(path).business_signature(connection)
        assert after != before
        jobs = {
            int(row[0]): tuple(row[1:])
            for row in connection.execute(
                "SELECT id, conversation_key, canonical_person_id, canonical_space_id, "
                "processing_source FROM memory_jobs"
            )
        }
        assert jobs[ids["live_job"]] == ("private:1001", _PERSON, None, "live")
        assert jobs[ids["rebuild_job"]] == (
            "rebuild:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            None,
            _SPACE,
            "rebuild",
        )
        receipt = connection.execute(
            "SELECT conversation_key_hash, bot_user_id, canonical_person_id, canonical_space_id "
            "FROM memory_tool_receipts"
        ).fetchone()
        assert tuple(receipt) == (_hash("private:1001"), "8000", _PERSON, None)
        state = connection.execute(
            "SELECT conversation_key_hash, bot_user_id, canonical_person_id, canonical_space_id "
            "FROM memory_self_reflection_states"
        ).fetchone()
        assert tuple(state) == (
            conversation_key_hash(ScopeType.PRIVATE, group_id=None, private_peer_user_id="1001"),
            "8000",
            _PERSON,
            None,
        )
        run = connection.execute(
            "SELECT conversation_key_hash, bot_user_id, canonical_person_id "
            "FROM memory_self_reflection_runs"
        ).fetchone()
        assert tuple(run) == (state[0], "8000", _PERSON)
        cluster = connection.execute(
            "SELECT partition_key, bot_user_id, canonical_subject_person_id, "
            "canonical_subject_space_id FROM memory_dream_clusters"
        ).fetchone()
        assert tuple(cluster) == ("legacy-partition", "8000", _PERSON, None)
        fact_person = connection.execute(
            "SELECT canonical_subject_person_id FROM memory_facts WHERE id = ?",
            (ids["fact"],),
        ).fetchone()[0]
        assert fact_person == _PERSON
    second = _service(path, _settings()).apply()
    assert second.status == "succeeded"
    assert second.business_diff == 0
    assert second.source_fingerprint != first_fp
    with _open(path) as connection:
        assert IdentityBackfillRepository(path).business_signature(connection) == after


def _conflict_fixture(path: Path, mutate) -> None:
    _create_schema(path)
    with _open(path) as connection:
        _seed_happy_path(connection)
        mutate(connection)
        connection.commit()


def _assert_conflicted_zero_business(
    path: Path, *, category: str, before_business: str | None = None
) -> None:
    with _open(path) as connection:
        repo = IdentityBackfillRepository(path)
        before = before_business or repo.business_signature(connection)
        before_jobs = list(
            connection.execute(
                "SELECT id, canonical_person_id, canonical_space_id FROM memory_jobs ORDER BY id"
            )
        )
        before_states = list(
            connection.execute(
                "SELECT id, canonical_person_id FROM memory_self_reflection_states ORDER BY id"
            )
        )
    report = _service(path, _settings()).apply()
    assert report.status == "conflicted"
    assert report.business_diff == 0
    assert report.run_recorded is True
    assert {item.error_category for item in report.conflicts} >= {category}
    rendered = render_report(report, "json")
    assert "secret-body" not in rendered
    assert "secret-fact" not in rendered
    with _open(path) as connection:
        repo = IdentityBackfillRepository(path)
        assert repo.business_signature(connection) == before
        assert (
            list(
                connection.execute(
                    "SELECT id, canonical_person_id, canonical_space_id "
                    "FROM memory_jobs ORDER BY id"
                )
            )
            == before_jobs
        )
        assert (
            list(
                connection.execute(
                    "SELECT id, canonical_person_id FROM memory_self_reflection_states ORDER BY id"
                )
            )
            == before_states
        )
        conflict_blob = " ".join(
            str(item)
            for item in connection.execute(
                "SELECT platform, external_id, subject_kind, conflict_kind, status, error_category "
                "FROM identity_conflicts"
            )
        )
        assert "secret-body" not in conflict_blob
        assert "secret-fact" not in conflict_blob
        assert all(
            str(row[0]).startswith("c21:")
            for row in connection.execute("SELECT external_id FROM identity_conflicts")
        )
        run = connection.execute(
            "SELECT status, error_category FROM identity_backfill_runs"
        ).fetchone()
        assert tuple(run) == ("failed", "identity_conflict")


def test_missing_owner_conflicts_without_partial_update(tmp_path: Path) -> None:
    path = tmp_path / "c21-missing.db"

    def mutate(connection) -> None:
        _insert_people(connection, "7777", is_bot=1)
        event = _insert_event(connection, message_id="missing", sender="7777", peer="7777")
        _insert_job(connection, event_id=event, conversation_key="private:7777")

    _conflict_fixture(path, mutate)
    _assert_conflicted_zero_business(path, category=MISSING_OWNER)


def test_conversation_owner_mismatch_with_binding_is_ambiguous(tmp_path: Path) -> None:
    path = tmp_path / "c21-conv-mismatch.db"

    def mutate(connection) -> None:
        _insert_person(connection, _PERSON_B)
        connection.execute(
            "UPDATE canonical_conversations SET person_id = ? WHERE id = ?",
            (_PERSON_B, _CONV_PRIVATE),
        )

    _conflict_fixture(path, mutate)
    _assert_conflicted_zero_business(path, category=AMBIGUOUS_OWNER)


def test_ambiguous_event_scope_conflicts_without_partial_update(tmp_path: Path) -> None:
    path = tmp_path / "c21-ambiguous.db"

    def mutate(connection) -> None:
        connection.execute(
            "UPDATE chat_events SET canonical_conversation_id = ? "
            "WHERE platform_message_id = 'p-1'",
            (_CONV_SPACE,),
        )

    _conflict_fixture(path, mutate)
    _assert_conflicted_zero_business(path, category=AMBIGUOUS_OWNER)


def test_reflection_many_to_one_conflicts_without_picking(tmp_path: Path) -> None:
    path = tmp_path / "c21-many.db"

    def mutate(connection) -> None:
        _insert_people(connection, "1002", person_id=_PERSON)
        _insert_binding(
            connection,
            binding_id=_BIND_PERSON_B,
            person_id=_PERSON,
            external_id="1002",
        )
        extra = _insert_event(
            connection,
            message_id="p-2",
            sender="1002",
            peer="1002",
            conversation_id=_CONV_PRIVATE,
        )
        _insert_state(connection, peer="1002", group=None, event_id=extra)

    _conflict_fixture(path, mutate)
    _assert_conflicted_zero_business(path, category=REFLECTION_OWNER_UNIQUE)


def test_run_ambiguous_conflicts_when_events_disagree(tmp_path: Path) -> None:
    path = tmp_path / "c21-run.db"

    def mutate(connection) -> None:
        connection.execute("DELETE FROM memory_self_reflection_states")
        group_event = connection.execute(
            "SELECT id FROM chat_events WHERE platform_message_id = 'g-1'"
        ).fetchone()[0]
        private_event = connection.execute(
            "SELECT id FROM chat_events WHERE platform_message_id = 'p-1'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE memory_self_reflection_runs SET first_event_id = ?, last_event_id = ?",
            (private_event, group_event),
        )

    _conflict_fixture(path, mutate)
    _assert_conflicted_zero_business(path, category=STATE_RUN_AMBIGUOUS)


def test_dream_mixed_and_incomplete_conflict(tmp_path: Path) -> None:
    path = tmp_path / "c21-dream.db"

    def mutate(connection) -> None:
        _insert_person(connection, _PERSON_B)
        _insert_people(connection, "1002", person_id=_PERSON_B)
        _insert_binding(
            connection,
            binding_id=_BIND_PERSON_B,
            person_id=_PERSON_B,
            external_id="1002",
        )
        other = _insert_fact(connection, user_id="1002", person_id=_PERSON_B, memory_key="other")
        first = connection.execute("SELECT id FROM memory_facts ORDER BY id").fetchone()[0]
        connection.execute(
            "UPDATE memory_dream_clusters SET fact_ids_json = ?",
            (str([first, other]),),
        )

    _conflict_fixture(path, mutate)
    _assert_conflicted_zero_business(path, category=MIXED_DREAM_SOURCE)


def test_dream_incomplete_shape_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "c21-dream-incomplete.db"

    def mutate(connection) -> None:
        connection.execute("UPDATE memory_dream_clusters SET fact_ids_json = '[9999]'")

    _conflict_fixture(path, mutate)
    _assert_conflicted_zero_business(path, category=INCOMPLETE_DREAM_SHAPE)


def test_fact_duplicate_conflicts_without_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "c21-dup.db"

    def mutate(connection) -> None:
        _insert_people(connection, "1002", person_id=_PERSON)
        _insert_binding(
            connection,
            binding_id=_BIND_PERSON_B,
            person_id=_PERSON,
            external_id="1002",
        )
        connection.execute("DROP INDEX IF EXISTS uq_memory_facts_active_canonical_person_key")
        _insert_fact(connection, user_id="1002", person_id=_PERSON, memory_key="likes")

    _conflict_fixture(path, mutate)
    with _open(path) as connection:
        before_facts = list(
            connection.execute(
                "SELECT id, canonical_subject_person_id, subject_user_id FROM memory_facts "
                "ORDER BY id"
            )
        )
    _assert_conflicted_zero_business(path, category=CANONICAL_DUPLICATE)
    with _open(path) as connection:
        assert (
            list(
                connection.execute(
                    "SELECT id, canonical_subject_person_id, subject_user_id FROM memory_facts "
                    "ORDER BY id"
                )
            )
            == before_facts
        )


def test_c21_failpoint_rolls_back_owner_writes(tmp_path: Path) -> None:
    path = tmp_path / "c21-fail.db"
    _create_schema(path)
    with _open(path) as connection:
        ids = _seed_happy_path(connection)
        connection.commit()

    def boom(name: str) -> None:
        if name == "after_c21_owner_writes":
            raise RuntimeError("failpoint")

    with pytest.raises(RuntimeError, match="failpoint"):
        _service(path, _settings(), failpoint=boom).apply()
    with _open(path) as connection:
        owners = [
            tuple(row)
            for row in connection.execute(
                "SELECT canonical_person_id, canonical_space_id FROM memory_jobs ORDER BY id"
            )
        ]
        assert owners == [(None, None), (None, None)]
        assert (
            connection.execute(
                "SELECT canonical_person_id FROM memory_self_reflection_states"
            ).fetchone()[0]
            is None
        )
        assert (
            connection.execute(
                "SELECT canonical_subject_person_id FROM memory_dream_clusters WHERE id = ?",
                (ids["cluster"],),
            ).fetchone()[0]
            is None
        )
        assert tuple(
            connection.execute(
                "SELECT status, error_category FROM identity_backfill_runs"
            ).fetchone()
        ) == ("failed", "apply_aborted")


@pytest.mark.asyncio
async def test_v1_claim_keeps_legacy_key_after_owner_apply(tmp_path: Path) -> None:
    path = tmp_path / "c21-worker.db"
    _create_schema(path)
    with _open(path) as connection:
        _seed_happy_path(connection)
        connection.commit()
    assert _service(path, _settings()).apply().status == "succeeded"
    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        claimed = await MemoryJobRepository(database).claim(limit=8)
        assert [job.conversation_key for job in claimed] == ["private:1001"]
    finally:
        await database.engine.dispose()
    with _open(path) as connection:
        assert (
            connection.execute(
                "SELECT canonical_person_id FROM memory_jobs "
                "WHERE conversation_key = 'private:1001'"
            ).fetchone()[0]
            == _PERSON
        )


def test_planner_does_not_swallow_integrity_error() -> None:
    source = (_SRC / "identity" / "canonical_memory_owners.py").read_text(encoding="utf-8")
    assert "from sqlalchemy.exc import IntegrityError" not in source
    assert "except IntegrityError" not in source
    tree = ast.parse(source)
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert "_ensure_person" not in names
    assert "_ensure_group" not in names
    assert "MembershipModel" not in names
    assert "qq_ai_bot.identity.classifier" not in modules


def test_yuki_presence_cannot_be_person_owner(tmp_path: Path) -> None:
    path = tmp_path / "c21-yuki-owner.db"

    def mutate(connection) -> None:
        event = _insert_event(connection, message_id="yuki-private", sender="8000", peer="8000")
        _insert_job(connection, event_id=event, conversation_key="private:8000")

    _conflict_fixture(path, mutate)
    _assert_conflicted_zero_business(path, category=MISSING_OWNER)


def test_c21_fingerprint_tracks_owner_plan_inputs_not_bodies(tmp_path: Path) -> None:
    path = tmp_path / "c21-fp.db"
    _create_schema(path)
    with _open(path) as connection:
        ids = _seed_happy_path(connection)
        connection.commit()
    settings = _settings()
    baseline = _service(path, settings).dry_run().source_fingerprint

    with _open(path) as connection:
        connection.execute(
            "UPDATE chat_events SET content = 'other-secret' WHERE id = ?",
            (ids["private_event"],),
        )
        connection.execute(
            "UPDATE memory_facts SET content = 'other-fact', normalized_content = 'other-fact' "
            "WHERE id = ?",
            (ids["fact"],),
        )
        connection.commit()
    assert _service(path, settings).dry_run().source_fingerprint == baseline

    mutations = (
        (
            "UPDATE memory_jobs SET processing_source = 'rebuild' WHERE id = ?",
            (ids["live_job"],),
        ),
        (
            "UPDATE memory_tool_receipts SET trigger_event_id = ? WHERE id = ?",
            (ids["group_event"], ids["receipt"]),
        ),
        (
            "UPDATE memory_self_reflection_states SET bot_user_id = '8001' WHERE id = ?",
            (ids["state"],),
        ),
        (
            "UPDATE memory_self_reflection_runs SET scheduled_slot = '2026-08-24:05' WHERE id = ?",
            (ids["run"],),
        ),
        (
            "UPDATE memory_facts SET status = 'superseded' WHERE id = ?",
            (ids["fact"],),
        ),
        (
            "UPDATE memory_dream_clusters SET fact_ids_json = ? WHERE id = ?",
            (str([ids["fact"], ids["fact"]]), ids["cluster"]),
        ),
    )
    seen = {baseline}
    for sql, params in mutations:
        with _open(path) as connection:
            connection.execute(sql, params)
            connection.commit()
        changed = _service(path, settings).dry_run().source_fingerprint
        assert changed not in seen
        seen.add(changed)
    with _open(path) as connection:
        _insert_person(connection, _PERSON_B)
        connection.execute(
            "UPDATE canonical_conversations SET person_id = ? WHERE id = ?",
            (_PERSON_B, _CONV_PRIVATE),
        )
        connection.commit()
    changed = _service(path, settings).dry_run().source_fingerprint
    assert changed not in seen
    seen.add(changed)
    with _open(path) as connection:
        _insert_people(connection, "1003")
        connection.commit()
    assert _service(path, settings).dry_run().source_fingerprint not in seen


def _seed_0047_legacy_memory(connection) -> dict[str, int]:
    _insert_people(connection, "8000", is_bot=1)
    _insert_people(connection, "1001")
    _insert_group(connection, "2001")
    _insert_membership(connection, "1001", "2001")
    private_event = _insert_event(connection, message_id="legacy-p", sender="1001", peer="1001")
    group_event = _insert_event(connection, message_id="legacy-g", sender="1001", group="2001")
    live_job = _insert_job(connection, event_id=private_event, conversation_key="private:1001")
    group_job = _insert_job(connection, event_id=group_event, conversation_key="group:2001")
    receipt = _insert_receipt(connection, event_id=private_event, conversation_key="private:1001")
    state = _insert_state(connection, peer="1001", group=None, event_id=private_event)
    group_state = _insert_state(connection, peer=None, group="2001", event_id=group_event)
    run = _insert_run(
        connection,
        peer="1001",
        group=None,
        first_event_id=private_event,
        last_event_id=private_event,
    )
    return {
        "private_event": private_event,
        "group_event": group_event,
        "live_job": live_job,
        "group_job": group_job,
        "receipt": receipt,
        "state": state,
        "group_state": group_state,
        "run": run,
    }


def _canonical_empty(connection) -> None:
    assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM spaces").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM identity_bindings").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM space_bindings").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM chat_events WHERE canonical_conversation_id IS NOT NULL"
        ).fetchone()[0]
        == 0
    )


def test_0047_legacy_identity_apply_fills_c21_without_conversations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "c21-0047-legacy.db"
    _upgrade(path, monkeypatch, "0047")
    with _open(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0047"
        ids = _seed_0047_legacy_memory(connection)
        connection.commit()
        repo = IdentityBackfillRepository(path)
        _canonical_empty(connection)
        before_schema = repo.schema_signature(connection)
        before_business = repo.business_signature(connection)
        before_runs = repo.count_rows(connection, "identity_backfill_runs")
        job_keys = [
            tuple(row)
            for row in connection.execute(
                "SELECT id, conversation_key FROM memory_jobs ORDER BY id"
            )
        ]
    settings = _settings()
    dry = _service(path, settings).dry_run()
    assert dry.status == "succeeded"
    assert dry.business_diff == 0
    assert dry.run_recorded is False
    assert dry.counts.memory_job_owners == 2
    assert dry.counts.memory_receipt_owners == 1
    assert dry.counts.memory_reflection_state_owners == 2
    assert dry.counts.memory_reflection_run_owners == 1
    rendered = render_report(dry, "json")
    assert "secret-body" not in rendered
    assert "1001" not in rendered
    with _open(path) as connection:
        repo = IdentityBackfillRepository(path)
        assert repo.schema_signature(connection) == before_schema
        assert repo.business_signature(connection) == before_business
        assert repo.count_rows(connection, "identity_backfill_runs") == before_runs
        _canonical_empty(connection)
        assert (
            connection.execute(
                "SELECT canonical_person_id FROM memory_jobs WHERE id = ?",
                (ids["live_job"],),
            ).fetchone()[0]
            is None
        )
    first = _service(path, settings).apply()
    assert first.status == "succeeded"
    assert first.business_diff == 1
    with _open(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM spaces").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM identity_bindings").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM space_bindings").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chat_events WHERE canonical_conversation_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        person_id = connection.execute(
            "SELECT person_id FROM identity_bindings WHERE external_account_id = '1001'"
        ).fetchone()[0]
        space_id = connection.execute(
            "SELECT space_id FROM space_bindings WHERE external_space_id = '2001'"
        ).fetchone()[0]
        assert tuple(
            connection.execute(
                "SELECT conversation_key, canonical_person_id, canonical_space_id "
                "FROM memory_jobs WHERE id = ?",
                (ids["live_job"],),
            ).fetchone()
        ) == ("private:1001", person_id, None)
        assert tuple(
            connection.execute(
                "SELECT conversation_key, canonical_person_id, canonical_space_id "
                "FROM memory_jobs WHERE id = ?",
                (ids["group_job"],),
            ).fetchone()
        ) == ("group:2001", None, space_id)
        assert tuple(
            connection.execute(
                "SELECT canonical_person_id, canonical_space_id FROM memory_tool_receipts "
                "WHERE id = ?",
                (ids["receipt"],),
            ).fetchone()
        ) == (person_id, None)
        assert tuple(
            connection.execute(
                "SELECT canonical_person_id, canonical_space_id "
                "FROM memory_self_reflection_states WHERE id = ?",
                (ids["state"],),
            ).fetchone()
        ) == (person_id, None)
        assert tuple(
            connection.execute(
                "SELECT canonical_person_id, canonical_space_id "
                "FROM memory_self_reflection_states WHERE id = ?",
                (ids["group_state"],),
            ).fetchone()
        ) == (None, space_id)
        assert tuple(
            connection.execute(
                "SELECT canonical_person_id, canonical_space_id "
                "FROM memory_self_reflection_runs WHERE id = ?",
                (ids["run"],),
            ).fetchone()
        ) == (person_id, None)
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT id, conversation_key FROM memory_jobs ORDER BY id"
            )
        ] == job_keys
        after = IdentityBackfillRepository(path).business_signature(connection)
    second = _service(path, settings).apply()
    assert second.status == "succeeded"
    assert second.business_diff == 0
    with _open(path) as connection:
        assert IdentityBackfillRepository(path).business_signature(connection) == after
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0
