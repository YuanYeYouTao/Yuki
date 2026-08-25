"""C21 slice 2B2a: cutover owner completeness, fingerprint, and inventory."""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from tests.unit.test_identity_cutover import (
    _NOW,
    _assert_apply_stale_v1,
    _assert_apply_stays_v1,
    _insert_event,
    _insert_memory_fact_person,
    _insert_people,
    _open,
    _prepare_snapshots,
    _seed_identity,
    _service,
)
from tests.unit.test_migration_0043 import _upgrade

from qq_ai_bot.identity.c21_readable_owners import (
    C21_OWNERS_INCOMPLETE,
    STATE_MISMATCH,
    require_c21_readable_owners,
)
from qq_ai_bot.identity.cutover_reporting import render_cutover_report
from qq_ai_bot.identity.cutover_source import live_source_manifest
from qq_ai_bot.identity.cutover_types import (
    CUTOVER_INVENTORY_VERSION,
    compute_decision_digest,
    is_sha256_hex,
)
from qq_ai_bot.identity.errors import IdentityCutoverPreconditionError
from qq_ai_bot.identity.inventory import (
    CUTOVER_BASELINE_PENDING,
    DEFERRED_SHADOWS,
    LEGACY_PROVENANCE_RETAINED,
)
from qq_ai_bot.identity.sanitize import looks_like_secret_or_path

_SRC = Path("src/qq_ai_bot")
_HASH = hashlib.sha256(b"private:1001").hexdigest()
_EXPIRES_FUTURE = "2026-12-31T00:00:00+00:00"
_EXPIRES_PAST = "2020-01-01T00:00:00+00:00"


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _insert_rebuild_job(
    connection,
    event_id: int,
    *,
    person_id: str | None = None,
    space_id: str | None = None,
    status: str = "done",
) -> int:
    cursor = connection.execute(
        "INSERT INTO memory_jobs("
        "event_id, conversation_key, status, attempts, next_attempt_at, "
        "created_at, updated_at, processing_source, canonical_person_id, canonical_space_id"
        ") VALUES (?, 'rebuild:audit', ?, 0, ?, ?, ?, 'rebuild', ?, ?)",
        (event_id, status, _NOW, _NOW, _NOW, person_id, space_id),
    )
    return int(cursor.lastrowid)


def _insert_receipt(
    connection,
    event_id: int,
    *,
    expires_at: str,
    person_id: str | None = None,
    space_id: str | None = None,
) -> int:
    cursor = connection.execute(
        "INSERT INTO memory_tool_receipts("
        "conversation_key_hash, trigger_event_id, bot_user_id, provider_id, tool_name, "
        "success, result_excerpt, result_characters, created_at, expires_at, "
        "canonical_person_id, canonical_space_id"
        ") VALUES (?, ?, '8000', 'test', 'web_search', 1, 'ok', 2, ?, ?, ?, ?)",
        (_hash("private:1001"), event_id, _NOW, expires_at, person_id, space_id),
    )
    return int(cursor.lastrowid)


def _insert_state(
    connection,
    *,
    event_id: int,
    pending_events: int,
    person_id: str | None = None,
    space_id: str | None = None,
    peer: str = "1001",
) -> int:
    cursor = connection.execute(
        "INSERT INTO memory_self_reflection_states("
        "conversation_key_hash, bot_user_id, scope_type, group_id, private_peer_user_id, "
        "last_event_id, latest_event_id, pending_events, pending_characters, "
        "has_yuki_reply, has_tool_result, high_value_signal, updated_at, "
        "canonical_person_id, canonical_space_id"
        ") VALUES (?, '8000', 'private', NULL, ?, ?, ?, ?, 1, 0, 0, 0, ?, ?, ?)",
        (_HASH, peer, event_id, event_id, pending_events, _NOW, person_id, space_id),
    )
    return int(cursor.lastrowid)


def _insert_run(
    connection,
    *,
    event_id: int,
    person_id: str | None = None,
    space_id: str | None = None,
    slot: str = "2026-08-24:04",
) -> int:
    cursor = connection.execute(
        "INSERT INTO memory_self_reflection_runs("
        "conversation_key_hash, bot_user_id, scheduled_slot, trigger_reason, "
        "first_event_id, last_event_id, status, proposal_count, committed_count, started_at, "
        "canonical_person_id, canonical_space_id"
        ") VALUES (?, '8000', ?, 'manual', ?, ?, 'completed', 0, 0, ?, ?, ?)",
        (_HASH, slot, event_id, event_id, _NOW, person_id, space_id),
    )
    return int(cursor.lastrowid)


def _insert_dream_cluster(
    connection,
    *,
    status: str = "pending",
    subject_person: str | None = None,
    subject_space: str | None = None,
    visibility_person: str | None = None,
    visibility_space: str | None = None,
    fact_ids: str = "[1]",
) -> int:
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
        "created_at, updated_at, canonical_subject_person_id, canonical_subject_space_id, "
        "canonical_visibility_person_id, canonical_visibility_space_id"
        ") VALUES (?, 'cluster-a', 'legacy-partition', '8000', 'fact', ?, "
        "?, ?, 0, 0, 0, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            status,
            fact_ids,
            "f" * 64,
            _NOW,
            _NOW,
            subject_person,
            subject_space,
            visibility_person,
            visibility_space,
        ),
    )
    return int(cursor.lastrowid)


def _ready(path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    _upgrade(path, monkeypatch, "head")
    with _open(path) as connection:
        ids = _seed_identity(connection)
        event_id = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="c21-base",
            author_person_id=ids["person"],
        )
        ids["event"] = str(event_id)
        connection.commit()
    return ids


def _plan(path: Path, snap: Path):
    settings = _prepare_snapshots(path, snap)
    return _service(path, settings).plan(), settings


_C21_OWNER_COMPLETE_KEYS = (
    "memory_jobs",
    "memory_tool_receipts",
    "memory_self_reflection_states/memory_self_reflection_runs",
    "memory_dream_runs/memory_dream_clusters/memory_dream_operations",
)
_LATER_CUTOFF = "2099-01-01T00:00:00+00:00"


def _inventory_keys(items: tuple[tuple[str, str], ...]) -> set[str]:
    return {key for key, _reason in items}


def _write_manifest_payload(path: Path, payload: dict[str, object]) -> None:
    with _open(path) as connection:
        connection.execute(
            "UPDATE identity_cutover_manifests SET payload_json = ?",
            (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),),
        )
        connection.commit()


def test_inventory_marks_c21_owner_projection_complete() -> None:
    assert CUTOVER_INVENTORY_VERSION == "c27-cutover-v3"
    deferred = _inventory_keys(DEFERRED_SHADOWS)
    baseline = _inventory_keys(CUTOVER_BASELINE_PENDING)
    provenance = _inventory_keys(LEGACY_PROVENANCE_RETAINED)
    deferred_text = " ".join(f"{key} {reason}" for key, reason in DEFERRED_SHADOWS)
    assert "C26 rekeys" not in deferred_text
    for key in _C21_OWNER_COMPLETE_KEYS:
        assert key not in deferred
    assert "memory_evidence" not in deferred
    assert "memory_reflection_jobs" not in deferred
    assert deferred.isdisjoint(baseline)
    assert deferred.isdisjoint(provenance)
    assert baseline == {
        "memory_jobs",
        "memory_evidence",
        "memory_reflection_jobs",
    }
    evidence_reason = next(
        reason for key, reason in CUTOVER_BASELINE_PENDING if key == "memory_evidence"
    )
    assert "C7" in evidence_reason
    assert "C26" in evidence_reason
    assert "completed runtime" in evidence_reason
    assert "memory_evidence.source_speaker_user_id" in provenance
    assert "memory_jobs.conversation_key" in provenance
    assert "memory_tool_receipts.bot_user_id/conversation_key_hash" in provenance
    assert (
        "memory_self_reflection_states/memory_self_reflection_runs."
        "conversation_key_hash/bot_user_id" in provenance
    )
    assert "memory_dream_runs/memory_dream_operations" in provenance
    assert "memory_dream_clusters" not in provenance
    assert "canonical_conversation_id" in " ".join(deferred)


def test_decision_digest_requires_and_binds_c21_readable_cutoff() -> None:
    common = {
        "conversations": (),
        "duplicates": (),
        "suppress_event_ids": (),
        "pending_preconfig": 0,
        "route_default_applicability": True,
    }
    with pytest.raises(TypeError):
        compute_decision_digest(**common)
    first = compute_decision_digest(**common, c21_readable_cutoff="2026-01-01T00:00:00+00:00")
    second = compute_decision_digest(**common, c21_readable_cutoff=_LATER_CUTOFF)
    assert first != second
    assert is_sha256_hex(first)
    assert is_sha256_hex(second)


def test_validator_blocks_rebuild_job_without_xor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "job-missing.db"
    ids = _ready(live, monkeypatch)
    with _open(live) as connection:
        _insert_rebuild_job(connection, int(ids["event"]))
        connection.commit()
        with pytest.raises(IdentityCutoverPreconditionError) as exc:
            require_c21_readable_owners(connection, _NOW)
        assert exc.value.category == C21_OWNERS_INCOMPLETE
    settings = _prepare_snapshots(live, tmp_path / "job-missing-snap")
    report = _service(live, settings).plan()
    assert report.status == "blocked"
    assert report.error_category == C21_OWNERS_INCOMPLETE
    rendered = render_cutover_report(report, "json")
    assert "secret" not in rendered.casefold()
    assert ids["person"] not in rendered


def test_validator_blocks_unexpired_receipt_and_respects_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "receipt.db"
    ids = _ready(live, monkeypatch)
    with _open(live) as connection:
        _insert_receipt(connection, int(ids["event"]), expires_at=_EXPIRES_FUTURE)
        connection.commit()
        with pytest.raises(IdentityCutoverPreconditionError) as exc:
            require_c21_readable_owners(connection, _NOW)
        assert exc.value.category == C21_OWNERS_INCOMPLETE
        require_c21_readable_owners(connection, "2027-01-01T00:00:00+00:00")
    settings = _prepare_snapshots(live, tmp_path / "receipt-snap")
    report = _service(live, settings).plan()
    assert report.status == "blocked"
    assert report.error_category == C21_OWNERS_INCOMPLETE


def test_expired_receipt_without_owner_does_not_block_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "receipt-expired.db"
    ids = _ready(live, monkeypatch)
    with _open(live) as connection:
        _insert_receipt(connection, int(ids["event"]), expires_at=_EXPIRES_PAST)
        connection.commit()
    planned, _settings = _plan(live, tmp_path / "receipt-expired-snap")
    assert planned.status == "succeeded", planned.error_category


def test_null_expires_receipt_without_owner_stays_in_scope() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE memory_tool_receipts ("
        "expires_at TEXT, canonical_person_id TEXT, canonical_space_id TEXT)"
    )
    connection.execute("INSERT INTO memory_tool_receipts VALUES (NULL, NULL, NULL)")
    try:
        with pytest.raises(IdentityCutoverPreconditionError) as exc:
            require_c21_readable_owners(connection, _LATER_CUTOFF)
        assert exc.value.category == C21_OWNERS_INCOMPLETE
    finally:
        connection.close()


def test_validator_blocks_pending_reflection_and_wrong_run_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "reflection.db"
    ids = _ready(live, monkeypatch)
    with _open(live) as connection:
        _insert_state(connection, event_id=int(ids["event"]), pending_events=1)
        connection.commit()
        with pytest.raises(IdentityCutoverPreconditionError) as exc:
            require_c21_readable_owners(connection, _NOW)
        assert exc.value.category == C21_OWNERS_INCOMPLETE
        connection.execute("DELETE FROM memory_self_reflection_states")
        _insert_state(
            connection,
            event_id=int(ids["event"]),
            pending_events=1,
            person_id=ids["person"],
        )
        _insert_run(connection, event_id=int(ids["event"]))
        connection.commit()
        with pytest.raises(IdentityCutoverPreconditionError) as exc:
            require_c21_readable_owners(connection, _NOW)
        assert exc.value.category == C21_OWNERS_INCOMPLETE
    settings = _prepare_snapshots(live, tmp_path / "reflection-snap")
    report = _service(live, settings).plan()
    assert report.status == "blocked"
    assert report.error_category == C21_OWNERS_INCOMPLETE


def test_validator_blocks_open_dream_shape_and_active_fact_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "shape.db"
    ids = _ready(live, monkeypatch)
    with _open(live) as connection:
        connection.execute("DROP TRIGGER IF EXISTS trg_memory_dream_clusters_memory_owner_insert")
        connection.execute("DROP TRIGGER IF EXISTS trg_memory_dream_clusters_memory_owner_update")
        _insert_dream_cluster(
            connection, subject_person=ids["person"], visibility_person=ids["person"]
        )
        connection.commit()
        with pytest.raises(IdentityCutoverPreconditionError) as exc:
            require_c21_readable_owners(connection, _NOW)
        assert exc.value.category == C21_OWNERS_INCOMPLETE
        connection.execute("DELETE FROM memory_dream_clusters")
        connection.execute("DELETE FROM memory_dream_runs")
        _insert_people(connection, "1001", canonical_person_id=ids["person"])
        _insert_memory_fact_person(
            connection, subject_user_id="1001", canonical_subject_person_id=None
        )
        connection.commit()
        with pytest.raises(IdentityCutoverPreconditionError) as exc:
            require_c21_readable_owners(connection, _NOW)
        assert exc.value.category == C21_OWNERS_INCOMPLETE


def test_validator_classifies_missing_person_as_state_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "fk.db"
    ids = _ready(live, monkeypatch)
    missing = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    with _open(live) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        _insert_rebuild_job(connection, int(ids["event"]), person_id=missing)
        connection.commit()
        with pytest.raises(IdentityCutoverPreconditionError) as exc:
            require_c21_readable_owners(connection, _NOW)
        assert exc.value.category == STATE_MISMATCH


def test_complete_owners_plan_and_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live = tmp_path / "happy.db"
    ids = _ready(live, monkeypatch)
    with _open(live) as connection:
        _insert_people(connection, "1001", canonical_person_id=ids["person"])
        _insert_rebuild_job(connection, int(ids["event"]), person_id=ids["person"])
        extra = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id="c21-extra",
            author_person_id=ids["person"],
        )
        _insert_receipt(connection, extra, expires_at=_EXPIRES_FUTURE, person_id=ids["person"])
        _insert_state(connection, event_id=extra, pending_events=1, person_id=ids["person"])
        _insert_run(connection, event_id=extra, person_id=ids["person"])
        _insert_dream_cluster(connection, subject_person=ids["person"])
        _insert_memory_fact_person(
            connection, subject_user_id="1001", canonical_subject_person_id=ids["person"]
        )
        jobs_before = [
            tuple(row)
            for row in connection.execute(
                "SELECT id, conversation_key, processing_source FROM memory_jobs"
            )
        ]
        connection.commit()
    planned, settings = _plan(live, tmp_path / "happy-snap")
    assert planned.status == "succeeded", planned.error_category
    with _open(live) as connection:
        payload = json.loads(
            connection.execute("SELECT payload_json FROM identity_cutover_manifests").fetchone()[0]
        )
        assert payload["c21_readable_cutoff"]
        assert payload["inventory"] == CUTOVER_INVENTORY_VERSION
        assert "memory_jobs" in payload["source"]
        assert looks_like_secret_or_path(json.dumps(payload)) is False
    applied = _service(live, settings).apply(planned.source_fingerprint)
    assert applied.status == "succeeded", applied.error_category
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v2"
        after_jobs = [
            tuple(row)
            for row in connection.execute(
                "SELECT id, conversation_key, processing_source FROM memory_jobs"
            )
        ]
        assert after_jobs == jobs_before


@pytest.mark.parametrize(
    "mutate",
    (
        "job_owner",
        "job_status",
        "receipt_expires",
        "state_pending",
        "run_owner",
        "dream_status",
        "dream_facts",
        "fact_owner",
    ),
)
def test_manifest_rejects_c21_input_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: str,
) -> None:
    live = tmp_path / f"tamper-{mutate}.db"
    ids = _ready(live, monkeypatch)
    with _open(live) as connection:
        _insert_people(connection, "1001", canonical_person_id=ids["person"])
        _insert_rebuild_job(connection, int(ids["event"]), person_id=ids["person"])
        extra = _insert_event(
            connection,
            bot_user_id="8000",
            platform_message_id=f"tamper-{mutate}",
            author_person_id=ids["person"],
        )
        _insert_receipt(connection, extra, expires_at=_EXPIRES_FUTURE, person_id=ids["person"])
        _insert_state(connection, event_id=extra, pending_events=1, person_id=ids["person"])
        _insert_run(connection, event_id=extra, person_id=ids["person"])
        _insert_dream_cluster(connection, subject_person=ids["person"], fact_ids="[1]")
        _insert_memory_fact_person(
            connection, subject_user_id="1001", canonical_subject_person_id=ids["person"]
        )
        connection.commit()
    planned, settings = _plan(live, tmp_path / f"tamper-{mutate}-snap")
    assert planned.status == "succeeded", planned.error_category
    with _open(live) as connection:
        before = live_source_manifest(connection)
        if mutate == "job_owner":
            connection.execute(
                "UPDATE memory_jobs SET canonical_person_id = NULL "
                "WHERE processing_source = 'rebuild'"
            )
        elif mutate == "job_status":
            connection.execute(
                "UPDATE memory_jobs SET status = 'failed' WHERE processing_source = 'rebuild'"
            )
        elif mutate == "receipt_expires":
            connection.execute("UPDATE memory_tool_receipts SET expires_at = ?", (_EXPIRES_PAST,))
        elif mutate == "state_pending":
            connection.execute("UPDATE memory_self_reflection_states SET pending_events = 0")
        elif mutate == "run_owner":
            connection.execute("UPDATE memory_self_reflection_runs SET canonical_person_id = NULL")
        elif mutate == "dream_status":
            connection.execute("UPDATE memory_dream_clusters SET status = 'completed'")
        elif mutate == "dream_facts":
            connection.execute("UPDATE memory_dream_clusters SET fact_ids_json = '[1,2]'")
        else:
            connection.execute("UPDATE memory_facts SET canonical_subject_person_id = NULL")
        connection.commit()
        assert live_source_manifest(connection) != before
    _assert_apply_stale_v1(live, settings, planned.source_fingerprint)


def test_apply_uses_manifest_cutoff_not_wall_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "cutoff.db"
    ids = _ready(live, monkeypatch)
    with _open(live) as connection:
        _insert_receipt(connection, int(ids["event"]), expires_at=_EXPIRES_PAST)
        connection.commit()
    planned, settings = _plan(live, tmp_path / "cutoff-snap")
    assert planned.status == "succeeded", planned.error_category
    with _open(live) as connection:
        payload = json.loads(
            connection.execute("SELECT payload_json FROM identity_cutover_manifests").fetchone()[0]
        )
        payload["c21_readable_cutoff"] = "2019-01-01T00:00:00+00:00"
        connection.execute(
            "UPDATE identity_cutover_manifests SET payload_json = ?",
            (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),),
        )
        connection.commit()
    report = _service(live, settings).apply(planned.source_fingerprint)
    assert report.status == "failed"
    assert report.error_category == C21_OWNERS_INCOMPLETE
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0


def test_later_cutoff_tamper_fails_decision_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "cutoff-later.db"
    ids = _ready(live, monkeypatch)
    with _open(live) as connection:
        _insert_receipt(
            connection,
            int(ids["event"]),
            expires_at=_EXPIRES_FUTURE,
            person_id=ids["person"],
        )
        connection.commit()
    planned, settings = _plan(live, tmp_path / "cutoff-later-snap")
    assert planned.status == "succeeded", planned.error_category
    with _open(live) as connection:
        payload = json.loads(
            connection.execute("SELECT payload_json FROM identity_cutover_manifests").fetchone()[0]
        )
        planned_cutoff = str(payload["c21_readable_cutoff"])
        assert planned_cutoff < _EXPIRES_FUTURE < _LATER_CUTOFF
        payload["c21_readable_cutoff"] = _LATER_CUTOFF
    _write_manifest_payload(live, payload)
    _assert_apply_stays_v1(live, settings, planned.source_fingerprint, "decision_digest")


def test_later_cutoff_and_owner_strip_cannot_skip_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "cutoff-later-owner.db"
    ids = _ready(live, monkeypatch)
    with _open(live) as connection:
        _insert_receipt(
            connection,
            int(ids["event"]),
            expires_at=_EXPIRES_FUTURE,
            person_id=ids["person"],
        )
        connection.commit()
    planned, settings = _plan(live, tmp_path / "cutoff-later-owner-snap")
    assert planned.status == "succeeded", planned.error_category
    with _open(live) as connection:
        payload = json.loads(
            connection.execute("SELECT payload_json FROM identity_cutover_manifests").fetchone()[0]
        )
        planned_cutoff = str(payload["c21_readable_cutoff"])
        assert planned_cutoff < _EXPIRES_FUTURE < _LATER_CUTOFF
        payload["c21_readable_cutoff"] = _LATER_CUTOFF
        connection.execute(
            "UPDATE identity_cutover_manifests SET payload_json = ?",
            (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),),
        )
        connection.execute("UPDATE memory_tool_receipts SET canonical_person_id = NULL")
        connection.commit()
    report = _service(live, settings).apply(planned.source_fingerprint)
    assert report.status == "failed"
    assert report.error_category in {
        "decision_digest",
        "source_fingerprint",
        STATE_MISMATCH,
    }
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0


def test_apply_failpoint_after_c21_owners_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "fp.db"
    _ready(live, monkeypatch)
    planned, settings = _plan(live, tmp_path / "fp-snap")
    assert planned.status == "succeeded", planned.error_category

    def trip(name: str) -> None:
        if name == "after_c21_owners":
            raise RuntimeError("after_c21_owners")

    report = _service(live, settings, failpoint=trip).apply(planned.source_fingerprint)
    assert report.status == "failed"
    assert report.error_category == "operational_error"
    with _open(live) as connection:
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone()[0] == "v1"
        assert connection.execute("SELECT COUNT(*) FROM canonical_conversations").fetchone()[0] == 0


def test_planner_module_stays_cutover_free() -> None:
    source = (_SRC / "identity" / "c21_readable_owners.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert "qq_ai_bot.cli" not in modules
    assert "qq_ai_bot.identity.cutover_repository" not in modules
    assert "sqlalchemy.orm" not in modules
    assert "INSERT INTO memory_jobs" not in source
    assert "except IntegrityError" not in source
