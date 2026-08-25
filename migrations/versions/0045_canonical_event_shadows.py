"""Add nullable canonical event shadows and OneBot transport receipts.

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-24

This revision is frozen and self-contained. It must not import application
modules or create tables from current ORM metadata.

chat_events is an FTS content table with many inbound foreign keys, so this
revision uses ADD/DROP COLUMN with PRAGMA foreign_keys=ON. SQLite 3.35+ is
required for DROP COLUMN on downgrade. ADD COLUMN ... REFERENCES ... on a
nullable column without a non-null default is recorded in
PRAGMA foreign_key_list and enforced for child writes and parent DELETE /
primary-key UPDATE while foreign_keys=ON. Triggers keep cross-column shape
rules; they are not a substitute for those foreign keys.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID4_GLOB = (
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-"
    "[89ab][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
)
_SHA256_GLOB = "[0-9a-f]" * 64
_CHAT_EVENT_SHADOW_COLUMNS: tuple[str, ...] = (
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
_CHAT_EVENT_SHADOW_INDEXES: tuple[str, ...] = (
    "ix_chat_events_canonical_event_id",
    "ix_chat_events_canonical_conversation_id",
    "uq_chat_events_canonical_event_keeper",
)
_SCOPE_SHADOW_INDEXES: tuple[str, ...] = ("ix_conversation_scopes_canonical_conversation_id",)
_FK_RESTRICT = "ON UPDATE RESTRICT ON DELETE RESTRICT"


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


def _trimmed_token_sql(column: str, max_length: int) -> str:
    return (
        f"length({column}) > 0 AND length({column}) <= {max_length} AND {column} = trim({column})"
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
_C4_TRIGGER_SQL: tuple[str, ...] = (
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
_C4_TRIGGER_NAMES: tuple[str, ...] = (
    "trg_chat_events_canonical_shadow_insert",
    "trg_chat_events_canonical_shadow_update",
    "trg_conversation_scopes_canonical_shadow_insert",
    "trg_conversation_scopes_canonical_shadow_update",
)


def _require_sqlite_column_alter() -> None:
    connection = op.get_bind()
    raw = connection.exec_driver_sql("SELECT sqlite_version()").scalar_one()
    parts = tuple(int(part) for part in str(raw).split(".")[:3])
    if parts < (3, 35, 0):
        raise RuntimeError(
            f"0045 requires SQLite 3.35+ for ADD/DROP COLUMN with foreign_keys=ON, got {raw}"
        )
    if int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()) != 1:
        raise RuntimeError("0045 requires PRAGMA foreign_keys=ON")


def upgrade() -> None:
    """Add nullable ledger/scope shadows, OneBot receipts, and shadow triggers."""

    _require_sqlite_column_alter()
    column_ddl = (
        ("canonical_event_id", "VARCHAR(36)"),
        (
            "canonical_conversation_id",
            f"VARCHAR(36) REFERENCES canonical_conversations(id) {_FK_RESTRICT}",
        ),
        ("author_kind", "VARCHAR(16)"),
        (
            "author_person_id",
            f"VARCHAR(36) REFERENCES persons(id) {_FK_RESTRICT}",
        ),
        (
            "author_presence_id",
            f"VARCHAR(36) REFERENCES presences(id) {_FK_RESTRICT}",
        ),
        (
            "ingress_presence_id",
            f"VARCHAR(36) REFERENCES presences(id) {_FK_RESTRICT}",
        ),
        ("utterance_fingerprint", "VARCHAR(64)"),
        ("suppression_status", "VARCHAR(16)"),
        ("ingress_provider", "VARCHAR(32)"),
        ("ingress_gateway_instance_id", "VARCHAR(128)"),
    )
    for name, sql_type in column_ddl:
        op.execute(sa.text(f"ALTER TABLE chat_events ADD COLUMN {name} {sql_type}"))
    op.execute(
        sa.text(
            "ALTER TABLE conversation_scopes ADD COLUMN canonical_conversation_id "
            f"VARCHAR(36) REFERENCES canonical_conversations(id) {_FK_RESTRICT}"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_chat_events_canonical_event_id ON chat_events (canonical_event_id)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_chat_events_canonical_conversation_id "
            "ON chat_events (canonical_conversation_id)"
        )
    )
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_chat_events_canonical_event_keeper "
            "ON chat_events (canonical_event_id) WHERE suppression_status = 'keeper'"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_conversation_scopes_canonical_conversation_id "
            "ON conversation_scopes (canonical_conversation_id)"
        )
    )
    op.create_table(
        "canonical_event_receipts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ingress_presence_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("platform_message_id", sa.String(128), nullable=False),
        sa.Column("canonical_event_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ingress_presence_id",
            "event_type",
            "platform_message_id",
            name="uq_canonical_event_receipts_transport",
        ),
        sa.ForeignKeyConstraint(
            ["ingress_presence_id"],
            ["presences.id"],
            name="fk_canonical_event_receipts_ingress_presence",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _uuid4_sql("ingress_presence_id"),
            name="ck_canonical_event_receipts_ingress_presence_id",
        ),
        sa.CheckConstraint(
            _trimmed_token_sql("event_type", 64),
            name="ck_canonical_event_receipts_event_type",
        ),
        sa.CheckConstraint(
            _trimmed_token_sql("platform_message_id", 128),
            name="ck_canonical_event_receipts_platform_message_id",
        ),
        sa.CheckConstraint(
            _uuid4_sql("canonical_event_id"),
            name="ck_canonical_event_receipts_canonical_event_id",
        ),
    )
    op.create_index(
        "ix_canonical_event_receipts_canonical_event_id",
        "canonical_event_receipts",
        ["canonical_event_id"],
    )
    for statement in _C4_TRIGGER_SQL:
        op.execute(statement)


def downgrade() -> None:
    """Remove only C4 shadows/receipts, leaving 0044 ledger rows intact."""

    _require_sqlite_column_alter()
    for name in _C4_TRIGGER_NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_index(
        "ix_canonical_event_receipts_canonical_event_id",
        table_name="canonical_event_receipts",
    )
    op.drop_table("canonical_event_receipts")
    for name in _SCOPE_SHADOW_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    for name in _CHAT_EVENT_SHADOW_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute(sa.text("ALTER TABLE conversation_scopes DROP COLUMN canonical_conversation_id"))
    for column in reversed(_CHAT_EVENT_SHADOW_COLUMNS):
        op.execute(sa.text(f"ALTER TABLE chat_events DROP COLUMN {column}"))
