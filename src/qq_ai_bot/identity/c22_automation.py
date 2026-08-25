"""C22 automation target backfill planner and cutover gate.

sqlite3-only. Fills XOR Person/Space target shadows from planned∪current
Binding overlay. Does not create Conversation, people, or groups. Conflicts
are content-free and block business writes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any, Literal

from qq_ai_bot.identity.backfill_types import IdentityConflict, MemoryOwnerAssignment
from qq_ai_bot.identity.canonical_memory_owners import (
    AMBIGUOUS_OWNER,
    MISSING_OWNER,
    C21OwnerContext,
    _decide_xor,
    _existing_canonical_owner_ids,
    _owner_complete,
    _plan_input_pairs,
    _xor_pair,
    overlay_owner_bindings,
)
from qq_ai_bot.identity.errors import IdentityCutoverPreconditionError
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.sanitize import fingerprint_external_id, normalize_external_id

C22_AUTOMATION_INCOMPLETE = "c22_automation_incomplete"
RUNNABLE_AUTOMATION_STATUSES = frozenset({"active", "paused"})
_SEND_PERSON_CALLS = frozenset({"onebot.send_private_message", "speech.send_private"})
_SEND_SPACE_CALLS = frozenset({"onebot.send_group_message", "speech.send_group"})
_SEND_EITHER_CALLS = frozenset({"emoji.send", "emoji.send_by_id", "admin.execute_action"})

C22_SIGNATURE_SQL = (
    "SELECT id, status, canonical_target_person_id, canonical_target_space_id "
    "FROM automations ORDER BY id"
)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _automations_ready(connection: sqlite3.Connection) -> bool:
    if not _table_exists(connection, "automations"):
        return False
    return all(
        _column_exists(connection, "automations", column)
        for column in (
            "id",
            "status",
            "creator_user_id",
            "script_json",
            "authority_snapshot_json",
            "canonical_target_person_id",
            "canonical_target_space_id",
            "canonical_creator_person_id",
        )
    )


def c22_signature_chunks(connection: sqlite3.Connection) -> list[str]:
    if not _automations_ready(connection):
        return []
    chunks = [C22_SIGNATURE_SQL]
    chunks.extend(str(tuple(row)) for row in connection.execute(C22_SIGNATURE_SQL))
    return chunks


def _c22_conflict(*, pk: int, category: str) -> IdentityConflict:
    conflict_kind: Literal["ambiguous_identity", "unclassified"] = (
        "unclassified" if category == MISSING_OWNER else "ambiguous_identity"
    )
    external_id = f"c22:automations:{pk}"
    return IdentityConflict(
        subject_kind="account",
        conflict_kind=conflict_kind,
        error_category=category,  # type: ignore[arg-type]
        platform=IDENTITY_PLATFORM,
        external_id=external_id,
        fingerprint=fingerprint_external_id(external_id),
    )


def c22_source_material(
    connection: sqlite3.Connection,
    ctx: C21OwnerContext,
    *,
    send_fingerprints: tuple[tuple[object, ...], ...] = (),
) -> tuple[tuple[object, ...], ...]:
    rows: list[tuple[object, ...]] = []
    if _automations_ready(connection):
        for row in connection.execute(C22_SIGNATURE_SQL):
            rows.append(("automations", *tuple(row)))
    rows.extend(send_fingerprints)
    existing_owners = _existing_canonical_owner_ids(connection)
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


def _parse_json_object(raw: object) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _resolved_external(value: object, *, fallback: str | None) -> str | None:
    if value == "$creator_user_id" or value == "$current_group_id":
        return normalize_external_id(fallback)
    if not isinstance(value, str) or "${" in value:
        return None
    return normalize_external_id(value)


def _collect_script_targets(
    script: Mapping[str, Any],
    *,
    creator_user_id: str | None,
    current_group_id: str | None,
) -> tuple[str | None, tuple[str, ...], str | None]:
    person_ids: list[str] = []
    space_ids: list[str] = []
    steps = script.get("steps")
    if not isinstance(steps, list):
        return None, (), None
    for step in steps:
        if not isinstance(step, dict):
            continue
        call = str(step.get("call") or "")
        arguments = step.get("arguments")
        args = arguments if isinstance(arguments, dict) else {}
        if call in _SEND_PERSON_CALLS:
            found = _resolved_external(args.get("user_id"), fallback=creator_user_id)
            if found is None:
                return None, (), MISSING_OWNER
            person_ids.append(found)
        elif call in _SEND_SPACE_CALLS:
            found = _resolved_external(args.get("group_id"), fallback=current_group_id)
            if found is None:
                return None, (), MISSING_OWNER
            space_ids.append(found)
        elif call in _SEND_EITHER_CALLS:
            if args.get("user_id"):
                found = _resolved_external(args.get("user_id"), fallback=creator_user_id)
                if found is None:
                    return None, (), MISSING_OWNER
                person_ids.append(found)
            if args.get("group_id"):
                found = _resolved_external(args.get("group_id"), fallback=current_group_id)
                if found is None:
                    return None, (), MISSING_OWNER
                space_ids.append(found)
        elif call == "onebot.call_api":
            error = _collect_onebot_params(
                args.get("params"),
                person_ids,
                space_ids,
                creator_user_id=creator_user_id,
                current_group_id=current_group_id,
            )
            if error is not None:
                return None, (), error
    if person_ids and space_ids:
        return None, (), AMBIGUOUS_OWNER
    unique_person = tuple(dict.fromkeys(person_ids))
    unique_space = tuple(dict.fromkeys(space_ids))
    if unique_person:
        return "person", unique_person, None
    if unique_space:
        return "space", unique_space, None
    return None, (), None


def _collect_onebot_params(
    value: object,
    person_ids: list[str],
    space_ids: list[str],
    *,
    creator_user_id: str | None,
    current_group_id: str | None,
) -> str | None:
    if not isinstance(value, dict):
        return None
    if "user_id" in value:
        found = _resolved_external(value.get("user_id"), fallback=creator_user_id)
        if found is None:
            return MISSING_OWNER
        person_ids.append(found)
    if "group_id" in value:
        found = _resolved_external(value.get("group_id"), fallback=current_group_id)
        if found is None:
            return MISSING_OWNER
        space_ids.append(found)
    for child in value.values():
        if isinstance(child, dict):
            error = _collect_onebot_params(
                child,
                person_ids,
                space_ids,
                creator_user_id=creator_user_id,
                current_group_id=current_group_id,
            )
            if error is not None:
                return error
    return None


def _resolve_owner(
    ctx: C21OwnerContext,
    *,
    kind: str | None,
    raw_ids: tuple[str, ...],
    creator_user_id: str | None,
    current_group_id: str | None,
) -> tuple[tuple[str | None, str | None] | None, str | None]:
    expected = kind
    ids = raw_ids
    if expected is None:
        if current_group_id:
            expected = "space"
            ids = (current_group_id,)
        elif creator_user_id:
            expected = "person"
            ids = (creator_user_id,)
        else:
            return None, MISSING_OWNER
    owners: set[str] = set()
    for raw in ids:
        external = normalize_external_id(raw)
        if external is None:
            return None, MISSING_OWNER
        if external in ctx.ambiguous_externals:
            return None, AMBIGUOUS_OWNER
        if expected == "person":
            if external in ctx.non_person_accounts:
                return None, MISSING_OWNER
            owner = ctx.person_bindings.get(external)
        else:
            owner = ctx.space_bindings.get(external)
        if owner is None:
            return None, MISSING_OWNER
        owners.add(owner)
    if len(owners) != 1:
        return None, AMBIGUOUS_OWNER
    winner = owners.pop()
    if expected == "person":
        return (winner, None), None
    return (None, winner), None


def plan_c22_automation_targets(
    connection: sqlite3.Connection,
    *,
    planned_person_bindings: Mapping[str, str] | None = None,
    planned_space_bindings: Mapping[str, str] | None = None,
    non_person_accounts: frozenset[str] = frozenset(),
) -> tuple[
    tuple[MemoryOwnerAssignment, ...],
    tuple[IdentityConflict, ...],
    int,
    tuple[tuple[object, ...], ...],
]:
    """Plan XOR target fills for runnable automations. Zero writes on conflict."""

    assignments: list[MemoryOwnerAssignment] = []
    conflicts: list[IdentityConflict] = []
    filled = 0
    send_fingerprints: list[tuple[object, ...]] = []
    ctx = overlay_owner_bindings(
        connection,
        planned_person_bindings=planned_person_bindings or {},
        planned_space_bindings=planned_space_bindings or {},
        non_person_accounts=non_person_accounts,
    )
    if not _automations_ready(connection):
        return (), (), 0, c22_source_material(connection, ctx)
    rows = connection.execute(
        "SELECT id, status, creator_user_id, script_json, authority_snapshot_json, "
        "canonical_target_person_id, canonical_target_space_id FROM automations ORDER BY id"
    ).fetchall()
    for row in rows:
        if str(row["status"] or "") not in RUNNABLE_AUTOMATION_STATUSES:
            continue
        authority = _parse_json_object(row["authority_snapshot_json"])
        creator = normalize_external_id(authority.get("creator_user_id") or row["creator_user_id"])
        group = normalize_external_id(authority.get("current_group_id"))
        kind, raw_ids, collect_error = _collect_script_targets(
            _parse_json_object(row["script_json"]),
            creator_user_id=creator,
            current_group_id=group,
        )
        send_fingerprints.append(
            (
                "automation_send",
                int(row["id"]),
                kind,
                tuple(fingerprint_external_id(item) for item in raw_ids),
                collect_error,
            )
        )
        current = _xor_pair(
            row["canonical_target_person_id"],
            row["canonical_target_space_id"],
        )
        if collect_error is not None:
            conflicts.append(_c22_conflict(pk=int(row["id"]), category=collect_error))
            continue
        resolved, resolve_error = _resolve_owner(
            ctx,
            kind=kind,
            raw_ids=raw_ids,
            creator_user_id=creator,
            current_group_id=group,
        )
        action, winner, error = _decide_xor(current, resolved, resolve_error)
        if action == "conflict" or error is not None or winner is None:
            conflicts.append(_c22_conflict(pk=int(row["id"]), category=error or MISSING_OWNER))
            continue
        if action == "fill":
            assignments.append(
                MemoryOwnerAssignment(
                    table="automations",
                    row_id=int(row["id"]),
                    values=(
                        ("canonical_target_person_id", winner[0]),
                        ("canonical_target_space_id", winner[1]),
                    ),
                )
            )
            filled += 1
    assignments.sort(key=lambda item: item.row_id)
    conflicts.sort(key=lambda item: (item.external_id, item.error_category))
    return (
        tuple(assignments),
        tuple(conflicts),
        filled,
        c22_source_material(connection, ctx, send_fingerprints=tuple(send_fingerprints)),
    )


def require_c22_runnable_automation_targets(connection: sqlite3.Connection) -> None:
    """Fail closed before flip: runnable automations must have a live XOR target."""

    if not _automations_ready(connection):
        return
    person_ids = (
        {str(row[0]) for row in connection.execute("SELECT id FROM persons")}
        if _table_exists(connection, "persons")
        else set()
    )
    space_ids = (
        {str(row[0]) for row in connection.execute("SELECT id FROM spaces")}
        if _table_exists(connection, "spaces")
        else set()
    )
    for row in connection.execute(
        "SELECT id, status, canonical_creator_person_id, "
        "canonical_target_person_id, canonical_target_space_id "
        "FROM automations ORDER BY id"
    ):
        if str(row["status"] or "") not in RUNNABLE_AUTOMATION_STATUSES:
            continue
        creator = str(row["canonical_creator_person_id"] or "").strip()
        if not creator or creator not in person_ids:
            raise IdentityCutoverPreconditionError(C22_AUTOMATION_INCOMPLETE)
        pair = _xor_pair(row["canonical_target_person_id"], row["canonical_target_space_id"])
        if not _owner_complete(pair):
            raise IdentityCutoverPreconditionError(C22_AUTOMATION_INCOMPLETE)
        person_id, space_id = pair
        if person_id and person_id not in person_ids:
            raise IdentityCutoverPreconditionError(C22_AUTOMATION_INCOMPLETE)
        if space_id and space_id not in space_ids:
            raise IdentityCutoverPreconditionError(C22_AUTOMATION_INCOMPLETE)


__all__ = [
    "C22_AUTOMATION_INCOMPLETE",
    "RUNNABLE_AUTOMATION_STATUSES",
    "c22_signature_chunks",
    "c22_source_material",
    "plan_c22_automation_targets",
    "require_c22_runnable_automation_targets",
]
