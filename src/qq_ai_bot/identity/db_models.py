"""SQLAlchemy models for Yuki's canonical identity foundation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.identity.sql_constraints import uuid4_text36_sql
from qq_ai_bot.persistence.models import Base

CANONICAL_IDENTITY_TABLES: tuple[str, ...] = (
    "persons",
    "identity_bindings",
    "spaces",
    "space_bindings",
    "presences",
)
CANONICAL_IDENTITY_CREATE_ORDER: tuple[str, ...] = (
    "persons",
    "spaces",
    "presences",
    "identity_bindings",
    "space_bindings",
)


def canonical_platform_sql(column: str) -> str:
    """Return the invariant for a non-empty lowercase platform token."""

    return (
        f"length({column}) > 0 AND length({column}) <= 32 "
        f"AND {column} = lower({column}) AND {column} = trim({column})"
    )


def opaque_external_id_sql(column: str) -> str:
    """Return the invariant for a trimmed, case-preserving external ID."""

    return f"length({column}) > 0 AND length({column}) <= 255 AND {column} = trim({column})"


class CanonicalPersonModel(Base):
    """Permanent human subject; platform display names live on bindings."""

    __tablename__ = "persons"
    __table_args__ = (
        CheckConstraint(uuid4_text36_sql("id"), name="ck_persons_id"),
        CheckConstraint("enabled IN (0, 1)", name="ck_persons_enabled"),
        CheckConstraint("revision >= 1", name="ck_persons_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revision: Mapped[int] = mapped_column(nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdentityBindingModel(Base):
    """One external human account owned by one permanent Person."""

    __tablename__ = "identity_bindings"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "external_account_id",
            name="uq_identity_bindings_platform_account",
        ),
        CheckConstraint(uuid4_text36_sql("id"), name="ck_identity_bindings_id"),
        CheckConstraint(uuid4_text36_sql("person_id"), name="ck_identity_bindings_person_id"),
        CheckConstraint(canonical_platform_sql("platform"), name="ck_identity_bindings_platform"),
        CheckConstraint(
            opaque_external_id_sql("external_account_id"),
            name="ck_identity_bindings_external_account_id",
        ),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_identity_bindings_status"),
        CheckConstraint("revision >= 1", name="ck_identity_bindings_revision"),
        CheckConstraint("first_seen_at <= last_seen_at", name="ck_identity_bindings_seen_range"),
        Index("ix_identity_bindings_person_id", "person_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    person_id: Mapped[str] = mapped_column(
        ForeignKey("persons.id", onupdate="RESTRICT", ondelete="RESTRICT"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    revision: Mapped[int] = mapped_column(nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CanonicalSpaceModel(Base):
    """Permanent shared-space subject; platform group IDs live on bindings."""

    __tablename__ = "spaces"
    __table_args__ = (
        CheckConstraint(uuid4_text36_sql("id"), name="ck_spaces_id"),
        CheckConstraint("enabled IN (0, 1)", name="ck_spaces_enabled"),
        CheckConstraint("autonomous_enabled IN (0, 1)", name="ck_spaces_autonomous_enabled"),
        CheckConstraint("require_mention IN (0, 1)", name="ck_spaces_require_mention"),
        CheckConstraint("revision >= 1", name="ck_spaces_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    autonomous_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    require_mention: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revision: Mapped[int] = mapped_column(nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SpaceBindingModel(Base):
    """One external shared space owned by one permanent Space."""

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
        CheckConstraint("status IN ('active', 'disabled')", name="ck_space_bindings_status"),
        CheckConstraint("revision >= 1", name="ck_space_bindings_revision"),
        CheckConstraint("first_seen_at <= last_seen_at", name="ck_space_bindings_seen_range"),
        Index("ix_space_bindings_space_id", "space_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    space_id: Mapped[str] = mapped_column(
        ForeignKey("spaces.id", onupdate="RESTRICT", ondelete="RESTRICT"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_space_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    revision: Mapped[int] = mapped_column(nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PresenceModel(Base):
    """One Yuki platform account; a Presence is never a Person."""

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
        CheckConstraint("ingest_eligible IN (0, 1)", name="ck_presences_ingest_eligible"),
        CheckConstraint("revision >= 1", name="ck_presences_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ingest_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revision: Mapped[int] = mapped_column(nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
