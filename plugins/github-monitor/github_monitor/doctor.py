"""Stopped-database diagnostics and legacy queue import for GitHub Monitor."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from qq_ai_bot.config import Settings

from .models import QueueState, RepositoryState
from .state import (
    DIAGNOSTIC_NAMESPACE,
    LEGACY_NAMESPACE,
    QUEUE_NAMESPACE,
    import_legacy_queue_state,
)

_PLUGIN_ID = "github-monitor"
_INVALID_JSON = object()
_QUEUE_CONFLICT_CATEGORIES = frozenset(
    {
        "github_queue_state_conflict",
        "github_queue_state_missing_after_cas",
        "github_queue_state_changed_after_cas",
    }
)


@dataclass(frozen=True, slots=True)
class QueueDoctorReport:
    ok: bool
    mode: str
    queue_count: int
    imported_queue_count: int
    legacy_import_pending_count: int
    legacy_queue_conflict_count: int
    invalid_state_count: int
    pending_source_count: int
    inflight_unit_count: int
    activation_pending_delivery_count: int
    backlog_repository_count: int
    cursor_gap_count: int
    cas_conflict_count: int
    active_diagnostic_count: int
    diagnostic_receipt_conflict_count: int
    expired_media_delivery_count: int
    receipt_conflict_count: int
    superseded_covered_count: int
    outbox_pending_count: int
    turn_pending_count: int


def inspect_database(database_url: str, *, apply_import: bool = False) -> QueueDoctorReport:
    """Inspect only sanitized counters; optionally persist deterministic legacy imports."""

    path = _sqlite_path(database_url)
    if not path.is_file():
        raise ValueError("github queue doctor requires an existing canonical database")
    connection = (
        sqlite3.connect(path)
        if apply_import
        else sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    )
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        if apply_import:
            connection.execute("BEGIN IMMEDIATE")
        legacy = _load_namespace(connection, LEGACY_NAMESPACE)
        queued = _load_namespace(connection, QUEUE_NAMESPACE)
        diagnostics = _load_namespace(connection, DIAGNOSTIC_NAMESPACE)
        legacy_states, legacy_invalid = _parse_legacy_states(legacy)
        queue_states, queue_invalid = _parse_queue_states(queued)
        invalid = legacy_invalid + queue_invalid
        legacy_queue_conflicts = _legacy_queue_conflicts(legacy_states, queue_states)
        imported = 0
        if apply_import:
            if invalid or legacy_queue_conflicts:
                connection.rollback()
            else:
                for repository in sorted(legacy.keys() - queued.keys()):
                    state = import_legacy_queue_state(legacy_states[repository], repository)
                    connection.execute(
                        """
                        INSERT INTO plugin_state (
                            plugin_id, namespace, key, value_json, version, expires_at,
                            updated_at, canonical_person_id
                        ) VALUES (?, ?, ?, ?, 1, NULL, ?, NULL)
                        """,
                        (
                            _PLUGIN_ID,
                            QUEUE_NAMESPACE,
                            repository,
                            state.model_dump_json(),
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                    imported += 1
                connection.commit()
            queued = _load_namespace(connection, QUEUE_NAMESPACE)
            queue_states, queue_invalid = _parse_queue_states(queued)
            invalid = legacy_invalid + queue_invalid
            legacy_queue_conflicts = _legacy_queue_conflicts(legacy_states, queue_states)

        pending_import = len(legacy.keys() - queued.keys())
        now = datetime.now(UTC)
        pending_sources = 0
        inflight_units = 0
        activation_pending_deliveries = 0
        backlogs = 0
        gaps = 0
        cas_conflicts = 0
        expired_media = 0
        for state in queue_states.values():
            pending_sources += len(state.pending)
            inflight_units += int(state.inflight is not None)
            if state.activation is not None:
                activation_pending_deliveries += sum(
                    delivery.status not in {"completed", "skipped"}
                    for delivery in state.activation.deliveries
                )
            backlogs += int(state.backlog_pending)
            gaps += int(bool(state.gap_reason))
            cas_conflicts += int(state.gap_reason in _QUEUE_CONFLICT_CATEGORIES)
            if state.inflight is not None:
                for delivery in state.inflight.deliveries:
                    prepared = delivery.prepared
                    if prepared is None or delivery.status not in {"prepared", "attempting"}:
                        continue
                    if any(
                        media.expires_at is not None and media.expires_at <= now
                        for media in prepared.media
                    ):
                        expired_media += 1

        receipt_conflicts = _error_count(connection, "receipt_conflict")
        diagnostic_categories: list[str] = []
        for value in diagnostics.values():
            if not isinstance(value, dict):
                invalid += 1
                continue
            version = value.get("version")
            category = value.get("category")
            recorded_at = value.get("recorded_at")
            if (
                version != 1
                or not isinstance(category, str)
                or not category
                or len(category) > 64
                or not all(
                    character.isascii() and (character.isalnum() or character == "_")
                    for character in category
                )
                or not isinstance(recorded_at, str)
            ):
                invalid += 1
                continue
            try:
                datetime.fromisoformat(recorded_at)
            except ValueError:
                invalid += 1
                continue
            diagnostic_categories.append(category)
        diagnostic_receipt_conflicts = diagnostic_categories.count("receipt_conflict")
        cas_conflicts += sum(item in _QUEUE_CONFLICT_CATEGORIES for item in diagnostic_categories)
        superseded = _turn_error_count(connection, "superseded_covered")
        outbox_pending = _status_count(
            connection,
            "plugin_notification_outbox",
            ("pending", "processing", "uncertain"),
        )
        turn_pending = _status_count(
            connection,
            "plugin_background_turn_jobs",
            ("pending", "processing"),
        )
        ok = not any(
            (
                pending_import,
                legacy_queue_conflicts,
                invalid,
                pending_sources,
                inflight_units,
                activation_pending_deliveries,
                backlogs,
                gaps,
                len(diagnostics),
                expired_media,
                receipt_conflicts,
                outbox_pending,
                turn_pending,
            )
        )
        return QueueDoctorReport(
            ok=ok,
            mode="apply_import" if apply_import else "check",
            queue_count=len(queued),
            imported_queue_count=imported,
            legacy_import_pending_count=pending_import,
            legacy_queue_conflict_count=legacy_queue_conflicts,
            invalid_state_count=invalid,
            pending_source_count=pending_sources,
            inflight_unit_count=inflight_units,
            activation_pending_delivery_count=activation_pending_deliveries,
            backlog_repository_count=backlogs,
            cursor_gap_count=gaps,
            cas_conflict_count=cas_conflicts,
            active_diagnostic_count=len(diagnostics),
            diagnostic_receipt_conflict_count=diagnostic_receipt_conflicts,
            expired_media_delivery_count=expired_media,
            receipt_conflict_count=receipt_conflicts,
            superseded_covered_count=superseded,
            outbox_pending_count=outbox_pending,
            turn_pending_count=turn_pending,
        )
    finally:
        connection.close()


def _sqlite_path(database_url: str) -> Path:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("github queue doctor requires the canonical SQLite database")
    value = database_url.removeprefix(prefix)
    if value == ":memory:":
        raise ValueError("github queue doctor requires a file-backed database")
    return Path(value)


def _load_namespace(connection: sqlite3.Connection, namespace: str) -> dict[str, object]:
    rows = connection.execute(
        """
        SELECT key, value_json
        FROM plugin_state
        WHERE plugin_id = ? AND namespace = ?
          AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
        ORDER BY key
        """,
        (_PLUGIN_ID, namespace),
    ).fetchall()
    result: dict[str, object] = {}
    for key, payload in rows:
        try:
            result[str(key)] = json.loads(str(payload))
        except json.JSONDecodeError:
            result[str(key)] = _INVALID_JSON
    return result


def _parse_legacy_states(
    values: dict[str, object],
) -> tuple[dict[str, RepositoryState], int]:
    parsed: dict[str, RepositoryState] = {}
    invalid = 0
    for repository, raw in values.items():
        try:
            parsed[repository] = RepositoryState.model_validate(raw)
        except ValueError:
            invalid += 1
    return parsed, invalid


def _parse_queue_states(values: dict[str, object]) -> tuple[dict[str, QueueState], int]:
    parsed: dict[str, QueueState] = {}
    invalid = 0
    for repository, raw in values.items():
        try:
            parsed[repository] = QueueState.model_validate(raw)
        except ValueError:
            invalid += 1
    return parsed, invalid


def _legacy_queue_conflicts(
    legacy: dict[str, RepositoryState],
    queued: dict[str, QueueState],
) -> int:
    conflicts = 0
    for repository in legacy.keys() & queued.keys():
        legacy_state = legacy[repository]
        queue_state = queued[repository]
        if queue_state.legacy_imported:
            continue
        legacy_cursor = int(legacy_state.last_event_id or "-1")
        queue_cursor = int(queue_state.committed_cursor or "-1")
        conflicts += int(queue_cursor < legacy_cursor)
    return conflicts


def _error_count(connection: sqlite3.Connection, category: str) -> int:
    outbox = connection.execute(
        """
        SELECT COUNT(*) FROM plugin_notification_outbox
        WHERE plugin_id = ? AND last_error_category = ?
        """,
        (_PLUGIN_ID, category),
    ).fetchone()
    turns = connection.execute(
        """
        SELECT COUNT(*) FROM plugin_background_turn_jobs
        WHERE plugin_id = ? AND last_error_category = ?
        """,
        (_PLUGIN_ID, category),
    ).fetchone()
    return int((outbox or (0,))[0]) + int((turns or (0,))[0])


def _turn_error_count(connection: sqlite3.Connection, category: str) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) FROM plugin_background_turn_jobs
        WHERE plugin_id = ? AND last_error_category = ?
        """,
        (_PLUGIN_ID, category),
    ).fetchone()
    return int((row or (0,))[0])


def _status_count(
    connection: sqlite3.Connection,
    table: str,
    statuses: tuple[str, ...],
) -> int:
    placeholders = ",".join("?" for _ in statuses)
    row = connection.execute(
        f"SELECT COUNT(*) FROM {table} WHERE plugin_id = ? AND status IN ({placeholders})",
        (_PLUGIN_ID, *statuses),
    ).fetchone()
    return int((row or (0,))[0])


async def _run(*, apply_import: bool) -> int:
    report = await asyncio.to_thread(
        inspect_database,
        Settings().database_url,
        apply_import=apply_import,
    )
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0 if report.ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect stopped GitHub Monitor queue state")
    parser.add_argument(
        "--apply-legacy-import",
        action="store_true",
        help="persist deterministic legacy queue imports before checking",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_run(apply_import=bool(args.apply_legacy_import)))
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "error_category": type(exc).__name__},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
