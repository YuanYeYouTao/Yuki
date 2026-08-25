"""C21 evidence: C7 legacy owner proof vs v2 runtime-readable chain.

memory_evidence has no owner columns. C7 backfill proves owner alignment from
planned∪current Binding overlay plus legacy event scope. Missing canonical
Conversation/Event is not a C7 conflict and must not create carriers.

v2 runtime readability requires canonical_event_id + canonical_conversation_id
+ author_kind + live suppression plus fact/conversation owner alignment.
Enqueue watermarks are not a history-read gate.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from qq_ai_bot.persistence.repository_helpers import (
    sql_keeper_event_predicate,
    suppression_is_canonical_live,
)

if TYPE_CHECKING:
    from qq_ai_bot.identity.canonical_memory_owners import C21OwnerContext

C21_EVIDENCE_INCOMPLETE = "c21_evidence_incomplete"
MISSING_OWNER = "missing_owner"
AMBIGUOUS_OWNER = "ambiguous_owner"

_EVENT_SCOPE_COLUMNS = (
    "id",
    "scope_type",
    "group_id",
    "private_peer_user_id",
    "sender_user_id",
    "direction",
    "canonical_conversation_id",
    "canonical_event_id",
    "author_kind",
    "suppression_status",
)


def event_is_v2_live(
    *,
    canonical_event_id: object,
    suppression_status: object,
    canonical_conversation_id: object,
    author_kind: object,
) -> bool:
    if not canonical_event_id or not canonical_conversation_id:
        return False
    if author_kind is None or not str(author_kind).strip():
        return False
    status = None if suppression_status is None else str(suppression_status)
    return suppression_is_canonical_live(status)


def event_suppression_is_hidden(suppression_status: object) -> bool:
    status = None if suppression_status is None else str(suppression_status)
    return not suppression_is_canonical_live(status)


def fact_conversation_aligns(
    *,
    scope_type: str,
    visibility_type: str | None,
    subject_person_id: str | None,
    subject_space_id: str | None,
    visibility_person_id: str | None,
    visibility_space_id: str | None,
    conversation_person_id: str | None,
    conversation_space_id: str | None,
) -> bool:
    """True when a live XOR conversation/event-scope owner may support this fact."""

    scope = str(scope_type or "")
    visibility = visibility_type
    subject_person = subject_person_id or None
    subject_space = subject_space_id or None
    visibility_person = visibility_person_id or None
    visibility_space = visibility_space_id or None
    conv_person = conversation_person_id or None
    conv_space = conversation_space_id or None
    if bool(conv_person) == bool(conv_space):
        return False
    if scope == "person":
        if not subject_person or subject_space or visibility_person or visibility_space:
            return False
        if conv_person is not None:
            return conv_person == subject_person
        return conv_space is not None
    if scope == "group":
        if not subject_space or subject_person or visibility_person or visibility_space:
            return False
        return conv_space == subject_space
    if scope == "person_group":
        if not subject_person or not subject_space or visibility_person or visibility_space:
            return False
        if conv_space != subject_space:
            return False
        return conv_person is None or conv_person == subject_person
    if scope == "self":
        if visibility in {None, "global"}:
            return not any((subject_person, subject_space, visibility_person, visibility_space))
        if visibility == "private":
            if not visibility_person or any((subject_person, subject_space, visibility_space)):
                return False
            return conv_person == visibility_person
        if visibility == "group":
            if not visibility_space or any((subject_person, subject_space, visibility_person)):
                return False
            return conv_space == visibility_space
        return False
    return False


def sql_live_event_predicate(alias: str = "c") -> str:
    return (
        f"{alias}.canonical_event_id IS NOT NULL AND "
        f"{alias}.canonical_conversation_id IS NOT NULL AND "
        f"{alias}.author_kind IS NOT NULL AND "
        f"{sql_keeper_event_predicate(alias)}"
    )


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _row_map(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _as_sqlite_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _load_facts(connection: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    if not _table_exists(connection, "memory_facts"):
        return {}
    required = (
        "id",
        "scope_type",
        "visibility_type",
        "status",
        "canonical_subject_person_id",
        "canonical_subject_space_id",
        "canonical_visibility_person_id",
        "canonical_visibility_space_id",
    )
    if not all(_column_exists(connection, "memory_facts", column) for column in required):
        return {}
    facts: dict[int, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT id, scope_type, visibility_type, status, "
        "canonical_subject_person_id, canonical_subject_space_id, "
        "canonical_visibility_person_id, canonical_visibility_space_id "
        "FROM memory_facts ORDER BY id"
    ):
        facts[int(row["id"])] = _row_map(row)
    return facts


def _conversation_owners(
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
    person = str(row["person_id"]) if row["person_id"] else None
    space = str(row["space_id"]) if row["space_id"] else None
    return person, space


def _aligns_fact_owner(
    fact: Mapping[str, Any],
    person_id: str | None,
    space_id: str | None,
) -> bool:
    return fact_conversation_aligns(
        scope_type=str(fact.get("scope_type") or ""),
        visibility_type=fact.get("visibility_type"),
        subject_person_id=fact.get("canonical_subject_person_id"),
        subject_space_id=fact.get("canonical_subject_space_id"),
        visibility_person_id=fact.get("canonical_visibility_person_id"),
        visibility_space_id=fact.get("canonical_visibility_space_id"),
        conversation_person_id=person_id,
        conversation_space_id=space_id,
    )


def evidence_event_chain_ok(
    connection: sqlite3.Connection,
    fact: Mapping[str, Any],
    event: Mapping[str, Any],
) -> bool:
    if not event_is_v2_live(
        canonical_event_id=event.get("canonical_event_id"),
        suppression_status=event.get("suppression_status"),
        canonical_conversation_id=event.get("canonical_conversation_id"),
        author_kind=event.get("author_kind"),
    ):
        return False
    owners = _conversation_owners(connection, event.get("canonical_conversation_id"))
    if owners is None:
        return False
    return _aligns_fact_owner(fact, owners[0], owners[1])


def _load_event(connection: sqlite3.Connection, event_id: object) -> sqlite3.Row | None:
    parsed = _as_sqlite_int(event_id)
    if parsed is None or not _table_exists(connection, "chat_events"):
        return None
    if not all(
        _column_exists(connection, "chat_events", column) for column in _EVENT_SCOPE_COLUMNS
    ):
        return None
    row = connection.execute(
        "SELECT id, scope_type, group_id, private_peer_user_id, sender_user_id, "
        "direction, canonical_conversation_id, canonical_event_id, author_kind, "
        "suppression_status FROM chat_events WHERE id = ?",
        (parsed,),
    ).fetchone()
    if row is None:
        return None
    if not isinstance(row, sqlite3.Row):
        return None
    return row


def _receipt_trigger_event_id(
    connection: sqlite3.Connection, tool_receipt_id: object
) -> int | None:
    parsed = _as_sqlite_int(tool_receipt_id)
    if parsed is None or not _table_exists(connection, "memory_tool_receipts"):
        return None
    if not _column_exists(connection, "memory_tool_receipts", "trigger_event_id"):
        return None
    receipt = connection.execute(
        "SELECT trigger_event_id FROM memory_tool_receipts WHERE id = ?",
        (parsed,),
    ).fetchone()
    if receipt is None:
        return None
    if not isinstance(receipt, sqlite3.Row):
        return None
    return _as_sqlite_int(receipt["trigger_event_id"])


def _resolve_trigger_event_id(
    connection: sqlite3.Connection,
    *,
    event_id: object | None,
    tool_receipt_id: object | None,
) -> int | None:
    if event_id is not None:
        return _as_sqlite_int(event_id)
    return _receipt_trigger_event_id(connection, tool_receipt_id)


def classify_legacy_evidence_owner(
    connection: sqlite3.Connection,
    fact: Mapping[str, Any],
    ctx: C21OwnerContext,
    *,
    event_id: object | None,
    tool_receipt_id: object | None,
) -> str | None:
    """C7 proof: Binding overlay + legacy event scope. Missing Conversation is ok."""

    from qq_ai_bot.identity.canonical_memory_owners import _event_owner

    trigger_id = _resolve_trigger_event_id(
        connection, event_id=event_id, tool_receipt_id=tool_receipt_id
    )
    if trigger_id is None:
        return MISSING_OWNER
    owner, error = _event_owner(connection, trigger_id, ctx)
    if error is not None or owner is None:
        return error or MISSING_OWNER
    if not _aligns_fact_owner(fact, owner[0], owner[1]):
        return AMBIGUOUS_OWNER
    return None


def classify_runtime_evidence_chain(
    connection: sqlite3.Connection,
    fact: Mapping[str, Any],
    *,
    event_id: object | None,
    tool_receipt_id: object | None,
) -> str | None:
    """Return a content-free conflict category, or None when the v2 chain is proven."""

    trigger_id = _resolve_trigger_event_id(
        connection, event_id=event_id, tool_receipt_id=tool_receipt_id
    )
    if trigger_id is None:
        return MISSING_OWNER
    event = _load_event(connection, trigger_id)
    if event is None:
        return MISSING_OWNER
    if evidence_event_chain_ok(connection, fact, _row_map(event)):
        return None
    if not event_is_v2_live(
        canonical_event_id=event["canonical_event_id"],
        suppression_status=event["suppression_status"],
        canonical_conversation_id=event["canonical_conversation_id"],
        author_kind=event["author_kind"],
    ):
        return MISSING_OWNER
    return AMBIGUOUS_OWNER


classify_active_evidence_chain = classify_runtime_evidence_chain


def _iter_active_evidence(
    connection: sqlite3.Connection,
) -> tuple[tuple[int, int, object, object], ...]:
    if not _table_exists(connection, "memory_evidence"):
        return ()
    required = ("id", "fact_id", "event_id", "tool_receipt_id")
    if not all(_column_exists(connection, "memory_evidence", column) for column in required):
        return ()
    return tuple(
        (int(row["id"]), int(row["fact_id"]), row["event_id"], row["tool_receipt_id"])
        for row in connection.execute(
            "SELECT id, fact_id, event_id, tool_receipt_id FROM memory_evidence ORDER BY id"
        )
    )


def iter_unproven_legacy_evidence(
    connection: sqlite3.Connection,
    facts: Mapping[int, Mapping[str, Any]],
    ctx: C21OwnerContext,
) -> tuple[tuple[int, str], ...]:
    """(evidence_id, category) for active-fact evidence C7 cannot uniquely prove."""

    found: list[tuple[int, str]] = []
    for evidence_id, fact_id, event_id, tool_receipt_id in _iter_active_evidence(connection):
        fact = facts.get(fact_id)
        if fact is None:
            found.append((evidence_id, MISSING_OWNER))
            continue
        if str(fact.get("status") or "") != "active":
            continue
        category = classify_legacy_evidence_owner(
            connection,
            fact,
            ctx,
            event_id=event_id,
            tool_receipt_id=tool_receipt_id,
        )
        if category is not None:
            found.append((evidence_id, category))
    return tuple(found)


def iter_unproven_active_evidence(
    connection: sqlite3.Connection,
    facts: Mapping[int, Mapping[str, Any]],
) -> tuple[tuple[int, str], ...]:
    """(evidence_id, category) for active-fact evidence that is not v2-readable.

    Hidden suppression (duplicate/suppressed/unknown) is not itself a conflict.
    An active fact whose every evidence row is hidden still fails closed.
    """

    by_fact: dict[int, list[tuple[int, object, object]]] = defaultdict(list)
    found: list[tuple[int, str]] = []
    for evidence_id, fact_id, event_id, tool_receipt_id in _iter_active_evidence(connection):
        by_fact[fact_id].append((evidence_id, event_id, tool_receipt_id))
    for fact_id, items in by_fact.items():
        fact = facts.get(fact_id)
        if fact is None:
            found.extend((evidence_id, MISSING_OWNER) for evidence_id, _event, _receipt in items)
            continue
        if str(fact.get("status") or "") != "active":
            continue
        readable = 0
        broken = False
        for evidence_id, event_id, tool_receipt_id in items:
            trigger_id = _resolve_trigger_event_id(
                connection, event_id=event_id, tool_receipt_id=tool_receipt_id
            )
            event = _load_event(connection, trigger_id) if trigger_id is not None else None
            if event is not None and event_suppression_is_hidden(event["suppression_status"]):
                continue
            category = classify_runtime_evidence_chain(
                connection,
                fact,
                event_id=event_id,
                tool_receipt_id=tool_receipt_id,
            )
            if category is not None:
                found.append((evidence_id, category))
                broken = True
                continue
            readable += 1
        if readable == 0 and not broken:
            found.append((items[0][0], MISSING_OWNER))
    return tuple(found)


def require_c21_readable_evidence(
    connection: sqlite3.Connection,
    facts: Mapping[int, Mapping[str, Any]] | None = None,
) -> None:
    from qq_ai_bot.identity.errors import IdentityCutoverPreconditionError

    loaded = facts if facts is not None else _load_facts(connection)
    if iter_unproven_active_evidence(connection, loaded):
        raise IdentityCutoverPreconditionError(C21_EVIDENCE_INCOMPLETE)


def _planned_owner_for_event(
    event: sqlite3.Row,
    ctx: C21OwnerContext,
    watermarks: Iterable[object],
) -> tuple[tuple[str | None, str | None] | None, str | None]:
    from qq_ai_bot.identity.canonical_memory_owners import (
        _binding_owner_for_scope,
        _event_expected_kind,
        _private_peer_external,
    )

    expected = _event_expected_kind(event)
    if expected is None:
        return None, MISSING_OWNER
    owner, error = _binding_owner_for_scope(
        ctx,
        expected=expected,
        group_id=event["group_id"],
        private_peer=_private_peer_external(event),
    )
    if error is not None or owner is None:
        return None, error or MISSING_OWNER
    kind = "space" if owner[1] else "private"
    owner_id = owner[1] or owner[0]
    matches = [
        mark
        for mark in watermarks
        if getattr(mark, "kind", None) == kind and getattr(mark, "owner_id", None) == owner_id
    ]
    if len(matches) != 1:
        return None, MISSING_OWNER if not matches else AMBIGUOUS_OWNER
    return owner, None


def require_planned_evidence_alignable(
    connection: sqlite3.Connection,
    *,
    watermarks: Iterable[object],
    suppress_event_ids: Iterable[int],
) -> None:
    """Read-only C26 plan gate: every active evidence must uniquely map and align.

    Planned-visible excludes both this-plan suppress_event_ids and events
    whose existing suppression_status is already hidden. Apply COALESCE keeps
    those statuses, so plan must fail closed first. NULL/keeper stay visible.
    """

    from qq_ai_bot.identity.canonical_memory_owners import overlay_owner_bindings
    from qq_ai_bot.identity.errors import IdentityCutoverPreconditionError

    facts = _load_facts(connection)
    if not facts:
        return
    ctx = overlay_owner_bindings(
        connection,
        planned_person_bindings={},
        planned_space_bindings={},
        non_person_accounts=frozenset(),
    )
    suppress = {int(item) for item in suppress_event_ids}
    by_fact: dict[int, list[tuple[int, object, object]]] = defaultdict(list)
    for evidence_id, fact_id, event_id, tool_receipt_id in _iter_active_evidence(connection):
        by_fact[fact_id].append((evidence_id, event_id, tool_receipt_id))
    for fact_id, items in by_fact.items():
        fact = facts.get(fact_id)
        if fact is None or str(fact.get("status") or "") != "active":
            if fact is None:
                raise IdentityCutoverPreconditionError(C21_EVIDENCE_INCOMPLETE)
            continue
        visible = 0
        for _evidence_id, event_id, tool_receipt_id in items:
            trigger_id = _resolve_trigger_event_id(
                connection, event_id=event_id, tool_receipt_id=tool_receipt_id
            )
            if trigger_id is None:
                raise IdentityCutoverPreconditionError(C21_EVIDENCE_INCOMPLETE)
            event = _load_event(connection, trigger_id)
            if event is None:
                raise IdentityCutoverPreconditionError(C21_EVIDENCE_INCOMPLETE)
            owner, error = _planned_owner_for_event(event, ctx, watermarks)
            if error is not None or owner is None:
                raise IdentityCutoverPreconditionError(C21_EVIDENCE_INCOMPLETE)
            if not _aligns_fact_owner(fact, owner[0], owner[1]):
                raise IdentityCutoverPreconditionError(C21_EVIDENCE_INCOMPLETE)
            if trigger_id in suppress or event_suppression_is_hidden(event["suppression_status"]):
                continue
            visible += 1
        if visible == 0:
            raise IdentityCutoverPreconditionError(C21_EVIDENCE_INCOMPLETE)
