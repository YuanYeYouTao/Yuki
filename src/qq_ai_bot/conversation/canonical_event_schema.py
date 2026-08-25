"""Frozen C4 ledger-shadow trigger SQL shared by ORM create_all.

Revision 0045 must embed identical literals and must not import this module.

Triggers encode cross-column shape only. Parent existence and parent DELETE /
primary-key UPDATE are enforced by real FOREIGN KEY constraints on the
canonical reference shadows. suppression_status='duplicate' is exact-utterance
equivalence, so it must carry a SHA-256 utterance_fingerprint; keeper is the
unique logical primary and may be classified before a fingerprint exists.
Plugin/external events stay on suppression_status IS NULL and therefore do
not require a fingerprint.
"""

from __future__ import annotations

CANONICAL_EVENT_RECEIPT_TABLE: str = "canonical_event_receipts"
CHAT_EVENT_CANONICAL_SHADOW_COLUMNS: tuple[str, ...] = (
    "canonical_event_id",
    "canonical_conversation_id",
    "author_kind",
    "author_person_id",
    "author_presence_id",
    "ingress_presence_id",
    "utterance_fingerprint",
    "suppression_status",
    "ingress_provider",
    "ingress_gateway_instance_id",
)
CHAT_EVENT_CANONICAL_SHADOW_INDEXES: tuple[str, ...] = (
    "ix_chat_events_canonical_event_id",
    "ix_chat_events_canonical_conversation_id",
    "uq_chat_events_canonical_event_keeper",
)
CONVERSATION_SCOPE_CANONICAL_SHADOW_COLUMNS: tuple[str, ...] = ("canonical_conversation_id",)
CONVERSATION_SCOPE_CANONICAL_SHADOW_INDEXES: tuple[str, ...] = (
    "ix_conversation_scopes_canonical_conversation_id",
)
CHAT_EVENT_CANONICAL_SHADOW_FOREIGN_KEYS: tuple[tuple[str, str], ...] = (
    ("canonical_conversation_id", "canonical_conversations"),
    ("author_person_id", "persons"),
    ("author_presence_id", "presences"),
    ("ingress_presence_id", "presences"),
)

_UUID4_GLOB = (
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-"
    "[89ab][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
)
_SHA256_GLOB = "[0-9a-f]" * 64


def _optional_uuid4_sql(column: str) -> str:
    return (
        f"({column} IS NULL OR ("
        f"length({column}) = 36 AND {column} = lower({column}) "
        f"AND {column} GLOB '{_UUID4_GLOB}'"
        "))"
    )


def _uuid4_sql(column: str) -> str:
    return f"length({column}) = 36 AND {column} = lower({column}) AND {column} GLOB '{_UUID4_GLOB}'"


def _optional_sha256_sql(column: str) -> str:
    return (
        f"({column} IS NULL OR ("
        f"length({column}) = 64 AND {column} = lower({column}) "
        f"AND {column} GLOB '{_SHA256_GLOB}'"
        "))"
    )


def _chat_event_shadow_valid_sql() -> str:
    return (
        f"{_optional_uuid4_sql('NEW.canonical_event_id')} AND "
        "("
        f"{_optional_uuid4_sql('NEW.canonical_conversation_id')} AND ("
        "NEW.canonical_conversation_id IS NULL OR EXISTS ("
        "SELECT 1 FROM canonical_conversations "
        "WHERE id = NEW.canonical_conversation_id))"
        ") AND ("
        "("
        "NEW.author_kind IS NULL AND NEW.author_person_id IS NULL "
        "AND NEW.author_presence_id IS NULL"
        ") OR ("
        "NEW.author_kind = 'person' AND NEW.author_person_id IS NOT NULL "
        f"AND {_uuid4_sql('NEW.author_person_id')} "
        "AND NEW.author_presence_id IS NULL "
        "AND EXISTS (SELECT 1 FROM persons WHERE id = NEW.author_person_id)"
        ") OR ("
        "NEW.author_kind = 'yuki' AND NEW.author_presence_id IS NOT NULL "
        f"AND {_uuid4_sql('NEW.author_presence_id')} "
        "AND NEW.author_person_id IS NULL "
        "AND EXISTS (SELECT 1 FROM presences WHERE id = NEW.author_presence_id)"
        ") OR ("
        "NEW.author_kind IN ('external_bot', 'system') "
        "AND NEW.author_person_id IS NULL AND NEW.author_presence_id IS NULL"
        ")"
        ") AND ("
        f"{_optional_uuid4_sql('NEW.ingress_presence_id')} AND ("
        "NEW.ingress_presence_id IS NULL OR EXISTS ("
        "SELECT 1 FROM presences WHERE id = NEW.ingress_presence_id))"
        ") AND "
        f"{_optional_sha256_sql('NEW.utterance_fingerprint')} AND ("
        "NEW.suppression_status IS NULL OR ("
        "NEW.suppression_status = 'keeper' "
        "AND NEW.canonical_event_id IS NOT NULL"
        ") OR ("
        "NEW.suppression_status = 'duplicate' "
        "AND NEW.canonical_event_id IS NOT NULL "
        "AND NEW.utterance_fingerprint IS NOT NULL"
        ")"
        ") AND ("
        "NEW.ingress_provider IS NULL OR ("
        "length(NEW.ingress_provider) > 0 AND length(NEW.ingress_provider) <= 32 "
        "AND NEW.ingress_provider = lower(NEW.ingress_provider) "
        "AND NEW.ingress_provider = trim(NEW.ingress_provider))"
        ") AND ("
        "NEW.ingress_gateway_instance_id IS NULL OR ("
        "length(NEW.ingress_gateway_instance_id) > 0 "
        "AND length(NEW.ingress_gateway_instance_id) <= 128 "
        "AND NEW.ingress_gateway_instance_id = trim(NEW.ingress_gateway_instance_id))"
        ")"
    )


def _scope_shadow_invalid_sql() -> str:
    return (
        "NEW.canonical_conversation_id IS NOT NULL AND NOT ("
        f"{_uuid4_sql('NEW.canonical_conversation_id')} AND EXISTS ("
        "SELECT 1 FROM canonical_conversations "
        "WHERE id = NEW.canonical_conversation_id)"
        ")"
    )


_CHAT_EVENT_SHADOW_VALID = _chat_event_shadow_valid_sql()
_SCOPE_SHADOW_INVALID = _scope_shadow_invalid_sql()

CHAT_EVENT_CANONICAL_SHADOW_TRIGGERS: tuple[str, ...] = (
    f"""
CREATE TRIGGER trg_chat_events_canonical_shadow_insert
BEFORE INSERT ON chat_events
BEGIN
    SELECT RAISE(ABORT, 'invalid chat event canonical shadow')
    WHERE NOT ({_CHAT_EVENT_SHADOW_VALID});
END
""".strip(),
    f"""
CREATE TRIGGER trg_chat_events_canonical_shadow_update
BEFORE UPDATE OF canonical_event_id, canonical_conversation_id, author_kind,
    author_person_id, author_presence_id, ingress_presence_id,
    utterance_fingerprint, suppression_status, ingress_provider,
    ingress_gateway_instance_id
ON chat_events
BEGIN
    SELECT RAISE(ABORT, 'invalid chat event canonical shadow')
    WHERE NOT ({_CHAT_EVENT_SHADOW_VALID});
END
""".strip(),
)

CONVERSATION_SCOPE_CANONICAL_SHADOW_TRIGGERS: tuple[str, ...] = (
    f"""
CREATE TRIGGER trg_conversation_scopes_canonical_shadow_insert
BEFORE INSERT ON conversation_scopes
BEGIN
    SELECT RAISE(ABORT, 'invalid conversation scope canonical shadow')
    WHERE {_SCOPE_SHADOW_INVALID};
END
""".strip(),
    f"""
CREATE TRIGGER trg_conversation_scopes_canonical_shadow_update
BEFORE UPDATE OF canonical_conversation_id ON conversation_scopes
BEGIN
    SELECT RAISE(ABORT, 'invalid conversation scope canonical shadow')
    WHERE {_SCOPE_SHADOW_INVALID};
END
""".strip(),
)

C4_TRIGGER_SQL: tuple[str, ...] = (
    *CHAT_EVENT_CANONICAL_SHADOW_TRIGGERS,
    *CONVERSATION_SCOPE_CANONICAL_SHADOW_TRIGGERS,
)

C4_TRIGGER_NAMES: tuple[str, ...] = (
    "trg_chat_events_canonical_shadow_insert",
    "trg_chat_events_canonical_shadow_update",
    "trg_conversation_scopes_canonical_shadow_insert",
    "trg_conversation_scopes_canonical_shadow_update",
)
