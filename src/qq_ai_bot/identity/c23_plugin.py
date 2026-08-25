"""C23 plugin canonical backfill planner and cutover gate.

sqlite3-only. Fills plugin shadows from planned∪current Binding overlay and
known non-person accounts. Does not create Person, Space, Presence, or
Conversation. Existing shadows that conflict with proof are sanitized
conflicts and are never overwritten.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from typing import Literal

from qq_ai_bot.identity.backfill_types import IdentityConflict, ShadowAssignment
from qq_ai_bot.identity.canonical_memory_owners import (
    AMBIGUOUS_OWNER,
    MISSING_OWNER,
    C21OwnerContext,
    _existing_canonical_owner_ids,
    _merge_owner_maps,
    _owner_complete,
    _plan_input_pairs,
    _xor_pair,
    overlay_owner_bindings,
)
from qq_ai_bot.identity.errors import IdentityCutoverPreconditionError
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.sanitize import fingerprint_external_id, normalize_external_id

C23_PLUGIN_INCOMPLETE = "c23_plugin_incomplete"
LIVE_OUTBOX_STATUSES = frozenset({"pending", "processing"})
LIVE_JOB_STATUSES = frozenset({"pending", "processing"})
_USER_MESSAGE_ROLE = "user"

C23_SIGNATURE_SQL: tuple[tuple[str, str], ...] = (
    (
        "plugin_config_values",
        "SELECT id, scope_type, canonical_person_id, canonical_space_id "
        "FROM plugin_config_values ORDER BY id",
    ),
    (
        "plugin_state",
        "SELECT id, canonical_person_id FROM plugin_state ORDER BY id",
    ),
    (
        "plugin_agent_sessions",
        "SELECT session_id, scope_type, canonical_owner_person_id, canonical_space_id "
        "FROM plugin_agent_sessions ORDER BY session_id",
    ),
    (
        "plugin_agent_messages",
        "SELECT id, role, canonical_sender_person_id FROM plugin_agent_messages ORDER BY id",
    ),
    (
        "plugin_background_target_grants",
        "SELECT id, enabled, canonical_target_person_id, canonical_target_space_id, "
        "canonical_created_by_person_id, canonical_presence_id "
        "FROM plugin_background_target_grants ORDER BY id",
    ),
    (
        "plugin_notification_outbox",
        "SELECT id, status, canonical_target_person_id, canonical_target_space_id, "
        "canonical_conversation_id, canonical_presence_id "
        "FROM plugin_notification_outbox ORDER BY id",
    ),
    (
        "plugin_background_turn_jobs",
        "SELECT id, status, canonical_target_person_id, canonical_target_space_id, "
        "canonical_conversation_id, canonical_presence_id "
        "FROM plugin_background_turn_jobs ORDER BY id",
    ),
)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _columns_ready(connection: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> bool:
    if not _table_exists(connection, table):
        return False
    return all(_column_exists(connection, table, column) for column in columns)


def _content_digest(raw: object) -> str:
    return hashlib.sha256(str(raw or "").encode()).hexdigest()[:16]


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _c23_conflict(*, table: str, pk: object, category: str) -> IdentityConflict:
    conflict_kind: Literal["ambiguous_identity", "unclassified"] = (
        "unclassified" if category == MISSING_OWNER else "ambiguous_identity"
    )
    external_id = f"c23:{table}:{pk}"
    return IdentityConflict(
        subject_kind="account",
        conflict_kind=conflict_kind,
        error_category=category,  # type: ignore[arg-type]
        platform=IDENTITY_PLATFORM,
        external_id=external_id,
        fingerprint=fingerprint_external_id(external_id),
    )


def c23_signature_chunks(connection: sqlite3.Connection) -> list[str]:
    chunks: list[str] = []
    for table, sql in C23_SIGNATURE_SQL:
        if not _table_exists(connection, table):
            continue
        chunks.append(sql)
        chunks.extend(str(tuple(row)) for row in connection.execute(sql))
    return chunks


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


def _presence_overlay(
    connection: sqlite3.Connection,
    planned_presence_bindings: Mapping[str, str],
) -> tuple[dict[str, str], frozenset[str]]:
    current = _binding_map(connection, "presences", "external_account_id", "id")
    merged, ambiguous = _merge_owner_maps(current, planned_presence_bindings)
    return merged, frozenset(ambiguous)


def _existing_ids(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    if not _table_exists(connection, table):
        return frozenset()
    return frozenset(str(row[0]) for row in connection.execute(f'SELECT id FROM "{table}"'))


def _conversation_lookup_fingerprints(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    """Hash kind/owner/existence/uniqueness used by `_lookup_conversation`."""

    if not _columns_ready(
        connection, "canonical_conversations", ("id", "kind", "person_id", "space_id")
    ):
        return (("conversation_lookup", "missing", 0),)
    grouped: dict[tuple[str, str, str], list[str]] = {}
    for row in connection.execute(
        "SELECT id, kind, person_id, space_id FROM canonical_conversations ORDER BY id"
    ):
        key = (
            str(row["kind"] or ""),
            _text(row["person_id"]) or "",
            _text(row["space_id"]) or "",
        )
        grouped.setdefault(key, []).append(str(row["id"]))
    if not grouped:
        return (("conversation_lookup", "empty", 0),)
    rows: list[tuple[object, ...]] = []
    for kind, person_id, space_id in sorted(grouped):
        ids = grouped[(kind, person_id, space_id)]
        unique = _content_digest(ids[0]) if len(ids) == 1 else "ambiguous"
        rows.append(("conversation_lookup", kind, person_id, space_id, len(ids), unique))
    return tuple(rows)


def _plugin_source_event_ids(connection: sqlite3.Connection) -> tuple[int, ...]:
    ids: set[int] = set()
    for table in ("plugin_notification_outbox", "plugin_background_turn_jobs"):
        if not _columns_ready(connection, table, ("id", "source_event_id")):
            continue
        for row in connection.execute(
            f"SELECT source_event_id FROM {table} WHERE source_event_id IS NOT NULL ORDER BY id"
        ):
            try:
                ids.add(int(str(row[0])))
            except (TypeError, ValueError):
                continue
    return tuple(sorted(ids))


def _event_link_tuple(connection: sqlite3.Connection, event_id: int) -> tuple[object, ...]:
    digest = _content_digest(str(event_id))
    if not _columns_ready(
        connection,
        "chat_events",
        (
            "id",
            "scope_type",
            "group_id",
            "private_peer_user_id",
            "sender_user_id",
            "direction",
            "canonical_conversation_id",
        ),
    ):
        return ("plugin_event_links", digest, "missing-table")
    event = connection.execute(
        "SELECT scope_type, group_id, private_peer_user_id, sender_user_id, "
        "direction, canonical_conversation_id FROM chat_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if event is None:
        return ("plugin_event_links", digest, "absent")
    return (
        "plugin_event_links",
        digest,
        str(event["scope_type"] or ""),
        str(event["direction"] or ""),
        _text(event["canonical_conversation_id"]) or "",
        fingerprint_external_id(str(event["group_id"] or "")),
        fingerprint_external_id(str(event["private_peer_user_id"] or "")),
        fingerprint_external_id(str(event["sender_user_id"] or "")),
    )


def _plugin_event_link_fingerprints(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    event_ids = _plugin_source_event_ids(connection)
    if not event_ids:
        return (("plugin_event_links", "empty"),)
    return tuple(_event_link_tuple(connection, event_id) for event_id in event_ids)


def _queued_event_inputs(connection: sqlite3.Connection, event_id: object) -> tuple[str, str]:
    if event_id is None:
        return "", _content_digest("")
    digest = _content_digest(str(event_id))
    if not _columns_ready(connection, "chat_events", ("id", "canonical_conversation_id")):
        return "", digest
    try:
        pk = int(str(event_id))
    except (TypeError, ValueError):
        return "", digest
    event = connection.execute(
        "SELECT canonical_conversation_id FROM chat_events WHERE id = ?",
        (pk,),
    ).fetchone()
    if event is None:
        return "", digest
    return _text(event["canonical_conversation_id"]) or "", digest


def c23_source_material(
    connection: sqlite3.Connection,
    ctx: C21OwnerContext,
    *,
    presence_bindings: Mapping[str, str] | None = None,
    row_fingerprints: tuple[tuple[object, ...], ...] = (),
) -> tuple[tuple[object, ...], ...]:
    rows: list[tuple[object, ...]] = []
    rows.extend(row_fingerprints)
    rows.extend(_conversation_lookup_fingerprints(connection))
    rows.extend(_plugin_event_link_fingerprints(connection))
    existing_owners = _existing_canonical_owner_ids(connection)
    existing_presences = _existing_ids(connection, "presences")
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
    rows.append(
        (
            "merged_presence_bindings",
            *_plan_input_pairs(dict(presence_bindings or {}), existing_presences),
        )
    )
    rows.append(("non_person_accounts", *sorted(ctx.non_person_accounts)))
    rows.append(("ambiguous_externals", *sorted(ctx.ambiguous_externals)))
    return tuple(rows)


def _resolve_person(ctx: C21OwnerContext, raw: object) -> tuple[str | None, str | None]:
    external = normalize_external_id(raw)
    if external is None:
        return None, None
    if external in ctx.ambiguous_externals:
        return None, AMBIGUOUS_OWNER
    if external in ctx.non_person_accounts:
        return None, None
    owner = ctx.person_bindings.get(external)
    if owner is None:
        return None, MISSING_OWNER
    return owner, None


def _resolve_space(ctx: C21OwnerContext, raw: object) -> tuple[str | None, str | None]:
    external = normalize_external_id(raw)
    if external is None:
        return None, MISSING_OWNER
    if external in ctx.ambiguous_externals:
        return None, AMBIGUOUS_OWNER
    owner = ctx.space_bindings.get(external)
    if owner is None:
        return None, MISSING_OWNER
    return owner, None


def _resolve_presence(
    presence_bindings: Mapping[str, str],
    ambiguous: frozenset[str],
    raw: object,
) -> tuple[str | None, str | None]:
    external = normalize_external_id(raw)
    if external is None:
        return None, None
    if external in ambiguous:
        return None, AMBIGUOUS_OWNER
    owner = presence_bindings.get(external)
    return owner, None


def _decide_optional(
    current: object,
    resolved: str | None,
    error: str | None,
    *,
    allow_null: bool,
    required: bool,
) -> tuple[str, str | None, str | None]:
    current_id = _text(current)
    if error is not None:
        return "conflict", None, error
    if required and resolved is None:
        return "conflict", None, MISSING_OWNER
    if current_id is None:
        if resolved is None:
            return "keep", None, None
        return "fill", resolved, None
    if resolved is None:
        if allow_null:
            return "conflict", None, AMBIGUOUS_OWNER
        return "conflict", None, MISSING_OWNER
    if current_id == resolved:
        return "keep", resolved, None
    return "conflict", None, AMBIGUOUS_OWNER


def _shadow(
    table: str,
    row_key: tuple[tuple[str, object], ...],
    column: str,
    value: str,
    current: object,
) -> ShadowAssignment:
    return ShadowAssignment(
        table=table,
        row_key=row_key,
        column=column,
        value=value,
        current=_text(current),
    )


def _record_decision(
    assignments: list[ShadowAssignment],
    conflicts: list[IdentityConflict],
    *,
    table: str,
    pk: object,
    row_key: tuple[tuple[str, object], ...],
    column: str,
    current: object,
    resolved: str | None,
    error: str | None,
    allow_null: bool,
    required: bool,
) -> None:
    action, winner, decide_error = _decide_optional(
        current,
        resolved,
        error,
        allow_null=allow_null,
        required=required,
    )
    if action == "conflict" or decide_error is not None:
        conflicts.append(_c23_conflict(table=table, pk=pk, category=decide_error or MISSING_OWNER))
        return
    if action == "fill" and winner is not None:
        assignments.append(_shadow(table, row_key, column, winner, current))


def _conversation_exists(connection: sqlite3.Connection, conversation_id: object) -> bool:
    if not conversation_id or not _table_exists(connection, "canonical_conversations"):
        return False
    row = connection.execute(
        "SELECT 1 FROM canonical_conversations WHERE id = ?",
        (str(conversation_id),),
    ).fetchone()
    return row is not None


def _conversation_owner(
    connection: sqlite3.Connection, conversation_id: object
) -> tuple[str | None, str | None] | None:
    if not conversation_id or not _table_exists(connection, "canonical_conversations"):
        return None
    row = connection.execute(
        "SELECT person_id, space_id FROM canonical_conversations WHERE id = ?",
        (str(conversation_id),),
    ).fetchone()
    if row is None:
        return None
    return _xor_pair(row["person_id"], row["space_id"])


def _lookup_conversation(
    connection: sqlite3.Connection, owner: tuple[str | None, str | None]
) -> tuple[str | None, str | None]:
    if not _owner_complete(owner) or not _table_exists(connection, "canonical_conversations"):
        return None, None
    person_id, space_id = owner
    if person_id:
        rows = list(
            connection.execute(
                "SELECT id FROM canonical_conversations WHERE kind = 'private' AND person_id = ? "
                "ORDER BY id",
                (person_id,),
            )
        )
    else:
        rows = list(
            connection.execute(
                "SELECT id FROM canonical_conversations WHERE kind = 'space' AND space_id = ? "
                "ORDER BY id",
                (space_id,),
            )
        )
    if not rows:
        return None, None
    if len(rows) != 1:
        return None, AMBIGUOUS_OWNER
    return str(rows[0][0]), None


def _resolve_conversation(
    connection: sqlite3.Connection,
    *,
    event_id: object,
    target: tuple[str | None, str | None],
    required: bool,
) -> tuple[str | None, str | None]:
    if event_id is None:
        return (None, MISSING_OWNER) if required else (None, None)
    if not _table_exists(connection, "chat_events"):
        return (None, MISSING_OWNER) if required else (None, None)
    event = connection.execute(
        "SELECT canonical_conversation_id FROM chat_events WHERE id = ?",
        (int(str(event_id)),),
    ).fetchone()
    if event is None:
        return (None, MISSING_OWNER) if required else (None, None)
    inherited = _text(event["canonical_conversation_id"])
    if inherited is not None:
        owner = _conversation_owner(connection, inherited)
        if owner is None:
            return (None, MISSING_OWNER) if required else (None, None)
        if owner != target:
            return (None, AMBIGUOUS_OWNER) if required else (None, None)
        return inherited, None
    looked_up, lookup_error = _lookup_conversation(connection, target)
    if lookup_error is not None:
        return None, lookup_error
    if looked_up is None and required:
        return None, MISSING_OWNER
    return looked_up, None


def _load_sessions(
    connection: sqlite3.Connection,
) -> dict[str, sqlite3.Row]:
    if not _columns_ready(
        connection,
        "plugin_agent_sessions",
        (
            "session_id",
            "owner_user_id",
            "scope_type",
            "scope_id",
            "status",
            "canonical_owner_person_id",
            "canonical_space_id",
        ),
    ):
        return {}
    return {
        str(row["session_id"]): row
        for row in connection.execute(
            "SELECT session_id, owner_user_id, scope_type, scope_id, status, "
            "canonical_owner_person_id, canonical_space_id FROM plugin_agent_sessions "
            "ORDER BY session_id"
        )
    }


def _fingerprint_scope_row(
    table: str,
    pk: object,
    *,
    kind: str,
    status: object,
    current_person: object,
    current_space: object,
    extra: tuple[object, ...] = (),
    content_hash: str,
) -> tuple[object, ...]:
    return (
        table,
        pk,
        kind,
        _text(status),
        _text(current_person),
        _text(current_space),
        content_hash,
        *extra,
    )


def _plan_config(
    connection: sqlite3.Connection,
    ctx: C21OwnerContext,
    assignments: list[ShadowAssignment],
    conflicts: list[IdentityConflict],
    fingerprints: list[tuple[object, ...]],
) -> None:
    if not _columns_ready(
        connection,
        "plugin_config_values",
        (
            "id",
            "scope_type",
            "scope_id",
            "value_json",
            "canonical_person_id",
            "canonical_space_id",
        ),
    ):
        return
    for row in connection.execute(
        "SELECT id, scope_type, scope_id, value_json, canonical_person_id, "
        "canonical_space_id FROM plugin_config_values ORDER BY id"
    ):
        pk = int(row["id"])
        key = (("id", pk),)
        scope = str(row["scope_type"] or "")
        fingerprints.append(
            _fingerprint_scope_row(
                "plugin_config_values",
                pk,
                kind=scope,
                status=scope,
                current_person=row["canonical_person_id"],
                current_space=row["canonical_space_id"],
                extra=(fingerprint_external_id(str(row["scope_id"] or "")),),
                content_hash=_content_digest(row["value_json"]),
            )
        )
        if scope == "global":
            if row["canonical_person_id"] or row["canonical_space_id"]:
                conflicts.append(
                    _c23_conflict(table="plugin_config_values", pk=pk, category=AMBIGUOUS_OWNER)
                )
            continue
        if scope == "user":
            person_id, error = _resolve_person(ctx, row["scope_id"])
            _record_decision(
                assignments,
                conflicts,
                table="plugin_config_values",
                pk=pk,
                row_key=key,
                column="canonical_person_id",
                current=row["canonical_person_id"],
                resolved=person_id,
                error=error,
                allow_null=False,
                required=True,
            )
            if row["canonical_space_id"]:
                conflicts.append(
                    _c23_conflict(table="plugin_config_values", pk=pk, category=AMBIGUOUS_OWNER)
                )
            continue
        if scope == "group":
            space_id, error = _resolve_space(ctx, row["scope_id"])
            _record_decision(
                assignments,
                conflicts,
                table="plugin_config_values",
                pk=pk,
                row_key=key,
                column="canonical_space_id",
                current=row["canonical_space_id"],
                resolved=space_id,
                error=error,
                allow_null=False,
                required=True,
            )
            if row["canonical_person_id"]:
                conflicts.append(
                    _c23_conflict(table="plugin_config_values", pk=pk, category=AMBIGUOUS_OWNER)
                )
            continue
        conflicts.append(_c23_conflict(table="plugin_config_values", pk=pk, category=MISSING_OWNER))


def _plan_state(
    connection: sqlite3.Connection,
    ctx: C21OwnerContext,
    assignments: list[ShadowAssignment],
    conflicts: list[IdentityConflict],
    fingerprints: list[tuple[object, ...]],
) -> None:
    if not _columns_ready(
        connection,
        "plugin_state",
        ("id", "subject_user_id", "value_json", "canonical_person_id"),
    ):
        return
    for row in connection.execute(
        "SELECT id, subject_user_id, value_json, canonical_person_id FROM plugin_state ORDER BY id"
    ):
        pk = int(row["id"])
        key = (("id", pk),)
        subject = normalize_external_id(row["subject_user_id"])
        fingerprints.append(
            _fingerprint_scope_row(
                "plugin_state",
                pk,
                kind="subject" if subject else "global",
                status=None,
                current_person=row["canonical_person_id"],
                current_space=None,
                extra=(fingerprint_external_id(subject) if subject else "",),
                content_hash=_content_digest(row["value_json"]),
            )
        )
        if subject is None:
            if row["canonical_person_id"]:
                conflicts.append(
                    _c23_conflict(table="plugin_state", pk=pk, category=AMBIGUOUS_OWNER)
                )
            continue
        if subject in ctx.non_person_accounts:
            if row["canonical_person_id"]:
                conflicts.append(
                    _c23_conflict(table="plugin_state", pk=pk, category=AMBIGUOUS_OWNER)
                )
            continue
        person_id, error = _resolve_person(ctx, subject)
        _record_decision(
            assignments,
            conflicts,
            table="plugin_state",
            pk=pk,
            row_key=key,
            column="canonical_person_id",
            current=row["canonical_person_id"],
            resolved=person_id,
            error=error,
            allow_null=False,
            required=True,
        )


def _plan_sessions(
    connection: sqlite3.Connection,
    ctx: C21OwnerContext,
    assignments: list[ShadowAssignment],
    conflicts: list[IdentityConflict],
    fingerprints: list[tuple[object, ...]],
    sessions: Mapping[str, sqlite3.Row],
) -> dict[str, str | None]:
    owners: dict[str, str | None] = {}
    for session_id, row in sessions.items():
        scope = str(row["scope_type"] or "")
        fingerprints.append(
            _fingerprint_scope_row(
                "plugin_agent_sessions",
                session_id,
                kind=scope,
                status=row["status"],
                current_person=row["canonical_owner_person_id"],
                current_space=row["canonical_space_id"],
                extra=(
                    fingerprint_external_id(str(row["owner_user_id"] or "")),
                    fingerprint_external_id(str(row["scope_id"] or "")),
                ),
                content_hash=_content_digest(row["status"]),
            )
        )
        key = (("session_id", session_id),)
        if scope == "plugin":
            owners[session_id] = None
            if row["canonical_owner_person_id"] or row["canonical_space_id"]:
                conflicts.append(
                    _c23_conflict(
                        table="plugin_agent_sessions", pk=session_id, category=AMBIGUOUS_OWNER
                    )
                )
            continue
        if scope == "user":
            person_id, error = _resolve_person(ctx, row["owner_user_id"] or row["scope_id"])
            owners[session_id] = person_id
            _record_decision(
                assignments,
                conflicts,
                table="plugin_agent_sessions",
                pk=session_id,
                row_key=key,
                column="canonical_owner_person_id",
                current=row["canonical_owner_person_id"],
                resolved=person_id,
                error=error,
                allow_null=False,
                required=True,
            )
            if row["canonical_space_id"]:
                conflicts.append(
                    _c23_conflict(
                        table="plugin_agent_sessions", pk=session_id, category=AMBIGUOUS_OWNER
                    )
                )
            continue
        if scope == "group":
            space_id, space_error = _resolve_space(ctx, row["scope_id"])
            _record_decision(
                assignments,
                conflicts,
                table="plugin_agent_sessions",
                pk=session_id,
                row_key=key,
                column="canonical_space_id",
                current=row["canonical_space_id"],
                resolved=space_id,
                error=space_error,
                allow_null=False,
                required=True,
            )
            owner_raw = row["owner_user_id"]
            if owner_raw:
                person_id, person_error = _resolve_person(ctx, owner_raw)
                owners[session_id] = person_id
                _record_decision(
                    assignments,
                    conflicts,
                    table="plugin_agent_sessions",
                    pk=session_id,
                    row_key=key,
                    column="canonical_owner_person_id",
                    current=row["canonical_owner_person_id"],
                    resolved=person_id,
                    error=person_error,
                    allow_null=False,
                    required=True,
                )
            else:
                owners[session_id] = _text(row["canonical_owner_person_id"])
                if row["canonical_owner_person_id"]:
                    conflicts.append(
                        _c23_conflict(
                            table="plugin_agent_sessions", pk=session_id, category=AMBIGUOUS_OWNER
                        )
                    )
            continue
        owners[session_id] = None
        conflicts.append(
            _c23_conflict(table="plugin_agent_sessions", pk=session_id, category=MISSING_OWNER)
        )
    return owners


def _plan_messages(
    connection: sqlite3.Connection,
    ctx: C21OwnerContext,
    assignments: list[ShadowAssignment],
    conflicts: list[IdentityConflict],
    fingerprints: list[tuple[object, ...]],
    sessions: Mapping[str, sqlite3.Row],
    session_owners: Mapping[str, str | None],
) -> None:
    if not _columns_ready(
        connection,
        "plugin_agent_messages",
        ("id", "session_id", "role", "sender_user_id", "content", "canonical_sender_person_id"),
    ):
        return
    for row in connection.execute(
        "SELECT id, session_id, role, sender_user_id, content, canonical_sender_person_id "
        "FROM plugin_agent_messages ORDER BY id"
    ):
        pk = int(row["id"])
        role = str(row["role"] or "")
        session_id = str(row["session_id"] or "")
        parent = sessions.get(session_id)
        parent_owner = session_owners.get(session_id)
        fingerprints.append(
            (
                "plugin_agent_messages",
                pk,
                role,
                _text(row["canonical_sender_person_id"]),
                fingerprint_external_id(session_id),
                str(parent["scope_type"] or "") if parent is not None else "",
                _text(parent["canonical_owner_person_id"]) if parent is not None else None,
                _text(parent["canonical_space_id"]) if parent is not None else None,
                fingerprint_external_id(str(parent["owner_user_id"] or ""))
                if parent is not None
                else "",
                fingerprint_external_id(str(parent["scope_id"] or ""))
                if parent is not None
                else "",
                fingerprint_external_id(str(row["sender_user_id"] or "")),
                _content_digest(row["content"]),
            )
        )
        current = row["canonical_sender_person_id"]
        if role != _USER_MESSAGE_ROLE:
            if current:
                conflicts.append(
                    _c23_conflict(table="plugin_agent_messages", pk=pk, category=AMBIGUOUS_OWNER)
                )
            continue
        if parent is not None and str(parent["scope_type"] or "") == "plugin":
            if current:
                conflicts.append(
                    _c23_conflict(table="plugin_agent_messages", pk=pk, category=AMBIGUOUS_OWNER)
                )
            continue
        sender = normalize_external_id(row["sender_user_id"])
        sender_person, sender_error = _resolve_person(ctx, sender) if sender else (None, None)
        if sender is not None and sender in ctx.non_person_accounts:
            if current:
                conflicts.append(
                    _c23_conflict(table="plugin_agent_messages", pk=pk, category=AMBIGUOUS_OWNER)
                )
            continue
        if sender_error is not None:
            conflicts.append(
                _c23_conflict(table="plugin_agent_messages", pk=pk, category=sender_error)
            )
            continue
        if parent_owner and sender_person and parent_owner != sender_person:
            conflicts.append(
                _c23_conflict(table="plugin_agent_messages", pk=pk, category=AMBIGUOUS_OWNER)
            )
            continue
        resolved = parent_owner or sender_person
        _record_decision(
            assignments,
            conflicts,
            table="plugin_agent_messages",
            pk=pk,
            row_key=(("id", pk),),
            column="canonical_sender_person_id",
            current=current,
            resolved=resolved,
            error=None,
            allow_null=resolved is None,
            required=resolved is not None or bool(sender) or bool(parent_owner),
        )


def _plan_grant_or_queued_target(
    ctx: C21OwnerContext,
    *,
    target_type: str,
    target_id: object,
) -> tuple[tuple[str | None, str | None] | None, str | None]:
    if target_type == "private":
        person_id, error = _resolve_person(ctx, target_id)
        if error is not None:
            return None, error
        if person_id is None:
            return None, MISSING_OWNER
        return (person_id, None), None
    if target_type == "group":
        space_id, error = _resolve_space(ctx, target_id)
        if error is not None:
            return None, error
        if space_id is None:
            return None, MISSING_OWNER
        return (None, space_id), None
    return None, MISSING_OWNER


def _plan_grants(
    connection: sqlite3.Connection,
    ctx: C21OwnerContext,
    presence_bindings: Mapping[str, str],
    presence_ambiguous: frozenset[str],
    assignments: list[ShadowAssignment],
    conflicts: list[IdentityConflict],
    fingerprints: list[tuple[object, ...]],
) -> None:
    if not _columns_ready(
        connection,
        "plugin_background_target_grants",
        (
            "id",
            "plugin_id",
            "target_type",
            "target_id",
            "bot_user_id",
            "enabled",
            "created_by_user_id",
            "canonical_target_person_id",
            "canonical_target_space_id",
            "canonical_created_by_person_id",
            "canonical_presence_id",
        ),
    ):
        return
    for row in connection.execute(
        "SELECT id, plugin_id, target_type, target_id, bot_user_id, enabled, "
        "created_by_user_id, canonical_target_person_id, canonical_target_space_id, "
        "canonical_created_by_person_id, canonical_presence_id "
        "FROM plugin_background_target_grants ORDER BY id"
    ):
        pk = int(row["id"])
        enabled = bool(row["enabled"])
        current = _xor_pair(row["canonical_target_person_id"], row["canonical_target_space_id"])
        fingerprints.append(
            (
                "plugin_background_target_grants",
                pk,
                str(row["target_type"] or ""),
                int(enabled),
                current,
                _text(row["canonical_created_by_person_id"]),
                _text(row["canonical_presence_id"]),
                fingerprint_external_id(str(row["target_id"] or "")),
                fingerprint_external_id(str(row["created_by_user_id"] or "")),
                fingerprint_external_id(str(row["bot_user_id"] or "")),
            )
        )
        key = (("id", pk),)
        creator_id, creator_error = _resolve_person(ctx, row["created_by_user_id"])
        _record_decision(
            assignments,
            conflicts,
            table="plugin_background_target_grants",
            pk=pk,
            row_key=key,
            column="canonical_created_by_person_id",
            current=row["canonical_created_by_person_id"],
            resolved=creator_id,
            error=creator_error,
            allow_null=not enabled,
            required=enabled,
        )
        target, target_error = _plan_grant_or_queued_target(
            ctx,
            target_type=str(row["target_type"] or ""),
            target_id=row["target_id"],
        )
        if current[0] and current[1]:
            conflicts.append(
                _c23_conflict(
                    table="plugin_background_target_grants", pk=pk, category=AMBIGUOUS_OWNER
                )
            )
        elif target_error is not None or target is None:
            if enabled or current != (None, None):
                conflicts.append(
                    _c23_conflict(
                        table="plugin_background_target_grants",
                        pk=pk,
                        category=target_error or MISSING_OWNER,
                    )
                )
        elif current == (None, None):
            column = "canonical_target_person_id" if target[0] else "canonical_target_space_id"
            winner = target[0] or target[1]
            if winner:
                assignments.append(
                    _shadow("plugin_background_target_grants", key, column, winner, None)
                )
        elif current != target:
            conflicts.append(
                _c23_conflict(
                    table="plugin_background_target_grants", pk=pk, category=AMBIGUOUS_OWNER
                )
            )
        presence_id, presence_error = _resolve_presence(
            presence_bindings, presence_ambiguous, row["bot_user_id"]
        )
        _record_decision(
            assignments,
            conflicts,
            table="plugin_background_target_grants",
            pk=pk,
            row_key=key,
            column="canonical_presence_id",
            current=row["canonical_presence_id"],
            resolved=presence_id,
            error=presence_error,
            allow_null=True,
            required=False,
        )


def _plan_queued(
    connection: sqlite3.Connection,
    ctx: C21OwnerContext,
    presence_bindings: Mapping[str, str],
    presence_ambiguous: frozenset[str],
    assignments: list[ShadowAssignment],
    conflicts: list[IdentityConflict],
    fingerprints: list[tuple[object, ...]],
    *,
    table: str,
    live_statuses: frozenset[str],
    content_column: str,
) -> None:
    required = (
        "id",
        "source_event_id",
        "plugin_id",
        "target_type",
        "target_id",
        "bot_user_id",
        "status",
        "canonical_target_person_id",
        "canonical_target_space_id",
        "canonical_conversation_id",
        "canonical_presence_id",
        content_column,
    )
    if not _columns_ready(connection, table, required):
        return
    for row in connection.execute(
        f"SELECT id, source_event_id, plugin_id, target_type, target_id, bot_user_id, "
        f"status, {content_column}, canonical_target_person_id, canonical_target_space_id, "
        f"canonical_conversation_id, canonical_presence_id FROM {table} ORDER BY id"
    ):
        pk = int(row["id"])
        status = str(row["status"] or "")
        live = status in live_statuses
        current = _xor_pair(row["canonical_target_person_id"], row["canonical_target_space_id"])
        event_conversation, event_digest = _queued_event_inputs(connection, row["source_event_id"])
        fingerprints.append(
            (
                table,
                pk,
                str(row["target_type"] or ""),
                status,
                current,
                _text(row["canonical_conversation_id"]),
                _text(row["canonical_presence_id"]),
                fingerprint_external_id(str(row["target_id"] or "")),
                fingerprint_external_id(str(row["bot_user_id"] or "")),
                event_conversation,
                event_digest,
                _content_digest(row[content_column]),
            )
        )
        key = (("id", pk),)
        target, target_error = _plan_grant_or_queued_target(
            ctx,
            target_type=str(row["target_type"] or ""),
            target_id=row["target_id"],
        )
        if current[0] and current[1]:
            conflicts.append(_c23_conflict(table=table, pk=pk, category=AMBIGUOUS_OWNER))
            target = None
        elif target_error is not None or target is None:
            if live or current != (None, None):
                conflicts.append(
                    _c23_conflict(table=table, pk=pk, category=target_error or MISSING_OWNER)
                )
            target = None
        elif current == (None, None):
            if target[0]:
                assignments.append(
                    _shadow(table, key, "canonical_target_person_id", target[0], current[0])
                )
            elif target[1]:
                assignments.append(
                    _shadow(table, key, "canonical_target_space_id", target[1], current[1])
                )
        elif current != target:
            conflicts.append(_c23_conflict(table=table, pk=pk, category=AMBIGUOUS_OWNER))
            target = None
        resolved_target = current if current != (None, None) and current == target else target
        conversation_id, conversation_error = _resolve_conversation(
            connection,
            event_id=row["source_event_id"],
            target=resolved_target or (None, None),
            required=live and resolved_target is not None,
        )
        current_conversation = _text(row["canonical_conversation_id"])
        if (
            conversation_id is None
            and conversation_error is None
            and current_conversation
            and resolved_target is not None
        ):
            owner = _conversation_owner(connection, current_conversation)
            if owner == resolved_target:
                conversation_id = current_conversation
            elif owner is not None:
                conversation_error = AMBIGUOUS_OWNER
        _record_decision(
            assignments,
            conflicts,
            table=table,
            pk=pk,
            row_key=key,
            column="canonical_conversation_id",
            current=row["canonical_conversation_id"],
            resolved=conversation_id,
            error=conversation_error,
            allow_null=not live,
            required=live,
        )
        presence_id, presence_error = _resolve_presence(
            presence_bindings, presence_ambiguous, row["bot_user_id"]
        )
        _record_decision(
            assignments,
            conflicts,
            table=table,
            pk=pk,
            row_key=key,
            column="canonical_presence_id",
            current=row["canonical_presence_id"],
            resolved=presence_id,
            error=presence_error,
            allow_null=True,
            required=False,
        )


def plan_c23_plugin_owners(
    connection: sqlite3.Connection,
    *,
    planned_person_bindings: Mapping[str, str] | None = None,
    planned_space_bindings: Mapping[str, str] | None = None,
    planned_presence_bindings: Mapping[str, str] | None = None,
    non_person_accounts: frozenset[str] = frozenset(),
) -> tuple[
    tuple[ShadowAssignment, ...],
    tuple[IdentityConflict, ...],
    int,
    tuple[tuple[object, ...], ...],
]:
    """Plan C23 plugin shadow fills. Zero writes on conflict."""

    assignments: list[ShadowAssignment] = []
    conflicts: list[IdentityConflict] = []
    fingerprints: list[tuple[object, ...]] = []
    ctx = overlay_owner_bindings(
        connection,
        planned_person_bindings=planned_person_bindings or {},
        planned_space_bindings=planned_space_bindings or {},
        non_person_accounts=non_person_accounts,
    )
    presence_bindings, presence_ambiguous = _presence_overlay(
        connection, planned_presence_bindings or {}
    )
    _plan_config(connection, ctx, assignments, conflicts, fingerprints)
    _plan_state(connection, ctx, assignments, conflicts, fingerprints)
    sessions = _load_sessions(connection)
    session_owners = _plan_sessions(connection, ctx, assignments, conflicts, fingerprints, sessions)
    _plan_messages(connection, ctx, assignments, conflicts, fingerprints, sessions, session_owners)
    _plan_grants(
        connection,
        ctx,
        presence_bindings,
        presence_ambiguous,
        assignments,
        conflicts,
        fingerprints,
    )
    _plan_queued(
        connection,
        ctx,
        presence_bindings,
        presence_ambiguous,
        assignments,
        conflicts,
        fingerprints,
        table="plugin_notification_outbox",
        live_statuses=LIVE_OUTBOX_STATUSES,
        content_column="text",
    )
    _plan_queued(
        connection,
        ctx,
        presence_bindings,
        presence_ambiguous,
        assignments,
        conflicts,
        fingerprints,
        table="plugin_background_turn_jobs",
        live_statuses=LIVE_JOB_STATUSES,
        content_column="agent_intent",
    )
    assignments.sort(key=lambda item: (item.table, item.column, item.row_key, item.value))
    conflicts.sort(key=lambda item: (item.external_id, item.error_category))
    filled = sum(1 for item in assignments if item.current is None)
    return (
        tuple(assignments),
        tuple(conflicts),
        filled,
        c23_source_material(
            connection,
            ctx,
            presence_bindings=presence_bindings,
            row_fingerprints=tuple(fingerprints),
        ),
    )


def _require_live(
    value: object,
    pool: frozenset[str],
    *,
    required: bool,
) -> None:
    text = _text(value)
    if text is None:
        if required:
            raise IdentityCutoverPreconditionError(C23_PLUGIN_INCOMPLETE)
        return
    if text not in pool:
        raise IdentityCutoverPreconditionError(C23_PLUGIN_INCOMPLETE)


def require_c23_plugin_targets(connection: sqlite3.Connection) -> None:
    """Fail closed before flip: live plugin grants/outbox/jobs must be complete."""

    persons = _existing_ids(connection, "persons")
    spaces = _existing_ids(connection, "spaces")
    presences = _existing_ids(connection, "presences")
    conversations = _existing_ids(connection, "canonical_conversations")
    if _columns_ready(
        connection,
        "plugin_background_target_grants",
        (
            "id",
            "enabled",
            "canonical_target_person_id",
            "canonical_target_space_id",
            "canonical_created_by_person_id",
            "canonical_presence_id",
        ),
    ):
        for row in connection.execute(
            "SELECT enabled, canonical_target_person_id, canonical_target_space_id, "
            "canonical_created_by_person_id, canonical_presence_id "
            "FROM plugin_background_target_grants ORDER BY id"
        ):
            pair = _xor_pair(row["canonical_target_person_id"], row["canonical_target_space_id"])
            if pair[0] and pair[1]:
                raise IdentityCutoverPreconditionError(C23_PLUGIN_INCOMPLETE)
            _require_live(pair[0], persons, required=False)
            _require_live(pair[1], spaces, required=False)
            _require_live(row["canonical_presence_id"], presences, required=False)
            live = bool(row["enabled"])
            if live:
                _require_live(row["canonical_created_by_person_id"], persons, required=True)
                if not _owner_complete(pair):
                    raise IdentityCutoverPreconditionError(C23_PLUGIN_INCOMPLETE)
            elif row["canonical_created_by_person_id"]:
                _require_live(row["canonical_created_by_person_id"], persons, required=True)
    for table, live_statuses in (
        ("plugin_notification_outbox", LIVE_OUTBOX_STATUSES),
        ("plugin_background_turn_jobs", LIVE_JOB_STATUSES),
    ):
        if not _columns_ready(
            connection,
            table,
            (
                "id",
                "status",
                "canonical_target_person_id",
                "canonical_target_space_id",
                "canonical_conversation_id",
                "canonical_presence_id",
            ),
        ):
            continue
        for row in connection.execute(
            f"SELECT status, canonical_target_person_id, canonical_target_space_id, "
            f"canonical_conversation_id, canonical_presence_id FROM {table} ORDER BY id"
        ):
            pair = _xor_pair(row["canonical_target_person_id"], row["canonical_target_space_id"])
            if pair[0] and pair[1]:
                raise IdentityCutoverPreconditionError(C23_PLUGIN_INCOMPLETE)
            _require_live(pair[0], persons, required=False)
            _require_live(pair[1], spaces, required=False)
            _require_live(row["canonical_presence_id"], presences, required=False)
            conversation = _text(row["canonical_conversation_id"])
            if conversation is not None:
                if conversation not in conversations or not _conversation_exists(
                    connection, conversation
                ):
                    raise IdentityCutoverPreconditionError(C23_PLUGIN_INCOMPLETE)
                owner = _conversation_owner(connection, conversation)
                if owner is None or owner != pair:
                    raise IdentityCutoverPreconditionError(C23_PLUGIN_INCOMPLETE)
            live = str(row["status"] or "") in live_statuses
            if live:
                if not _owner_complete(pair):
                    raise IdentityCutoverPreconditionError(C23_PLUGIN_INCOMPLETE)
                _require_live(row["canonical_conversation_id"], conversations, required=True)


__all__ = [
    "C23_PLUGIN_INCOMPLETE",
    "LIVE_JOB_STATUSES",
    "LIVE_OUTBOX_STATUSES",
    "c23_signature_chunks",
    "c23_source_material",
    "plan_c23_plugin_owners",
    "require_c23_plugin_targets",
]
