"""Cutover-time C21 Memory owner completeness. Read-only; never fills or merges."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from qq_ai_bot.identity.canonical_memory_schema import memory_fact_canonical_conflict_kind
from qq_ai_bot.identity.errors import IdentityCutoverPreconditionError

C21_OWNERS_INCOMPLETE = "c21_owners_incomplete"
STATE_MISMATCH = "state_mismatch"

_XOR_COLUMNS = ("canonical_person_id", "canonical_space_id")
_DREAM_COLUMNS = (
    "canonical_subject_person_id",
    "canonical_subject_space_id",
    "canonical_visibility_person_id",
    "canonical_visibility_space_id",
)
_DREAM_OPEN = frozenset({"pending", "processing"})
_LIVE_OPEN = frozenset({"pending", "processing"})
_VALID_DREAM_SHAPES = frozenset(
    {
        (False, False, False, False),
        (False, False, True, False),
        (False, False, False, True),
        (True, False, False, False),
        (False, True, False, False),
        (True, True, False, False),
    }
)


def snapshot_c21_cutoff(snapshot_db: str) -> str:
    stamp = datetime.fromtimestamp(Path(snapshot_db).stat().st_mtime, UTC)
    return stamp.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _stamp(value: object) -> str:
    text = str(value or "").strip().replace(" ", "T")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    return text


def _receipt_in_scope(expires_at: object, cutoff: str) -> bool:
    if expires_at is None:
        return True
    return _stamp(expires_at) > _stamp(cutoff)


def _xor_pair(person_id: object, space_id: object) -> tuple[str | None, str | None]:
    person = str(person_id) if person_id else None
    space = str(space_id) if space_id else None
    return person, space


def _xor_complete(pair: tuple[str | None, str | None]) -> bool:
    return bool(pair[0]) != bool(pair[1])


def _owner_ids(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    if not _table_exists(connection, table):
        return frozenset()
    return frozenset(str(row[0]) for row in connection.execute(f'SELECT id FROM "{table}"'))


def _require_existing(
    pair: tuple[str | None, str | None],
    persons: frozenset[str],
    spaces: frozenset[str],
) -> None:
    person_id, space_id = pair
    if person_id is not None and person_id not in persons:
        raise IdentityCutoverPreconditionError(STATE_MISMATCH)
    if space_id is not None and space_id not in spaces:
        raise IdentityCutoverPreconditionError(STATE_MISMATCH)


def _require_xor(
    pair: tuple[str | None, str | None],
    persons: frozenset[str],
    spaces: frozenset[str],
) -> None:
    if not _xor_complete(pair):
        raise IdentityCutoverPreconditionError(C21_OWNERS_INCOMPLETE)
    _require_existing(pair, persons, spaces)


def _fact_shape_complete(row: sqlite3.Row) -> bool:
    scope = str(row["scope_type"] or "")
    visibility = row["visibility_type"]
    subject_person = row["canonical_subject_person_id"]
    subject_space = row["canonical_subject_space_id"]
    visibility_person = row["canonical_visibility_person_id"]
    visibility_space = row["canonical_visibility_space_id"]
    extra_subjects = (subject_person, subject_space)
    extra_visibility = (visibility_person, visibility_space)
    if scope == "self":
        if visibility in {None, "global"}:
            return not any((*extra_subjects, *extra_visibility))
        if visibility == "private":
            return bool(visibility_person) and not any((*extra_subjects, visibility_space))
        if visibility == "group":
            return bool(visibility_space) and not any((*extra_subjects, visibility_person))
        return False
    if scope == "person":
        return bool(subject_person) and not any((subject_space, *extra_visibility))
    if scope == "group":
        return bool(subject_space) and not any((subject_person, *extra_visibility))
    if scope == "person_group":
        return bool(subject_person) and bool(subject_space) and not any(extra_visibility)
    return False


def _dream_shape_complete(row: sqlite3.Row) -> bool:
    flags = tuple(bool(row[column]) for column in _DREAM_COLUMNS)
    return flags in _VALID_DREAM_SHAPES


def _require_jobs(
    connection: sqlite3.Connection, persons: frozenset[str], spaces: frozenset[str]
) -> None:
    """Live pending/processing and every rebuild row (including done) must XOR.

    Live done/failed are not claimed by complete-v2 and are not required here.
    """

    if not _table_exists(connection, "memory_jobs"):
        return
    if not all(_column_exists(connection, "memory_jobs", column) for column in _XOR_COLUMNS):
        return
    for row in connection.execute(
        "SELECT status, processing_source, canonical_person_id, canonical_space_id FROM memory_jobs"
    ):
        source = str(row["processing_source"] or "")
        status = str(row["status"] or "")
        if source == "rebuild" or (source == "live" and status in _LIVE_OPEN):
            _require_xor(
                _xor_pair(row["canonical_person_id"], row["canonical_space_id"]), persons, spaces
            )


def _require_receipts(
    connection: sqlite3.Connection,
    cutoff: str,
    persons: frozenset[str],
    spaces: frozenset[str],
) -> None:
    if not _table_exists(connection, "memory_tool_receipts"):
        return
    if not all(
        _column_exists(connection, "memory_tool_receipts", column)
        for column in (*_XOR_COLUMNS, "expires_at")
    ):
        return
    for row in connection.execute(
        "SELECT expires_at, canonical_person_id, canonical_space_id FROM memory_tool_receipts"
    ):
        if not _receipt_in_scope(row["expires_at"], cutoff):
            continue
        _require_xor(
            _xor_pair(row["canonical_person_id"], row["canonical_space_id"]), persons, spaces
        )


def _require_reflection(
    connection: sqlite3.Connection,
    persons: frozenset[str],
    spaces: frozenset[str],
) -> None:
    if not _table_exists(connection, "memory_self_reflection_states"):
        return
    states = list(
        connection.execute(
            "SELECT id, conversation_key_hash, bot_user_id, pending_events, "
            "canonical_person_id, canonical_space_id FROM memory_self_reflection_states"
        )
    )
    pending = [row for row in states if int(row["pending_events"] or 0) > 0]
    for row in pending:
        _require_xor(
            _xor_pair(row["canonical_person_id"], row["canonical_space_id"]), persons, spaces
        )
    if not _table_exists(connection, "memory_self_reflection_runs"):
        return
    runs = list(
        connection.execute(
            "SELECT conversation_key_hash, bot_user_id, canonical_person_id, canonical_space_id "
            "FROM memory_self_reflection_runs"
        )
    )
    by_legacy: dict[tuple[str, str], list[tuple[str | None, str | None]]] = defaultdict(list)
    by_owner: dict[tuple[str | None, str | None], list[tuple[str | None, str | None]]] = (
        defaultdict(list)
    )
    for row in runs:
        owner = _xor_pair(row["canonical_person_id"], row["canonical_space_id"])
        by_legacy[(str(row["conversation_key_hash"]), str(row["bot_user_id"]))].append(owner)
        if _xor_complete(owner):
            by_owner[owner].append(owner)
    for row in pending:
        expected = _xor_pair(row["canonical_person_id"], row["canonical_space_id"])
        linked: list[tuple[str | None, str | None]] = []
        linked.extend(
            by_legacy.get((str(row["conversation_key_hash"]), str(row["bot_user_id"])), [])
        )
        linked.extend(by_owner.get(expected, []))
        owners = {item for item in linked}
        if any(not _xor_complete(item) for item in owners):
            raise IdentityCutoverPreconditionError(C21_OWNERS_INCOMPLETE)
        if any(item != expected for item in owners):
            raise IdentityCutoverPreconditionError(C21_OWNERS_INCOMPLETE)


def _require_dreams(
    connection: sqlite3.Connection, persons: frozenset[str], spaces: frozenset[str]
) -> None:
    if not _table_exists(connection, "memory_dream_clusters"):
        return
    if not all(
        _column_exists(connection, "memory_dream_clusters", column) for column in _DREAM_COLUMNS
    ):
        return
    for row in connection.execute(
        "SELECT status, canonical_subject_person_id, canonical_subject_space_id, "
        "canonical_visibility_person_id, canonical_visibility_space_id "
        "FROM memory_dream_clusters"
    ):
        if str(row["status"] or "") not in _DREAM_OPEN:
            continue
        if not _dream_shape_complete(row):
            raise IdentityCutoverPreconditionError(C21_OWNERS_INCOMPLETE)
        for column, pool in (
            ("canonical_subject_person_id", persons),
            ("canonical_visibility_person_id", persons),
            ("canonical_subject_space_id", spaces),
            ("canonical_visibility_space_id", spaces),
        ):
            value = row[column]
            if value and str(value) not in pool:
                raise IdentityCutoverPreconditionError(STATE_MISMATCH)


def _require_facts(
    connection: sqlite3.Connection, persons: frozenset[str], spaces: frozenset[str]
) -> None:
    if not _table_exists(connection, "memory_facts"):
        return
    required = (
        "scope_type",
        "visibility_type",
        "status",
        *_DREAM_COLUMNS,
    )
    if not all(_column_exists(connection, "memory_facts", column) for column in required):
        return
    if memory_fact_canonical_conflict_kind(connection) is not None:
        raise IdentityCutoverPreconditionError("canonical_memory_fact_conflict")
    for row in connection.execute(
        "SELECT scope_type, visibility_type, status, "
        "canonical_subject_person_id, canonical_subject_space_id, "
        "canonical_visibility_person_id, canonical_visibility_space_id "
        "FROM memory_facts"
    ):
        if str(row["status"] or "") != "active":
            continue
        if not _fact_shape_complete(row):
            raise IdentityCutoverPreconditionError(C21_OWNERS_INCOMPLETE)
        for column, pool in (
            ("canonical_subject_person_id", persons),
            ("canonical_visibility_person_id", persons),
            ("canonical_subject_space_id", spaces),
            ("canonical_visibility_space_id", spaces),
        ):
            value = row[column]
            if value and str(value) not in pool:
                raise IdentityCutoverPreconditionError(STATE_MISMATCH)


def require_c21_readable_owners(connection: sqlite3.Connection, cutoff: str) -> None:
    """Fail closed when v2-readable Memory rows lack a complete C21 owner.

    `cutoff` is the plan-snapshot instant. Apply must pass the stored cutoff
    so wall-clock expiry cannot skip receipts that were in scope at plan.
    Evidence runtime-readability is not this gate: C7 proves Binding/scope
    alignment; C26 planned alignability plus post-map
    ``require_c21_readable_evidence`` close the v2 chain.
    """

    if not cutoff or not _stamp(cutoff):
        raise IdentityCutoverPreconditionError(STATE_MISMATCH)
    persons = _owner_ids(connection, "persons")
    spaces = _owner_ids(connection, "spaces")
    _require_jobs(connection, persons, spaces)
    _require_receipts(connection, cutoff, persons, spaces)
    _require_reflection(connection, persons, spaces)
    _require_dreams(connection, persons, spaces)
    _require_facts(connection, persons, spaces)


def c21_owner_tables() -> Iterable[str]:
    return (
        "memory_jobs",
        "memory_tool_receipts",
        "memory_self_reflection_states",
        "memory_self_reflection_runs",
        "memory_dream_clusters",
        "memory_facts",
    )
