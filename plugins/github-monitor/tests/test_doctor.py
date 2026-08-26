from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from github_monitor.doctor import inspect_database
from github_monitor.models import (
    ActivationState,
    DeliveryUnit,
    PreparedMedia,
    QueuedSourceEvent,
    QueueState,
    RepositoryState,
    TargetDelivery,
    build_prepared_notification,
    delivery_target_key,
)
from github_monitor.state import DIAGNOSTIC_NAMESPACE, LEGACY_NAMESPACE, QUEUE_NAMESPACE

NOW = datetime(2026, 8, 27, tzinfo=UTC)


def _database(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "doctor.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE plugin_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_id TEXT NOT NULL,
            namespace TEXT NOT NULL,
            key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            version INTEGER NOT NULL,
            expires_at TEXT,
            updated_at TEXT NOT NULL,
            canonical_person_id TEXT,
            UNIQUE(plugin_id, namespace, key)
        );
        CREATE TABLE plugin_notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_id TEXT NOT NULL,
            status TEXT NOT NULL,
            last_error_category TEXT
        );
        CREATE TABLE plugin_background_turn_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_id TEXT NOT NULL,
            status TEXT NOT NULL,
            last_error_category TEXT
        );
        """
    )
    connection.close()
    return path, f"sqlite+aiosqlite:///{path.as_posix()}"


def _store(path: Path, namespace: str, key: str, value: object) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        INSERT INTO plugin_state (
            plugin_id, namespace, key, value_json, version, updated_at
        ) VALUES ('github-monitor', ?, ?, ?, 1, ?)
        """,
        (namespace, key, json.dumps(value), NOW.isoformat()),
    )
    connection.commit()
    connection.close()


def test_doctor_imports_legacy_queue_atomically_and_idempotently(tmp_path: Path) -> None:
    path, url = _database(tmp_path)
    legacy = RepositoryState(
        last_event_id="100",
        last_event_created_at=NOW,
        baseline_notified=True,
    )
    _store(path, LEGACY_NAMESPACE, "owner/repo", legacy.model_dump(mode="json"))

    dry_run = inspect_database(url)
    assert dry_run.ok is False
    assert dry_run.legacy_import_pending_count == 1
    assert dry_run.imported_queue_count == 0

    applied = inspect_database(url, apply_import=True)
    assert applied.ok is True
    assert applied.imported_queue_count == 1
    assert applied.legacy_import_pending_count == 0

    repeated = inspect_database(url, apply_import=True)
    assert repeated.ok is True
    assert repeated.imported_queue_count == 0
    connection = sqlite3.connect(path)
    payload = connection.execute(
        "SELECT value_json FROM plugin_state WHERE namespace = ?",
        (QUEUE_NAMESPACE,),
    ).fetchone()
    legacy_count = connection.execute(
        "SELECT COUNT(*) FROM plugin_state WHERE namespace = ?",
        (LEGACY_NAMESPACE,),
    ).fetchone()
    connection.close()
    assert payload is not None
    state = QueueState.model_validate_json(str(payload[0]))
    assert state.accepted_cursor == state.committed_cursor == "100"
    assert state.legacy_boundary == "100"
    assert legacy_count == (1,)


def test_doctor_check_does_not_create_a_missing_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"

    with pytest.raises(ValueError, match="existing canonical database"):
        inspect_database(f"sqlite+aiosqlite:///{path.as_posix()}")

    assert not path.exists()


def test_doctor_fails_closed_on_queue_and_host_work_without_exposing_content(
    tmp_path: Path,
) -> None:
    path, url = _database(tmp_path)
    state = QueueState(
        gap_reason="github_queue_state_conflict",
        gap_at=NOW,
        backlog_pending=True,
    )
    _store(path, QUEUE_NAMESPACE, "secret-owner/secret-repo", state.model_dump(mode="json"))
    _store(
        path,
        DIAGNOSTIC_NAMESPACE,
        "secret-owner/secret-repo",
        {"version": 1, "category": "receipt_conflict", "recorded_at": NOW.isoformat()},
    )
    _store(
        path,
        DIAGNOSTIC_NAMESPACE,
        "another-secret/repo",
        {
            "version": 1,
            "category": "github_queue_state_missing_after_cas",
            "recorded_at": NOW.isoformat(),
        },
    )
    connection = sqlite3.connect(path)
    connection.executemany(
        """
        INSERT INTO plugin_notification_outbox (plugin_id, status, last_error_category)
        VALUES ('github-monitor', ?, ?)
        """,
        (("failed", "receipt_conflict"), ("pending", None)),
    )
    connection.executemany(
        """
        INSERT INTO plugin_background_turn_jobs (plugin_id, status, last_error_category)
        VALUES ('github-monitor', ?, ?)
        """,
        (("completed", "superseded_covered"), ("processing", None)),
    )
    connection.commit()
    connection.close()

    report = inspect_database(url)
    rendered = json.dumps(report.__dict__ if hasattr(report, "__dict__") else str(report))
    assert report.ok is False
    assert report.cursor_gap_count == 1
    assert report.cas_conflict_count == 2
    assert report.active_diagnostic_count == 2
    assert report.diagnostic_receipt_conflict_count == 1
    assert report.backlog_repository_count == 1
    assert report.receipt_conflict_count == 1
    assert report.superseded_covered_count == 1
    assert report.outbox_pending_count == 1
    assert report.turn_pending_count == 1
    assert "secret-owner" not in rendered


def test_doctor_detects_expired_prepared_media(tmp_path: Path) -> None:
    path, url = _database(tmp_path)
    source = QueuedSourceEvent(
        github_event_id="101",
        source_fingerprint="a" * 64,
        skip_reason="filtered",
    )
    prepared = build_prepared_notification(
        event_key="github:owner/repo:event:101",
        event_type="ReleaseEvent",
        target_type="group",
        target_id="2001",
        occurred_at=NOW,
        summary="bounded",
        payload={"source_event_ids": ["101"]},
        media=(
            PreparedMedia(
                index=0,
                handle_id="expired",
                sha256="b" * 64,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
        ),
    )
    delivery = TargetDelivery(
        target_key=delivery_target_key("group", "2001"),
        target_type="group",
        target_id="2001",
        send_text=True,
        send_card=True,
        ask_agent=False,
        status="attempting",
        prepared=prepared,
    )
    state = QueueState(
        accepted_cursor="101",
        accepted_fingerprint="a" * 64,
        inflight=DeliveryUnit(
            unit_id="unit-101",
            members=(source,),
            deliveries=(delivery,),
            sealed_at=NOW,
        ),
    )
    _store(path, QUEUE_NAMESPACE, "owner/repo", state.model_dump(mode="json"))

    report = inspect_database(url)
    assert report.ok is False
    assert report.inflight_unit_count == 1
    assert report.expired_media_delivery_count == 1


def test_doctor_rejects_malformed_diagnostic_json(tmp_path: Path) -> None:
    path, url = _database(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        INSERT INTO plugin_state (
            plugin_id, namespace, key, value_json, version, updated_at
        ) VALUES ('github-monitor', ?, 'owner/repo', '{broken', 1, ?)
        """,
        (DIAGNOSTIC_NAMESPACE, NOW.isoformat()),
    )
    connection.commit()
    connection.close()

    report = inspect_database(url)

    assert report.ok is False
    assert report.invalid_state_count == 1
    assert report.active_diagnostic_count == 1


def test_doctor_fails_closed_on_incomplete_activation(tmp_path: Path) -> None:
    path, url = _database(tmp_path)
    state = QueueState(
        activation=ActivationState(
            activation_id="activation-1",
            occurred_at=NOW,
            deliveries=(
                TargetDelivery(
                    target_key=delivery_target_key("group", "2001"),
                    target_type="group",
                    target_id="2001",
                    send_text=True,
                    send_card=False,
                    ask_agent=False,
                ),
            ),
        )
    )
    _store(path, QUEUE_NAMESPACE, "owner/repo", state.model_dump(mode="json"))

    report = inspect_database(url)

    assert report.ok is False
    assert report.activation_pending_delivery_count == 1


def test_doctor_rejects_shadowed_legacy_cursor_and_rolls_back_other_imports(
    tmp_path: Path,
) -> None:
    path, url = _database(tmp_path)
    _store(
        path,
        LEGACY_NAMESPACE,
        "owner/conflict",
        RepositoryState(last_event_id="100").model_dump(mode="json"),
    )
    _store(
        path,
        QUEUE_NAMESPACE,
        "owner/conflict",
        QueueState().model_dump(mode="json"),
    )
    _store(
        path,
        LEGACY_NAMESPACE,
        "owner/importable",
        RepositoryState(last_event_id="200").model_dump(mode="json"),
    )

    report = inspect_database(url, apply_import=True)

    assert report.ok is False
    assert report.legacy_queue_conflict_count == 1
    assert report.legacy_import_pending_count == 1
    assert report.imported_queue_count == 0
    connection = sqlite3.connect(path)
    imported = connection.execute(
        "SELECT COUNT(*) FROM plugin_state WHERE namespace = ? AND key = ?",
        (QUEUE_NAMESPACE, "owner/importable"),
    ).fetchone()
    connection.close()
    assert imported == (0,)


def test_doctor_accepts_stale_legacy_mirror_after_authoritative_import(
    tmp_path: Path,
) -> None:
    path, url = _database(tmp_path)
    _store(
        path,
        LEGACY_NAMESPACE,
        "owner/repo",
        RepositoryState(last_event_id="100").model_dump(mode="json"),
    )
    _store(
        path,
        QUEUE_NAMESPACE,
        "owner/repo",
        QueueState(
            accepted_cursor="101",
            committed_cursor="101",
            legacy_boundary="100",
            legacy_imported=True,
        ).model_dump(mode="json"),
    )

    report = inspect_database(url)

    assert report.ok is True
    assert report.legacy_queue_conflict_count == 0


def test_doctor_accepts_fresh_queue_ahead_of_stale_activation_mirror(
    tmp_path: Path,
) -> None:
    path, url = _database(tmp_path)
    _store(
        path,
        LEGACY_NAMESPACE,
        "owner/repo",
        RepositoryState(last_event_id="100", baseline_notified=False).model_dump(mode="json"),
    )
    _store(
        path,
        QUEUE_NAMESPACE,
        "owner/repo",
        QueueState(
            accepted_cursor="100",
            accepted_fingerprint="a" * 64,
            committed_cursor="100",
            committed_fingerprint="a" * 64,
            activation=ActivationState(
                activation_id="activation-1",
                occurred_at=NOW,
                deliveries=(),
            ),
        ).model_dump(mode="json"),
    )

    report = inspect_database(url)

    assert report.ok is True
    assert report.legacy_queue_conflict_count == 0
