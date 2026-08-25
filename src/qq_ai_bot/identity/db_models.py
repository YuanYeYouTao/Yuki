"""SQLAlchemy models for the canonical identity foundation tables.

These models register schema only. They do not implement backfill, routing,
or control-plane behavior.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import MetaData, Table

from qq_ai_bot.identity.canonical_ownership_schema import (
    C5_OWNERSHIP_COLUMNS,
    C5_OWNERSHIP_TABLES,
    C5_TRIGGER_NAMES,
    C5_TRIGGER_SQL,
)
from qq_ai_bot.persistence.models import Base

CANONICAL_IDENTITY_TABLES: tuple[str, ...] = (
    "persons",
    "identity_bindings",
    "spaces",
    "space_bindings",
    "presences",
    "identity_runtime_state",
    "identity_backfill_runs",
    "identity_conflicts",
)
CANONICAL_IDENTITY_CREATE_ORDER: tuple[str, ...] = (
    "persons",
    "spaces",
    "presences",
    "identity_bindings",
    "space_bindings",
    "identity_runtime_state",
    "identity_backfill_runs",
    "identity_conflicts",
)

_UUID4_GLOB = (
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-"
    "[89ab][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
)
_RUNTIME_STATE_V1_SEED_SQL = """
INSERT INTO identity_runtime_state (
    id, state, cutover_id, source_fingerprint, completed_at,
    revision, created_at, updated_at
)
SELECT 1, 'v1', NULL, NULL, NULL, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
WHERE NOT EXISTS (
    SELECT 1 FROM identity_runtime_state WHERE id = 1
)
"""


def uuid4_text36_sql(column: str) -> str:
    """SQLite CHECK for canonical lowercase UUID4 TEXT(36)."""

    return f"length({column}) = 36 AND {column} = lower({column}) AND {column} GLOB '{_UUID4_GLOB}'"


def optional_uuid4_text36_sql(column: str) -> str:
    """SQLite CHECK for a nullable canonical UUID4 TEXT(36)."""

    return f"{column} IS NULL OR ({uuid4_text36_sql(column)})"


def canonical_platform_sql(column: str) -> str:
    """Non-empty, trimmed, lowercase platform token."""

    return (
        f"length({column}) > 0 AND length({column}) <= 32 "
        f"AND {column} = lower({column}) AND {column} = trim({column})"
    )


def opaque_external_id_sql(column: str) -> str:
    """Non-empty, trimmed, case-preserving opaque external ID."""

    return f"length({column}) > 0 AND length({column}) <= 255 AND {column} = trim({column})"


class CanonicalPersonModel(Base):
    """Permanent person subject. Display names live on bindings."""

    __tablename__ = "persons"
    __table_args__ = (
        CheckConstraint(uuid4_text36_sql("id"), name="ck_persons_id"),
        CheckConstraint("enabled IN (0, 1)", name="ck_persons_enabled"),
        CheckConstraint("revision >= 1", name="ck_persons_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdentityBindingModel(Base):
    """One external account owned by one person."""

    __tablename__ = "identity_bindings"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "external_account_id",
            name="uq_identity_bindings_platform_account",
        ),
        CheckConstraint(uuid4_text36_sql("id"), name="ck_identity_bindings_id"),
        CheckConstraint(
            uuid4_text36_sql("person_id"),
            name="ck_identity_bindings_person_id",
        ),
        CheckConstraint(canonical_platform_sql("platform"), name="ck_identity_bindings_platform"),
        CheckConstraint(
            opaque_external_id_sql("external_account_id"),
            name="ck_identity_bindings_external_account_id",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_identity_bindings_status",
        ),
        CheckConstraint("revision >= 1", name="ck_identity_bindings_revision"),
        Index("ix_identity_bindings_person_id", "person_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    person_id: Mapped[str] = mapped_column(
        ForeignKey("persons.id", ondelete="RESTRICT"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CanonicalSpaceModel(Base):
    """Permanent space subject. External group numbers live on bindings."""

    __tablename__ = "spaces"
    __table_args__ = (
        CheckConstraint(uuid4_text36_sql("id"), name="ck_spaces_id"),
        CheckConstraint("enabled IN (0, 1)", name="ck_spaces_enabled"),
        CheckConstraint(
            "autonomous_enabled IN (0, 1)",
            name="ck_spaces_autonomous_enabled",
        ),
        CheckConstraint("require_mention IN (0, 1)", name="ck_spaces_require_mention"),
        CheckConstraint("revision >= 1", name="ck_spaces_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    autonomous_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    require_mention: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SpaceBindingModel(Base):
    """One external space owned by one canonical space."""

    __tablename__ = "space_bindings"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "external_space_id",
            name="uq_space_bindings_platform_space",
        ),
        CheckConstraint(uuid4_text36_sql("id"), name="ck_space_bindings_id"),
        CheckConstraint(uuid4_text36_sql("space_id"), name="ck_space_bindings_space_id"),
        CheckConstraint(canonical_platform_sql("platform"), name="ck_space_bindings_platform"),
        CheckConstraint(
            opaque_external_id_sql("external_space_id"),
            name="ck_space_bindings_external_space_id",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_space_bindings_status",
        ),
        CheckConstraint("revision >= 1", name="ck_space_bindings_revision"),
        Index("ix_space_bindings_space_id", "space_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    space_id: Mapped[str] = mapped_column(
        ForeignKey("spaces.id", ondelete="RESTRICT"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_space_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PresenceModel(Base):
    """One Yuki platform account. Not a Person."""

    __tablename__ = "presences"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "external_account_id",
            name="uq_presences_platform_account",
        ),
        CheckConstraint(uuid4_text36_sql("id"), name="ck_presences_id"),
        CheckConstraint(canonical_platform_sql("platform"), name="ck_presences_platform"),
        CheckConstraint(
            opaque_external_id_sql("external_account_id"),
            name="ck_presences_external_account_id",
        ),
        CheckConstraint("enabled IN (0, 1)", name="ck_presences_enabled"),
        CheckConstraint(
            "ingest_eligible IN (0, 1)",
            name="ck_presences_ingest_eligible",
        ),
        CheckConstraint("revision >= 1", name="ck_presences_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ingest_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdentityRuntimeStateModel(Base):
    """Database singleton for the v1/v2 identity epoch."""

    __tablename__ = "identity_runtime_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_identity_runtime_state_singleton"),
        CheckConstraint(
            "state IN ('v1', 'v2')",
            name="ck_identity_runtime_state_state",
        ),
        CheckConstraint(
            optional_uuid4_text36_sql("cutover_id"),
            name="ck_identity_runtime_state_cutover_id",
        ),
        CheckConstraint(
            "source_fingerprint IS NULL OR length(source_fingerprint) > 0",
            name="ck_identity_runtime_state_source_fingerprint",
        ),
        CheckConstraint("revision >= 1", name="ck_identity_runtime_state_revision"),
        CheckConstraint(
            "("
            "state = 'v1' AND cutover_id IS NULL "
            "AND source_fingerprint IS NULL AND completed_at IS NULL"
            ") OR ("
            "state = 'v2' AND cutover_id IS NOT NULL "
            "AND source_fingerprint IS NOT NULL AND completed_at IS NOT NULL"
            ")",
            name="ck_identity_runtime_state_epoch",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(8), nullable=False)
    cutover_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@event.listens_for(IdentityRuntimeStateModel.__table__, "after_create")
def seed_identity_runtime_state_v1(
    target: Table,
    connection: Connection,
    **_kwargs: object,
) -> None:
    """Seed the singleton when this table is created via metadata.create_all."""

    if target is not IdentityRuntimeStateModel.__table__:
        raise RuntimeError("identity runtime seed is bound only to identity_runtime_state")
    connection.execute(text(_RUNTIME_STATE_V1_SEED_SQL))


class IdentityBackfillRunModel(Base):
    """Local ledger for later dry-run/apply identity backfill progress."""

    __tablename__ = "identity_backfill_runs"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('dry_run', 'apply')",
            name="ck_identity_backfill_runs_mode",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_identity_backfill_runs_status",
        ),
        CheckConstraint(
            "processed_count >= 0 AND persons_count >= 0 "
            "AND identity_bindings_count >= 0 AND spaces_count >= 0 "
            "AND space_bindings_count >= 0 AND presences_count >= 0 "
            "AND conflicts_count >= 0 AND skipped_count >= 0",
            name="ck_identity_backfill_runs_counts",
        ),
        CheckConstraint(
            "checkpoint IS NULL OR length(checkpoint) > 0",
            name="ck_identity_backfill_runs_checkpoint",
        ),
        CheckConstraint(
            "error_category IS NULL OR length(error_category) > 0",
            name="ck_identity_backfill_runs_error_category",
        ),
        CheckConstraint(
            "("
            "status = 'pending' AND started_at IS NULL AND finished_at IS NULL "
            "AND error_category IS NULL"
            ") OR ("
            "status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL"
            ") OR ("
            "status IN ('succeeded', 'cancelled') AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL AND error_category IS NULL"
            ") OR ("
            "status = 'failed' AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL AND error_category IS NOT NULL"
            ")",
            name="ck_identity_backfill_runs_lifecycle",
        ),
        Index("ix_identity_backfill_runs_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    checkpoint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    persons_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    identity_bindings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spaces_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    space_bindings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    presences_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conflicts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdentityConflictModel(Base):
    """Idempotent unclassifiable or ambiguous identity objects."""

    __tablename__ = "identity_conflicts"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "external_id",
            "subject_kind",
            "conflict_kind",
            name="uq_identity_conflicts_subject",
        ),
        CheckConstraint(canonical_platform_sql("platform"), name="ck_identity_conflicts_platform"),
        CheckConstraint(
            opaque_external_id_sql("external_id"),
            name="ck_identity_conflicts_external_id",
        ),
        CheckConstraint(
            "subject_kind IN ('account', 'space')",
            name="ck_identity_conflicts_subject_kind",
        ),
        CheckConstraint(
            "conflict_kind IN ('ambiguous_identity', 'unclassified')",
            name="ck_identity_conflicts_kind",
        ),
        CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_identity_conflicts_status",
        ),
        CheckConstraint(
            "("
            "status = 'open' AND resolved_at IS NULL"
            ") OR ("
            "status = 'resolved' AND resolved_at IS NOT NULL"
            ")",
            name="ck_identity_conflicts_lifecycle",
        ),
        CheckConstraint(
            "error_category IS NULL OR length(error_category) > 0",
            name="ck_identity_conflicts_error_category",
        ),
        Index("ix_identity_conflicts_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    conflict_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


_C5_PARENT_TABLES: tuple[str, ...] = ("persons", "spaces")


def _is_sqlite_connection(connection: Connection) -> bool:
    return connection.dialect.name == "sqlite"


def _c5_hosts_and_parents_ready(connection: Connection) -> bool:
    required = (*C5_OWNERSHIP_TABLES, *_C5_PARENT_TABLES)
    present = connection.execute(
        text(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name IN "
            f"({', '.join(repr(name) for name in required)})"
        )
    ).scalar()
    if int(present or 0) != len(required):
        return False
    for table, columns in C5_OWNERSHIP_COLUMNS.items():
        info = {str(row[1]) for row in connection.execute(text(f'PRAGMA table_info("{table}")'))}
        if not set(columns) <= info:
            return False
    return True


def _install_c5_triggers_if_ready(connection: Connection) -> None:
    """Install ownership-shadow guards once every C5 host and parent exists."""

    if not _is_sqlite_connection(connection):
        return
    if not _c5_hosts_and_parents_ready(connection):
        return
    installed = connection.execute(
        text("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = :name"),
        {"name": C5_TRIGGER_NAMES[0]},
    ).scalar()
    if installed is not None:
        return
    for statement in C5_TRIGGER_SQL:
        connection.execute(text(statement))


@event.listens_for(Base.metadata, "after_create")
def _install_c5_triggers_after_metadata_create(
    target: MetaData,
    connection: Connection,
    **_kwargs: object,
) -> None:
    """Install C5 triggers after create_all, independent of table order."""

    if target is not Base.metadata:
        return
    if not _is_sqlite_connection(connection):
        return
    _install_c5_triggers_if_ready(connection)
