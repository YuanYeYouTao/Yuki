"""SQLAlchemy models for canonical conversations, routes, and command receipts.

These models register schema only. They do not implement routing, backfill,
or control-plane behavior.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import Table

from qq_ai_bot.conversation.canonical_schema import (
    ALIAS_PRIMARY_LOCK_TRIGGERS,
    CONVERSATION_PRIMARY_POINTER_TRIGGERS,
    PARENT_ROUTE_GUARD_TRIGGERS,
    PERSON_ACTIVE_ROUTE_TRIGGERS,
    SPACE_ACTIVE_ROUTE_TRIGGERS,
    SPACE_BINDING_INGEST_ROUTE_TRIGGERS,
)
from qq_ai_bot.identity.db_models import optional_uuid4_text36_sql, uuid4_text36_sql
from qq_ai_bot.persistence.models import Base

CANONICAL_CONVERSATION_TABLES: tuple[str, ...] = (
    "canonical_conversations",
    "conversation_legacy_aliases",
    "person_active_routes",
    "space_binding_ingest_routes",
    "space_active_routes",
    "control_command_receipts",
)
CANONICAL_CONVERSATION_CREATE_ORDER: tuple[str, ...] = (
    "canonical_conversations",
    "conversation_legacy_aliases",
    "person_active_routes",
    "space_binding_ingest_routes",
    "space_active_routes",
    "control_command_receipts",
)


def sha256_hex_sql(column: str) -> str:
    """SQLite CHECK for a lowercase SHA-256 hex digest."""

    hex_glob = "[0-9a-f]" * 64
    return f"length({column}) = 64 AND {column} = lower({column}) AND {column} GLOB '{hex_glob}'"


def trimmed_token_sql(column: str, max_length: int) -> str:
    """Non-empty, trim-invariant token with a defensible length bound."""

    return (
        f"length({column}) > 0 AND length({column}) <= {max_length} AND {column} = trim({column})"
    )


def optional_opaque_id_sql(column: str, max_length: int) -> str:
    """Nullable opaque identifier: empty and padded values are rejected."""

    return f"{column} IS NULL OR ({trimmed_token_sql(column, max_length)})"


def optional_effective_state_json_sql(column: str, max_length: int) -> str:
    """Nullable sanitized JSON object snapshot with a hard character bound."""

    return (
        f"{column} IS NULL OR ("
        f"length({column}) > 0 AND length({column}) <= {max_length} "
        f"AND {column} = trim({column}) "
        f"AND json_valid({column}) "
        f"AND substr({column}, 1, 1) = '{{'"
        ")"
    )


def _install_triggers(
    target: Table,
    expected: Table | object,
    connection: Connection,
    sql: tuple[str, ...],
) -> None:
    if target is not expected:
        name = getattr(expected, "name", type(expected).__name__)
        raise RuntimeError(f"trigger installer is bound only to {name}")
    for statement in sql:
        connection.execute(text(statement))


_PARENT_ROUTE_GUARD_MARKER = "trg_identity_bindings_route_consistency_update"


def _install_parent_route_guards_if_ready(connection: Connection) -> None:
    """Install reverse parent UPDATE guards once all three route tables exist."""

    present = connection.execute(
        text(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name IN "
            "('person_active_routes', 'space_binding_ingest_routes', 'space_active_routes')"
        )
    ).scalar()
    if int(present or 0) != 3:
        return
    installed = connection.execute(
        text("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = :name"),
        {"name": _PARENT_ROUTE_GUARD_MARKER},
    ).scalar()
    if installed is not None:
        return
    for statement in PARENT_ROUTE_GUARD_TRIGGERS:
        connection.execute(text(statement))


class CanonicalConversationModel(Base):
    """One canonical private conversation per Person, or one space conversation per Space."""

    __tablename__ = "canonical_conversations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name="fk_canonical_conversations_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name="fk_canonical_conversations_space",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["id", "primary_alias_id", "primary_marker"],
            [
                "conversation_legacy_aliases.conversation_id",
                "conversation_legacy_aliases.id",
                "conversation_legacy_aliases.is_primary",
            ],
            name="fk_canonical_conversations_primary_alias",
            deferrable=True,
            initially="DEFERRED",
            onupdate="NO ACTION",
            ondelete="NO ACTION",
        ),
        CheckConstraint(uuid4_text36_sql("id"), name="ck_canonical_conversations_id"),
        CheckConstraint(
            "kind IN ('private', 'space')",
            name="ck_canonical_conversations_kind",
        ),
        CheckConstraint(
            "("
            "kind = 'private' AND person_id IS NOT NULL AND space_id IS NULL"
            ") OR ("
            "kind = 'space' AND space_id IS NOT NULL AND person_id IS NULL"
            ")",
            name="ck_canonical_conversations_owner_xor",
        ),
        CheckConstraint(
            optional_uuid4_text36_sql("person_id"),
            name="ck_canonical_conversations_person_id",
        ),
        CheckConstraint(
            optional_uuid4_text36_sql("space_id"),
            name="ck_canonical_conversations_space_id",
        ),
        CheckConstraint(
            uuid4_text36_sql("primary_alias_id"),
            name="ck_canonical_conversations_primary_alias_id",
        ),
        CheckConstraint("primary_marker = 1", name="ck_canonical_conversations_primary_marker"),
        CheckConstraint("generation >= 1", name="ck_canonical_conversations_generation"),
        CheckConstraint("revision >= 1", name="ck_canonical_conversations_revision"),
        CheckConstraint(
            "starts_after_event_id >= 0 AND last_event_id >= 0 "
            "AND last_generation_change_event_id >= 0 AND covered_through_event_id >= 0 "
            "AND uncovered_event_count >= 0 AND uncovered_character_count >= 0 "
            "AND starts_after_event_id <= last_event_id "
            "AND last_generation_change_event_id <= last_event_id "
            "AND covered_through_event_id <= last_event_id "
            "AND starts_after_event_id <= covered_through_event_id",
            name="ck_canonical_conversations_watermarks",
        ),
        Index(
            "uq_canonical_conversations_person",
            "person_id",
            unique=True,
            sqlite_where=text("kind = 'private'"),
        ),
        Index(
            "uq_canonical_conversations_space",
            "space_id",
            unique=True,
            sqlite_where=text("kind = 'space'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    person_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    space_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    primary_alias_id: Mapped[str] = mapped_column(String(36), nullable=False)
    primary_marker: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    starts_after_event_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_event_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_generation_change_event_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    covered_through_event_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uncovered_event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uncovered_character_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@event.listens_for(CanonicalConversationModel.__table__, "after_create")
def install_canonical_conversation_triggers(
    target: Table,
    connection: Connection,
    **_kwargs: object,
) -> None:
    """Install the immutable primary-alias pointer trigger after table create."""

    _install_triggers(
        target,
        CanonicalConversationModel.__table__,
        connection,
        CONVERSATION_PRIMARY_POINTER_TRIGGERS,
    )


class ConversationLegacyAliasModel(Base):
    """Legacy scope_key mapping onto one canonical conversation."""

    __tablename__ = "conversation_legacy_aliases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id"],
            ["canonical_conversations.id"],
            name="fk_conversation_legacy_aliases_conversation",
            deferrable=True,
            initially="DEFERRED",
            onupdate="NO ACTION",
            ondelete="NO ACTION",
        ),
        UniqueConstraint(
            "conversation_id",
            "id",
            "is_primary",
            name="uq_conversation_legacy_aliases_primary_target",
        ),
        UniqueConstraint("scope_key", name="uq_conversation_legacy_aliases_scope_key"),
        CheckConstraint(uuid4_text36_sql("id"), name="ck_conversation_legacy_aliases_id"),
        CheckConstraint(
            uuid4_text36_sql("conversation_id"),
            name="ck_conversation_legacy_aliases_conversation_id",
        ),
        CheckConstraint(
            trimmed_token_sql("scope_key", 255),
            name="ck_conversation_legacy_aliases_scope_key",
        ),
        CheckConstraint(
            "is_primary IN (0, 1)",
            name="ck_conversation_legacy_aliases_is_primary",
        ),
        Index("ix_conversation_legacy_aliases_conversation_id", "conversation_id"),
        Index(
            "uq_conversation_legacy_aliases_primary",
            "conversation_id",
            unique=True,
            sqlite_where=text("is_primary = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False)
    is_primary: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@event.listens_for(ConversationLegacyAliasModel.__table__, "after_create")
def install_conversation_legacy_alias_triggers(
    target: Table,
    connection: Connection,
    **_kwargs: object,
) -> None:
    """Install pinned-primary update/delete guards after table create."""

    _install_triggers(
        target,
        ConversationLegacyAliasModel.__table__,
        connection,
        ALIAS_PRIMARY_LOCK_TRIGGERS,
    )


class PersonActiveRouteModel(Base):
    """Active outbound route from one Person to one binding plus Presence."""

    __tablename__ = "person_active_routes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name="fk_person_active_routes_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["identity_binding_id"],
            ["identity_bindings.id"],
            name="fk_person_active_routes_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["presence_id"],
            ["presences.id"],
            name="fk_person_active_routes_presence",
            ondelete="RESTRICT",
        ),
        CheckConstraint(uuid4_text36_sql("person_id"), name="ck_person_active_routes_person_id"),
        CheckConstraint(
            uuid4_text36_sql("identity_binding_id"),
            name="ck_person_active_routes_binding_id",
        ),
        CheckConstraint(
            uuid4_text36_sql("presence_id"), name="ck_person_active_routes_presence_id"
        ),
        CheckConstraint("route_generation >= 1", name="ck_person_active_routes_generation"),
        CheckConstraint("paused IN (0, 1)", name="ck_person_active_routes_paused"),
        CheckConstraint("revision >= 1", name="ck_person_active_routes_revision"),
    )

    person_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    identity_binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    presence_id: Mapped[str] = mapped_column(String(36), nullable=False)
    route_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@event.listens_for(PersonActiveRouteModel.__table__, "after_create")
def install_person_active_route_triggers(
    target: Table,
    connection: Connection,
    **_kwargs: object,
) -> None:
    """Install ownership/platform guards for person active routes."""

    _install_triggers(
        target,
        PersonActiveRouteModel.__table__,
        connection,
        PERSON_ACTIVE_ROUTE_TRIGGERS,
    )
    _install_parent_route_guards_if_ready(connection)


class SpaceBindingIngestRouteModel(Base):
    """Ingest Presence assigned to one external space binding."""

    __tablename__ = "space_binding_ingest_routes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["space_binding_id"],
            ["space_bindings.id"],
            name="fk_space_binding_ingest_routes_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ingest_presence_id"],
            ["presences.id"],
            name="fk_space_binding_ingest_routes_presence",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            uuid4_text36_sql("space_binding_id"),
            name="ck_space_binding_ingest_routes_binding_id",
        ),
        CheckConstraint(
            uuid4_text36_sql("ingest_presence_id"),
            name="ck_space_binding_ingest_routes_presence_id",
        ),
        CheckConstraint("route_generation >= 1", name="ck_space_binding_ingest_routes_generation"),
        CheckConstraint("paused IN (0, 1)", name="ck_space_binding_ingest_routes_paused"),
        CheckConstraint("revision >= 1", name="ck_space_binding_ingest_routes_revision"),
    )

    space_binding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ingest_presence_id: Mapped[str] = mapped_column(String(36), nullable=False)
    route_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@event.listens_for(SpaceBindingIngestRouteModel.__table__, "after_create")
def install_space_binding_ingest_route_triggers(
    target: Table,
    connection: Connection,
    **_kwargs: object,
) -> None:
    """Install platform guards for space-binding ingest routes."""

    _install_triggers(
        target,
        SpaceBindingIngestRouteModel.__table__,
        connection,
        SPACE_BINDING_INGEST_ROUTE_TRIGGERS,
    )
    _install_parent_route_guards_if_ready(connection)


class SpaceActiveRouteModel(Base):
    """Active outbound route from one Space to one binding plus Presence."""

    __tablename__ = "space_active_routes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name="fk_space_active_routes_space",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["space_binding_id"],
            ["space_bindings.id"],
            name="fk_space_active_routes_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["presence_id"],
            ["presences.id"],
            name="fk_space_active_routes_presence",
            ondelete="RESTRICT",
        ),
        CheckConstraint(uuid4_text36_sql("space_id"), name="ck_space_active_routes_space_id"),
        CheckConstraint(
            uuid4_text36_sql("space_binding_id"),
            name="ck_space_active_routes_binding_id",
        ),
        CheckConstraint(uuid4_text36_sql("presence_id"), name="ck_space_active_routes_presence_id"),
        CheckConstraint("route_generation >= 1", name="ck_space_active_routes_generation"),
        CheckConstraint("paused IN (0, 1)", name="ck_space_active_routes_paused"),
        CheckConstraint("revision >= 1", name="ck_space_active_routes_revision"),
    )

    space_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    space_binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    presence_id: Mapped[str] = mapped_column(String(36), nullable=False)
    route_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@event.listens_for(SpaceActiveRouteModel.__table__, "after_create")
def install_space_active_route_triggers(
    target: Table,
    connection: Connection,
    **_kwargs: object,
) -> None:
    """Install ownership/platform guards for space active routes."""

    _install_triggers(
        target,
        SpaceActiveRouteModel.__table__,
        connection,
        SPACE_ACTIVE_ROUTE_TRIGGERS,
    )
    _install_parent_route_guards_if_ready(connection)


class ControlCommandReceiptModel(Base):
    """Local ledger of control-command idempotency receipts. Secret-free."""

    __tablename__ = "control_command_receipts"
    __table_args__ = (
        UniqueConstraint(
            "principal_id",
            "request_id",
            name="uq_control_command_receipts_principal_request",
        ),
        CheckConstraint(
            uuid4_text36_sql("principal_id"),
            name="ck_control_command_receipts_principal_id",
        ),
        CheckConstraint(
            uuid4_text36_sql("request_id"),
            name="ck_control_command_receipts_request_id",
        ),
        CheckConstraint(
            sha256_hex_sql("payload_hash"),
            name="ck_control_command_receipts_payload_hash",
        ),
        CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name="ck_control_command_receipts_status",
        ),
        ForeignKeyConstraint(
            ["audit_id"],
            ["admin_operation_events.id"],
            name="fk_control_command_receipts_audit",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "("
            "status = 'succeeded' AND problem_code IS NULL "
            "AND audit_id IS NOT NULL AND audit_id >= 1 "
            "AND effective_state_json IS NOT NULL"
            ") OR ("
            "status = 'failed' AND problem_code IS NOT NULL "
            "AND length(problem_code) > 0 AND length(problem_code) <= 64 "
            "AND problem_code = trim(problem_code) "
            "AND effective_state_json IS NULL "
            "AND result_resource_id IS NULL AND result_revision IS NULL"
            ")",
            name="ck_control_command_receipts_lifecycle",
        ),
        CheckConstraint(
            optional_opaque_id_sql("result_resource_id", 255),
            name="ck_control_command_receipts_result_resource_id",
        ),
        CheckConstraint(
            "result_revision IS NULL OR result_revision >= 1",
            name="ck_control_command_receipts_result_revision",
        ),
        CheckConstraint(
            "audit_id IS NULL OR audit_id >= 1",
            name="ck_control_command_receipts_audit_id",
        ),
        CheckConstraint(
            optional_effective_state_json_sql("effective_state_json", 4096),
            name="ck_control_command_receipts_effective_state",
        ),
        CheckConstraint(
            "("
            "operation_kind IS NULL AND operation_ref IS NULL"
            ") OR ("
            "operation_kind IS NOT NULL AND operation_ref IS NOT NULL "
            f"AND {trimmed_token_sql('operation_kind', 64)} "
            f"AND {trimmed_token_sql('operation_ref', 128)}"
            ")",
            name="ck_control_command_receipts_operation",
        ),
        Index(
            "ix_control_command_receipts_principal_created",
            "principal_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    principal_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result_resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    effective_state_json: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    result_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    problem_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audit_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operation_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operation_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
