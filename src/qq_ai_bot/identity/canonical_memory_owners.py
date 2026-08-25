"""C21 Memory owner backfill planner.

sqlite3-only. Does not create people/groups/memberships, does not rewrite
facts, and does not swallow IntegrityError. Binding status is ignored.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from qq_ai_bot.identity.backfill_types import (
    IdentityConflict,
    MemoryOwnerAssignment,
    MemoryOwnerCounts,
    ShadowAssignment,
)
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.sanitize import fingerprint_external_id, normalize_external_id

MISSING_OWNER = "missing_owner"
AMBIGUOUS_OWNER = "ambiguous_owner"
CANONICAL_DUPLICATE = "canonical_duplicate"
MIXED_DREAM_SOURCE = "mixed_dream_source"
STATE_RUN_AMBIGUOUS = "state_run_ambiguous"
REFLECTION_OWNER_UNIQUE = "reflection_owner_unique"
INCOMPLETE_DREAM_SHAPE = "incomplete_dream_shape"

_MISSING_KINDS = frozenset({MISSING_OWNER, INCOMPLETE_DREAM_SHAPE})
OwnerPair = tuple[str | None, str | None]
DreamShape = tuple[str | None, str | None, str | None, str | None]

C21_SIGNATURE_SQL: tuple[tuple[str, str], ...] = (
    (
        "memory_jobs",
        "SELECT id, event_id, processing_source, canonical_person_id, canonical_space_id "
        "FROM memory_jobs ORDER BY id",
    ),
    (
        "memory_tool_receipts",
        "SELECT id, trigger_event_id, canonical_person_id, canonical_space_id "
        "FROM memory_tool_receipts ORDER BY id",
    ),
    (
        "memory_self_reflection_states",
        "SELECT id, conversation_key_hash, bot_user_id, scope_type, group_id, "
        "private_peer_user_id, canonical_person_id, canonical_space_id "
        "FROM memory_self_reflection_states ORDER BY id",
    ),
    (
        "memory_self_reflection_runs",
        "SELECT id, conversation_key_hash, bot_user_id, scheduled_slot, "
        "first_event_id, last_event_id, canonical_person_id, canonical_space_id "
        "FROM memory_self_reflection_runs ORDER BY id",
    ),
    (
        "memory_dream_clusters",
        "SELECT id, canonical_subject_person_id, canonical_subject_space_id, "
        "canonical_visibility_person_id, canonical_visibility_space_id, fact_ids_json "
        "FROM memory_dream_clusters ORDER BY id",
    ),
    (
        "memory_facts",
        "SELECT id, scope_type, visibility_type, status, "
        "canonical_subject_person_id, canonical_subject_space_id, "
        "canonical_visibility_person_id, canonical_visibility_space_id "
        "FROM memory_facts ORDER BY id",
    ),
    (
        "memory_evidence",
        "SELECT id, fact_id, event_id, tool_receipt_id, relation, authority "
        "FROM memory_evidence ORDER BY id",
    ),
    (
        "canonical_conversations",
        "SELECT id, kind, person_id, space_id FROM canonical_conversations ORDER BY id",
    ),
)

C21_EVIDENCE_EVENT_SCOPE_SQL = (
    "SELECT e.id, e.scope_type, e.group_id, e.private_peer_user_id, "
    "e.sender_user_id, e.direction, e.canonical_conversation_id "
    "FROM chat_events e WHERE e.id IN ("
    "SELECT event_id FROM memory_evidence WHERE event_id IS NOT NULL "
    "UNION "
    "SELECT r.trigger_event_id FROM memory_evidence ev "
    "JOIN memory_tool_receipts r ON r.id = ev.tool_receipt_id "
    "WHERE ev.event_id IS NULL AND ev.tool_receipt_id IS NOT NULL"
    ") ORDER BY e.id"
)
_EVIDENCE_EVENT_SCOPE_COLUMNS = (
    "scope_type",
    "group_id",
    "private_peer_user_id",
    "sender_user_id",
    "direction",
    "canonical_conversation_id",
)

XOR_COLUMNS = ("canonical_person_id", "canonical_space_id")
DREAM_COLUMNS = (
    "canonical_subject_person_id",
    "canonical_subject_space_id",
    "canonical_visibility_person_id",
    "canonical_visibility_space_id",
)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _evidence_event_scope_ready(connection: sqlite3.Connection) -> bool:
    if not _table_exists(connection, "chat_events"):
        return False
    if not _table_exists(connection, "memory_evidence"):
        return False
    if not _table_exists(connection, "memory_tool_receipts"):
        return False
    return all(
        _column_exists(connection, "chat_events", column)
        for column in _EVIDENCE_EVENT_SCOPE_COLUMNS
    )


def c21_signature_chunks(connection: sqlite3.Connection) -> list[str]:
    chunks: list[str] = []
    for table, sql in C21_SIGNATURE_SQL:
        if not _table_exists(connection, table):
            continue
        if table == "memory_evidence" and not all(
            _column_exists(connection, table, column)
            for column in ("id", "fact_id", "event_id", "tool_receipt_id", "relation", "authority")
        ):
            continue
        chunks.append(sql)
        if table == "memory_dream_clusters":
            for row in connection.execute(sql):
                chunks.append(
                    str((row[0], row[1], row[2], row[3], row[4], _fact_ids_digest(row[5])))
                )
            continue
        chunks.extend(str(tuple(row)) for row in connection.execute(sql))
    if _evidence_event_scope_ready(connection):
        chunks.append(C21_EVIDENCE_EVENT_SCOPE_SQL)
        chunks.extend(str(tuple(row)) for row in connection.execute(C21_EVIDENCE_EVENT_SCOPE_SQL))
    return chunks


def _normalized_pairs(mapping: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    items: list[tuple[str, str]] = []
    for raw, owner in mapping.items():
        external = normalize_external_id(raw)
        if external is None or not owner:
            continue
        items.append((external, str(owner)))
    return tuple(sorted(items))


def _existing_canonical_owner_ids(connection: sqlite3.Connection) -> frozenset[str]:
    ids: set[str] = set()
    if _table_exists(connection, "persons"):
        ids.update(str(row[0]) for row in connection.execute("SELECT id FROM persons"))
    if _table_exists(connection, "spaces"):
        ids.update(str(row[0]) for row in connection.execute("SELECT id FROM spaces"))
    return frozenset(ids)


def _plan_input_pairs(
    mapping: Mapping[str, str], existing_owner_ids: frozenset[str]
) -> tuple[tuple[str, str], ...]:
    items: list[tuple[str, str]] = []
    for raw, owner in mapping.items():
        external = normalize_external_id(raw)
        if external is None or not owner:
            continue
        token = str(owner) if str(owner) in existing_owner_ids else "planned-create"
        items.append((external, token))
    return tuple(sorted(items))


def c21_source_material(
    connection: sqlite3.Connection,
    ctx: C21OwnerContext | None = None,
    *,
    planned_person_bindings: Mapping[str, str] | None = None,
    planned_space_bindings: Mapping[str, str] | None = None,
) -> tuple[tuple[object, ...], ...]:
    rows: list[tuple[object, ...]] = []
    for table, sql in C21_SIGNATURE_SQL:
        if not _table_exists(connection, table):
            continue
        if table == "memory_evidence" and not all(
            _column_exists(connection, table, column)
            for column in ("id", "fact_id", "event_id", "tool_receipt_id", "relation", "authority")
        ):
            continue
        if table == "memory_dream_clusters":
            for row in connection.execute(sql):
                rows.append(
                    (table, row[0], row[1], row[2], row[3], row[4], _fact_ids_digest(row[5]))
                )
            continue
        for row in connection.execute(sql):
            rows.append((table, *tuple(row)))
    if _evidence_event_scope_ready(connection):
        for row in connection.execute(C21_EVIDENCE_EVENT_SCOPE_SQL):
            rows.append(("evidence_event_scope", *tuple(row)))
    current_person = _binding_map(
        connection, "identity_bindings", "external_account_id", "person_id"
    )
    current_space = _binding_map(connection, "space_bindings", "external_space_id", "space_id")
    existing_owners = _existing_canonical_owner_ids(connection)
    rows.append(("current_person_bindings", *_normalized_pairs(current_person)))
    rows.append(("current_space_bindings", *_normalized_pairs(current_space)))
    rows.append(
        (
            "planned_person_bindings",
            *_plan_input_pairs(planned_person_bindings or {}, existing_owners),
        )
    )
    rows.append(
        (
            "planned_space_bindings",
            *_plan_input_pairs(planned_space_bindings or {}, existing_owners),
        )
    )
    if ctx is not None:
        rows.append(
            (
                "merged_person_bindings",
                *_plan_input_pairs(dict(ctx.person_bindings), existing_owners),
            )
        )
        rows.append(
            (
                "merged_space_bindings",
                *_plan_input_pairs(dict(ctx.space_bindings), existing_owners),
            )
        )
        rows.append(("non_person_accounts", *sorted(ctx.non_person_accounts)))
        rows.append(("ambiguous_externals", *sorted(ctx.ambiguous_externals)))
    return tuple(rows)


def merge_source_fingerprint(c7: str, material: tuple[tuple[object, ...], ...]) -> str:
    if not material:
        return c7
    encoded = json.dumps(material, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(f"{c7}\n{encoded}".encode()).hexdigest()


def _fact_ids_digest(raw: object) -> str:
    parsed = _parse_fact_ids(raw)
    if parsed is None:
        return "invalid"
    return hashlib.sha256(",".join(str(item) for item in parsed).encode()).hexdigest()[:16]


def _parse_fact_ids(raw: object) -> tuple[int, ...] | None:
    if raw is None:
        return None
    try:
        data = json.loads(str(raw))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    ids: list[int] = []
    for item in data:
        if not isinstance(item, int) or isinstance(item, bool):
            return None
        ids.append(item)
    return tuple(ids)


def _c21_conflict(
    *,
    table: str,
    pk: int,
    category: str,
    person_id: str | None = None,
    space_id: str | None = None,
) -> IdentityConflict:
    subject_kind: Literal["account", "space"] = "space" if space_id and not person_id else "account"
    conflict_kind: Literal["ambiguous_identity", "unclassified"] = (
        "unclassified" if category in _MISSING_KINDS else "ambiguous_identity"
    )
    external_id = f"c21:{table}:{pk}"
    return IdentityConflict(
        subject_kind=subject_kind,
        conflict_kind=conflict_kind,
        error_category=category,  # type: ignore[arg-type]
        platform=IDENTITY_PLATFORM,
        external_id=external_id,
        fingerprint=fingerprint_external_id(external_id),
    )


def _xor_pair(person_id: object, space_id: object) -> OwnerPair:
    person = str(person_id) if person_id else None
    space = str(space_id) if space_id else None
    return person, space


def _owner_complete(pair: OwnerPair) -> bool:
    return bool(pair[0]) != bool(pair[1])


@dataclass(frozen=True, slots=True)
class C21OwnerContext:
    person_bindings: Mapping[str, str]
    space_bindings: Mapping[str, str]
    non_person_accounts: frozenset[str]
    ambiguous_externals: frozenset[str]


def _merge_owner_maps(
    current: Mapping[str, str], planned: Mapping[str, str]
) -> tuple[dict[str, str], set[str]]:
    merged = dict(current)
    ambiguous: set[str] = set()
    for raw_external, owner in planned.items():
        external = normalize_external_id(raw_external)
        if external is None or not owner:
            continue
        if external in merged and merged[external] != owner:
            ambiguous.add(external)
            merged.pop(external, None)
            continue
        merged[external] = owner
    return merged, ambiguous


def overlay_owner_bindings(
    connection: sqlite3.Connection,
    *,
    planned_person_bindings: Mapping[str, str],
    planned_space_bindings: Mapping[str, str],
    non_person_accounts: frozenset[str],
) -> C21OwnerContext:
    current_person = _binding_map(
        connection, "identity_bindings", "external_account_id", "person_id"
    )
    current_space = _binding_map(connection, "space_bindings", "external_space_id", "space_id")
    person_bindings, person_ambiguous = _merge_owner_maps(current_person, planned_person_bindings)
    space_bindings, space_ambiguous = _merge_owner_maps(current_space, planned_space_bindings)
    blocked = {
        normalized for item in non_person_accounts if (normalized := normalize_external_id(item))
    }
    for external in blocked:
        person_bindings.pop(external, None)
    return C21OwnerContext(
        person_bindings=person_bindings,
        space_bindings=space_bindings,
        non_person_accounts=frozenset(blocked),
        ambiguous_externals=frozenset(person_ambiguous | space_ambiguous),
    )


def _binding_map(
    connection: sqlite3.Connection,
    table: str,
    external_column: str,
    owner_column: str,
) -> dict[str, str]:
    if not _table_exists(connection, table):
        return {}
    mapping: dict[str, str] = {}
    for row in connection.execute(
        f'SELECT "{external_column}", "{owner_column}" FROM "{table}" WHERE platform = ?',
        (IDENTITY_PLATFORM,),
    ):
        external = normalize_external_id(row[0])
        owner = str(row[1] or "")
        if external is None or not owner:
            continue
        mapping[external] = owner
    return mapping


def _conversation_owner(
    connection: sqlite3.Connection, conversation_id: object
) -> tuple[OwnerPair | None, str | None]:
    if not conversation_id:
        return None, MISSING_OWNER
    if not _table_exists(connection, "canonical_conversations"):
        return None, MISSING_OWNER
    row = connection.execute(
        "SELECT kind, person_id, space_id FROM canonical_conversations WHERE id = ?",
        (str(conversation_id),),
    ).fetchone()
    if row is None:
        return None, MISSING_OWNER
    kind = str(row["kind"] or "")
    pair = _xor_pair(row["person_id"], row["space_id"])
    if kind == "private" and pair[0] and pair[1] is None:
        return pair, None
    if kind == "space" and pair[1] and pair[0] is None:
        return pair, None
    return None, AMBIGUOUS_OWNER


def _event_expected_kind(event: sqlite3.Row) -> str | None:
    if event["group_id"] or str(event["scope_type"] or "") == "group":
        return "space"
    if str(event["scope_type"] or "") == "private" or event["private_peer_user_id"]:
        return "private"
    return None


def _private_peer_external(event: sqlite3.Row) -> str | None:
    peer = normalize_external_id(event["private_peer_user_id"])
    if peer is not None:
        return peer
    if str(event["direction"] or "") == "inbound":
        return normalize_external_id(event["sender_user_id"])
    return None


def _binding_owner_for_scope(
    ctx: C21OwnerContext,
    *,
    expected: str,
    group_id: object,
    private_peer: str | None,
) -> tuple[OwnerPair | None, str | None]:
    if expected == "space":
        external = normalize_external_id(group_id)
        if external is None:
            return None, MISSING_OWNER
        if external in ctx.ambiguous_externals:
            return None, AMBIGUOUS_OWNER
        space_id = ctx.space_bindings.get(external)
        if space_id is None:
            return None, MISSING_OWNER
        return (None, space_id), None
    if private_peer is None:
        return None, MISSING_OWNER
    if private_peer in ctx.non_person_accounts:
        return None, MISSING_OWNER
    if private_peer in ctx.ambiguous_externals:
        return None, AMBIGUOUS_OWNER
    person_id = ctx.person_bindings.get(private_peer)
    if person_id is None:
        return None, MISSING_OWNER
    return (person_id, None), None


def _event_owner(
    connection: sqlite3.Connection,
    event_id: object,
    ctx: C21OwnerContext,
) -> tuple[OwnerPair | None, str | None]:
    if event_id is None or not _table_exists(connection, "chat_events"):
        return None, MISSING_OWNER
    event = connection.execute(
        "SELECT scope_type, group_id, private_peer_user_id, sender_user_id, direction, "
        "canonical_conversation_id FROM chat_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if event is None:
        return None, MISSING_OWNER
    expected = _event_expected_kind(event)
    if expected is None:
        return None, MISSING_OWNER
    binding_owner, binding_error = _binding_owner_for_scope(
        ctx,
        expected=expected,
        group_id=event["group_id"],
        private_peer=_private_peer_external(event),
    )
    conversation_id = event["canonical_conversation_id"]
    if not conversation_id:
        return binding_owner, binding_error
    conversation_owner, conversation_error = _conversation_owner(connection, conversation_id)
    if conversation_error is not None or conversation_owner is None:
        return None, conversation_error or MISSING_OWNER
    kind = "space" if conversation_owner[1] else "private"
    if kind != expected:
        return None, AMBIGUOUS_OWNER
    if binding_error is not None or binding_owner is None or binding_owner != conversation_owner:
        return None, AMBIGUOUS_OWNER
    return conversation_owner, None


def _decide_xor(
    current: OwnerPair, resolved: OwnerPair | None, error: str | None
) -> tuple[str, OwnerPair | None, str | None]:
    if error is not None or resolved is None or not _owner_complete(resolved):
        return "conflict", None, error or MISSING_OWNER
    if current == (None, None):
        return "fill", resolved, None
    if current == resolved:
        return "keep", resolved, None
    return "conflict", None, AMBIGUOUS_OWNER


def _xor_assignment(table: str, row_id: int, owner: OwnerPair) -> MemoryOwnerAssignment:
    return MemoryOwnerAssignment(
        table=table,
        row_id=row_id,
        values=(
            ("canonical_person_id", owner[0]),
            ("canonical_space_id", owner[1]),
        ),
    )


def _fact_complete(fact: Mapping[str, Any]) -> bool:
    scope = str(fact.get("scope_type") or "")
    visibility = fact.get("visibility_type")
    subject_person = fact.get("canonical_subject_person_id")
    subject_space = fact.get("canonical_subject_space_id")
    visibility_person = fact.get("canonical_visibility_person_id")
    visibility_space = fact.get("canonical_visibility_space_id")
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


def _overlay_facts(
    connection: sqlite3.Connection, shadows: tuple[ShadowAssignment, ...]
) -> dict[int, dict[str, Any]]:
    if not _table_exists(connection, "memory_facts"):
        return {}
    facts: dict[int, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT id, scope_type, visibility_type, kind, memory_key, status, "
        "canonical_subject_person_id, canonical_subject_space_id, "
        "canonical_visibility_person_id, canonical_visibility_space_id "
        "FROM memory_facts ORDER BY id"
    ):
        facts[int(row["id"])] = {key: row[key] for key in row.keys()}
    for shadow in shadows:
        if shadow.table != "memory_facts":
            continue
        row_id: int | None = None
        for key, value in shadow.row_key:
            if key == "id" and value is not None:
                row_id = int(str(value))
                break
        if row_id is None or row_id not in facts:
            continue
        facts[row_id][shadow.column] = shadow.value
    return facts


def _fact_duplicate_key(fact: Mapping[str, Any]) -> tuple[object, ...] | None:
    if str(fact.get("status") or "") != "active":
        return None
    scope = str(fact.get("scope_type") or "")
    kind = fact.get("kind")
    memory_key = fact.get("memory_key")
    if scope == "person" and fact.get("canonical_subject_person_id"):
        return ("person", fact.get("canonical_subject_person_id"), kind, memory_key)
    if (
        scope == "person_group"
        and fact.get("canonical_subject_person_id")
        and fact.get("canonical_subject_space_id")
    ):
        return (
            "person_group",
            fact.get("canonical_subject_person_id"),
            fact.get("canonical_subject_space_id"),
            kind,
            memory_key,
        )
    if scope == "group" and fact.get("canonical_subject_space_id"):
        return ("group", fact.get("canonical_subject_space_id"), kind, memory_key)
    if scope == "self":
        return (
            "self",
            memory_key,
            fact.get("visibility_type"),
            fact.get("canonical_visibility_person_id") or "",
            fact.get("canonical_visibility_space_id") or "",
        )
    return None


class _C21Plan:
    def __init__(self) -> None:
        self.assignments: list[MemoryOwnerAssignment] = []
        self.conflicts: list[IdentityConflict] = []
        self.jobs = 0
        self.receipts = 0
        self.states = 0
        self.runs = 0
        self.dreams = 0
        self.facts_verified = 0

    def conflict(
        self,
        table: str,
        pk: int,
        category: str,
        *,
        person_id: str | None = None,
        space_id: str | None = None,
    ) -> None:
        self.conflicts.append(
            _c21_conflict(
                table=table,
                pk=pk,
                category=category,
                person_id=person_id,
                space_id=space_id,
            )
        )


def _plan_event_owned_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    event_column: str,
    planned: _C21Plan,
    count_attr: str,
    ctx: C21OwnerContext,
) -> None:
    if not _table_exists(connection, table) or not _column_exists(connection, table, event_column):
        return
    for row in connection.execute(
        f'SELECT id, "{event_column}", canonical_person_id, canonical_space_id FROM "{table}" '
        "ORDER BY id"
    ):
        current = _xor_pair(row["canonical_person_id"], row["canonical_space_id"])
        resolved, error = _event_owner(connection, row[event_column], ctx)
        action, owner, category = _decide_xor(current, resolved, error)
        if action == "conflict" or owner is None:
            planned.conflict(
                table,
                int(row["id"]),
                category or MISSING_OWNER,
                person_id=None if resolved is None else resolved[0],
                space_id=None if resolved is None else resolved[1],
            )
            continue
        if action == "fill":
            planned.assignments.append(_xor_assignment(table, int(row["id"]), owner))
            setattr(planned, count_attr, getattr(planned, count_attr) + 1)


def _plan_reflection_states(
    connection: sqlite3.Connection,
    ctx: C21OwnerContext,
    planned: _C21Plan,
) -> tuple[dict[int, OwnerPair], set[int], dict[tuple[str, str], list[int]]]:
    effective: dict[int, OwnerPair] = {}
    conflicted: set[int] = set()
    by_legacy: dict[tuple[str, str], list[int]] = defaultdict(list)
    if not _table_exists(connection, "memory_self_reflection_states"):
        return effective, conflicted, by_legacy
    rows = list(
        connection.execute(
            "SELECT id, conversation_key_hash, bot_user_id, scope_type, group_id, "
            "private_peer_user_id, canonical_person_id, canonical_space_id "
            "FROM memory_self_reflection_states ORDER BY id"
        )
    )
    pending: dict[int, OwnerPair] = {}
    by_owner: dict[OwnerPair, list[int]] = defaultdict(list)
    for row in rows:
        row_id = int(row["id"])
        by_legacy[(str(row["conversation_key_hash"]), str(row["bot_user_id"]))].append(row_id)
        current = _xor_pair(row["canonical_person_id"], row["canonical_space_id"])
        expected = "space" if str(row["scope_type"] or "") == "group" else "private"
        resolved, error = _binding_owner_for_scope(
            ctx,
            expected=expected,
            group_id=row["group_id"],
            private_peer=normalize_external_id(row["private_peer_user_id"]),
        )
        action, owner, category = _decide_xor(current, resolved, error)
        if action == "conflict" or owner is None:
            planned.conflict(
                "memory_self_reflection_states",
                row_id,
                category or MISSING_OWNER,
                person_id=None if resolved is None else resolved[0],
                space_id=None if resolved is None else resolved[1],
            )
            conflicted.add(row_id)
            continue
        effective[row_id] = owner
        by_owner[owner].append(row_id)
        if action == "fill":
            pending[row_id] = owner
    for owner, ids in by_owner.items():
        if len(ids) < 2:
            continue
        for row_id in ids:
            if row_id not in conflicted:
                planned.conflict(
                    "memory_self_reflection_states",
                    row_id,
                    REFLECTION_OWNER_UNIQUE,
                    person_id=owner[0],
                    space_id=owner[1],
                )
                conflicted.add(row_id)
            pending.pop(row_id, None)
            effective.pop(row_id, None)
    for row_id, owner in pending.items():
        planned.assignments.append(_xor_assignment("memory_self_reflection_states", row_id, owner))
        planned.states += 1
    return effective, conflicted, by_legacy


def _plan_reflection_runs(
    connection: sqlite3.Connection,
    state_effective: Mapping[int, OwnerPair],
    state_conflicted: set[int],
    states_by_legacy: Mapping[tuple[str, str], list[int]],
    planned: _C21Plan,
    ctx: C21OwnerContext,
) -> None:
    if not _table_exists(connection, "memory_self_reflection_runs"):
        return
    pending: dict[int, OwnerPair] = {}
    by_unique: dict[tuple[OwnerPair, str], list[int]] = defaultdict(list)
    for row in connection.execute(
        "SELECT id, conversation_key_hash, bot_user_id, scheduled_slot, "
        "first_event_id, last_event_id, canonical_person_id, canonical_space_id "
        "FROM memory_self_reflection_runs ORDER BY id"
    ):
        row_id = int(row["id"])
        current = _xor_pair(row["canonical_person_id"], row["canonical_space_id"])
        matches = states_by_legacy.get(
            (str(row["conversation_key_hash"]), str(row["bot_user_id"])),
            [],
        )
        resolved: OwnerPair | None = None
        error: str | None
        if len(matches) == 1:
            state_id = matches[0]
            if state_id in state_conflicted:
                error = STATE_RUN_AMBIGUOUS
            else:
                resolved = state_effective.get(state_id)
                error = (
                    None if resolved is not None and _owner_complete(resolved) else MISSING_OWNER
                )
        else:
            first, first_error = _event_owner(connection, row["first_event_id"], ctx)
            last, last_error = _event_owner(connection, row["last_event_id"], ctx)
            if first is None and last is None:
                error = (
                    MISSING_OWNER
                    if first_error == MISSING_OWNER and last_error == MISSING_OWNER
                    else STATE_RUN_AMBIGUOUS
                )
            elif first is None or last is None or first != last:
                error = STATE_RUN_AMBIGUOUS
            else:
                resolved = first
                error = None
        action, owner, category = _decide_xor(current, resolved, error)
        if action == "conflict" or owner is None:
            planned.conflict(
                "memory_self_reflection_runs",
                row_id,
                category or MISSING_OWNER,
                person_id=None if resolved is None else resolved[0],
                space_id=None if resolved is None else resolved[1],
            )
            continue
        slot = str(row["scheduled_slot"])
        by_unique[(owner, slot)].append(row_id)
        if action == "fill":
            pending[row_id] = owner
    for (owner, _slot), ids in by_unique.items():
        if len(ids) < 2:
            continue
        for row_id in ids:
            planned.conflict(
                "memory_self_reflection_runs",
                row_id,
                REFLECTION_OWNER_UNIQUE,
                person_id=owner[0],
                space_id=owner[1],
            )
            pending.pop(row_id, None)
    for row_id, owner in pending.items():
        planned.assignments.append(_xor_assignment("memory_self_reflection_runs", row_id, owner))
        planned.runs += 1


def _plan_dreams(
    connection: sqlite3.Connection,
    facts: Mapping[int, Mapping[str, Any]],
    planned: _C21Plan,
) -> None:
    if not _table_exists(connection, "memory_dream_clusters"):
        return
    for row in connection.execute(
        "SELECT id, fact_ids_json, canonical_subject_person_id, canonical_subject_space_id, "
        "canonical_visibility_person_id, canonical_visibility_space_id "
        "FROM memory_dream_clusters ORDER BY id"
    ):
        row_id = int(row["id"])
        fact_ids = _parse_fact_ids(row["fact_ids_json"])
        if fact_ids is None:
            planned.conflict("memory_dream_clusters", row_id, INCOMPLETE_DREAM_SHAPE)
            continue
        shapes: set[DreamShape] = set()
        incomplete = False
        for fact_id in fact_ids:
            fact = facts.get(fact_id)
            if fact is None or not _fact_complete(fact):
                incomplete = True
                break
            shapes.add(
                (
                    fact.get("canonical_subject_person_id"),
                    fact.get("canonical_subject_space_id"),
                    fact.get("canonical_visibility_person_id"),
                    fact.get("canonical_visibility_space_id"),
                )
            )
        if incomplete:
            planned.conflict("memory_dream_clusters", row_id, INCOMPLETE_DREAM_SHAPE)
            continue
        if len(shapes) != 1:
            planned.conflict("memory_dream_clusters", row_id, MIXED_DREAM_SOURCE)
            continue
        shape = next(iter(shapes))
        current = (
            row["canonical_subject_person_id"],
            row["canonical_subject_space_id"],
            row["canonical_visibility_person_id"],
            row["canonical_visibility_space_id"],
        )
        if current == shape:
            continue
        if any(current):
            planned.conflict(
                "memory_dream_clusters",
                row_id,
                AMBIGUOUS_OWNER,
                person_id=shape[0] or shape[2],
                space_id=shape[1] or shape[3],
            )
            continue
        planned.assignments.append(
            MemoryOwnerAssignment(
                table="memory_dream_clusters",
                row_id=row_id,
                values=tuple(zip(DREAM_COLUMNS, shape, strict=True)),
            )
        )
        planned.dreams += 1


def _plan_facts(facts: Mapping[int, Mapping[str, Any]], planned: _C21Plan) -> None:
    groups: dict[tuple[object, ...], list[int]] = defaultdict(list)
    for fact_id, fact in facts.items():
        if not _fact_complete(fact):
            planned.conflict(
                "memory_facts",
                fact_id,
                MISSING_OWNER,
                person_id=fact.get("canonical_subject_person_id")
                or fact.get("canonical_visibility_person_id"),
                space_id=fact.get("canonical_subject_space_id")
                or fact.get("canonical_visibility_space_id"),
            )
            continue
        key = _fact_duplicate_key(fact)
        if key is not None:
            groups[key].append(fact_id)
        planned.facts_verified += 1
    for key, ids in groups.items():
        if len(ids) < 2:
            continue
        person_id = key[1] if key[0] in {"person", "person_group"} else None
        space_id = None
        if key[0] == "group":
            space_id = key[1]
        elif key[0] == "person_group":
            space_id = key[2]
        for fact_id in ids:
            planned.conflict(
                "memory_facts",
                fact_id,
                CANONICAL_DUPLICATE,
                person_id=str(person_id) if person_id else None,
                space_id=str(space_id) if space_id else None,
            )
            planned.facts_verified = max(0, planned.facts_verified - 1)


def _plan_evidence(
    connection: sqlite3.Connection,
    facts: Mapping[int, Mapping[str, Any]],
    planned: _C21Plan,
    ctx: C21OwnerContext,
) -> None:
    from qq_ai_bot.identity.c21_evidence import iter_unproven_legacy_evidence

    for evidence_id, category in iter_unproven_legacy_evidence(connection, facts, ctx):
        planned.conflict("memory_evidence", evidence_id, category)


def plan_c21_memory_owners(
    connection: sqlite3.Connection,
    *,
    planned_shadows: tuple[ShadowAssignment, ...] = (),
    planned_person_bindings: Mapping[str, str] | None = None,
    planned_space_bindings: Mapping[str, str] | None = None,
    non_person_accounts: frozenset[str] = frozenset(),
) -> tuple[
    tuple[MemoryOwnerAssignment, ...],
    tuple[IdentityConflict, ...],
    MemoryOwnerCounts,
    tuple[tuple[object, ...], ...],
]:
    """Complete C21 owner plan. Callers apply only when the merged plan has zero conflicts."""

    planned = _C21Plan()
    ctx = overlay_owner_bindings(
        connection,
        planned_person_bindings=planned_person_bindings or {},
        planned_space_bindings=planned_space_bindings or {},
        non_person_accounts=non_person_accounts,
    )
    _plan_event_owned_rows(
        connection,
        table="memory_jobs",
        event_column="event_id",
        planned=planned,
        count_attr="jobs",
        ctx=ctx,
    )
    _plan_event_owned_rows(
        connection,
        table="memory_tool_receipts",
        event_column="trigger_event_id",
        planned=planned,
        count_attr="receipts",
        ctx=ctx,
    )
    effective, conflicted, by_legacy = _plan_reflection_states(connection, ctx, planned)
    _plan_reflection_runs(connection, effective, conflicted, by_legacy, planned, ctx)
    facts = _overlay_facts(connection, planned_shadows)
    _plan_dreams(connection, facts, planned)
    _plan_facts(facts, planned)
    _plan_evidence(connection, facts, planned, ctx)
    planned.assignments.sort(key=lambda item: (item.table, item.row_id, item.values))
    planned.conflicts.sort(key=lambda item: (item.external_id, item.error_category))
    return (
        tuple(planned.assignments),
        tuple(planned.conflicts),
        MemoryOwnerCounts(
            jobs=planned.jobs,
            receipts=planned.receipts,
            reflection_states=planned.states,
            reflection_runs=planned.runs,
            dream_clusters=planned.dreams,
            facts_verified=planned.facts_verified,
        ),
        c21_source_material(
            connection,
            ctx,
            planned_person_bindings=planned_person_bindings or {},
            planned_space_bindings=planned_space_bindings or {},
        ),
    )
