"""SQLAlchemy models for Yuki's canonical identity foundation."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.selectable import ScalarSelect

from yuki.identity.sql_constraints import uuid4_text36_sql
from yuki.persistence.models import Base

CANONICAL_IDENTITY_TABLES: tuple[str, ...] = (
    "persons",
    "accounts",
    "account_assignments",
    "spaces",
    "space_bindings",
    "account_space_profiles",
    "account_name_observations",
)
CANONICAL_IDENTITY_CREATE_ORDER: tuple[str, ...] = (
    "persons",
    "accounts",
    "spaces",
    "space_bindings",
    "account_assignments",
    "account_space_profiles",
    "account_name_observations",
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
    """Permanent human identity; merged IDs remain the original facts' subjects."""

    __tablename__ = "persons"
    __table_args__ = (
        CheckConstraint(uuid4_text36_sql("id"), name="ck_persons_id"),
        CheckConstraint("enabled IN (0, 1)", name="ck_persons_enabled"),
        CheckConstraint("revision >= 1", name="ck_persons_revision"),
        Index("ix_persons_merged_into_person_id", "merged_into_person_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    merged_into_person_id: Mapped[str | None] = mapped_column(
        ForeignKey("persons.id", onupdate="RESTRICT", ondelete="RESTRICT"), nullable=True
    )
    display_name: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default=""
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_superuser: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    revision: Mapped[int] = mapped_column(nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AccountModel(Base):
    """One stable physical account, independently of its current owner."""

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("platform", "external_account_id", name="uq_accounts_native_key"),
        CheckConstraint(uuid4_text36_sql("id"), name="ck_accounts_id"),
        CheckConstraint(canonical_platform_sql("platform"), name="ck_accounts_platform"),
        CheckConstraint(
            opaque_external_id_sql("external_account_id"), name="ck_accounts_native_id"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AccountAssignmentModel(Base):
    """The only account ownership fact; ended stages retain their original IDs."""

    __tablename__ = "account_assignments"
    __table_args__ = (
        CheckConstraint(uuid4_text36_sql("id"), name="ck_account_assignments_id"),
        CheckConstraint("kind IN ('person', 'yuki')", name="ck_account_assignments_kind"),
        CheckConstraint(
            "(kind='person' AND person_id IS NOT NULL) OR (kind='yuki' AND person_id IS NULL)",
            name="ck_account_assignments_owner",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled', 'ended')", name="ck_account_assignments_status"
        ),
        CheckConstraint("revision >= 1", name="ck_account_assignments_revision"),
        Index(
            "uq_account_assignments_current",
            "account_id",
            unique=True,
            sqlite_where=text("status <> 'ended'"),
        ),
        Index("ix_account_assignments_person", "person_id", "id"),
        Index("ix_account_assignments_history", "account_id", "started_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(16))
    person_id: Mapped[str | None] = mapped_column(ForeignKey("persons.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    revision: Mapped[int] = mapped_column(default=1)
    ingest_eligible: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    account: Mapped[AccountModel] = relationship(lazy="joined")
    __mapper_args__ = {"polymorphic_on": kind}  # noqa: RUF012

    @hybrid_property
    def platform(self) -> str:
        return self.account.platform

    @platform.inplace.expression
    @classmethod
    def _platform_expression(cls) -> ScalarSelect[str]:
        return (
            select(AccountModel.platform).where(AccountModel.id == cls.account_id).scalar_subquery()
        )

    @hybrid_property
    def external_account_id(self) -> str:
        return self.account.external_account_id

    @external_account_id.inplace.expression
    @classmethod
    def _external_account_expression(cls) -> ScalarSelect[str]:
        return (
            select(AccountModel.external_account_id)
            .where(AccountModel.id == cls.account_id)
            .scalar_subquery()
        )

    @hybrid_property
    def display_name(self) -> str:
        return self.account.display_name

    @display_name.inplace.setter
    def _set_display_name(self, value: str) -> None:
        self.account.display_name = value

    @display_name.inplace.expression
    @classmethod
    def _display_name_expression(cls) -> ScalarSelect[str]:
        return (
            select(AccountModel.display_name)
            .where(AccountModel.id == cls.account_id)
            .scalar_subquery()
        )

    @hybrid_property
    def history_starts_at(self) -> datetime | None:
        return self.started_at

    @history_starts_at.inplace.setter
    def _set_history_start(self, value: datetime | None) -> None:
        self.started_at = value

    @history_starts_at.inplace.expression
    @classmethod
    def _history_start_expression(cls) -> ColumnElement[datetime | None]:
        return cls.started_at.expression


class IdentityBindingModel(AccountAssignmentModel):
    """The Person projection of the one Assignment table."""

    person_id: Mapped[str] = mapped_column(use_existing_column=True, nullable=True)
    __mapper_args__ = {"polymorphic_identity": "person"}  # noqa: RUF012


class PresenceModel(AccountAssignmentModel):
    """The Yuki projection of the one Assignment table."""

    __mapper_args__ = {"polymorphic_identity": "yuki"}  # noqa: RUF012

    @hybrid_property
    def enabled(self) -> bool:
        return self.status == "active"

    @enabled.inplace.setter
    def _set_enabled(self, value: bool) -> None:
        self.status = "active" if value else "disabled"

    @enabled.inplace.expression
    @classmethod
    def _enabled_expression(cls) -> ColumnElement[bool]:
        return cls.status == "active"


class AccountSpaceProfileModel(Base):
    """An observed account name in a physical platform space."""

    __tablename__ = "account_space_profiles"
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    space_binding_id: Mapped[str] = mapped_column(
        ForeignKey("space_bindings.id", ondelete="CASCADE"), primary_key=True
    )
    display_name: Mapped[str] = mapped_column(String(128), default="", server_default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_account_space_profiles_space", "space_binding_id", "account_id"),)


class AccountNameObservationModel(Base):
    """A real name observation; never a permission or an identity cache."""

    __tablename__ = "account_name_observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    assignment_id: Mapped[str | None] = mapped_column(
        ForeignKey("account_assignments.id", ondelete="CASCADE")
    )
    space_binding_id: Mapped[str | None] = mapped_column(
        ForeignKey("space_bindings.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32), default="nickname")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_account_names_scope_account", "space_binding_id", "account_id", "assignment_id"),
        Index("ix_account_names_name_scope_account", "name", "space_binding_id", "account_id"),
        Index(
            "uq_account_names_observation",
            "account_id",
            func.coalesce(assignment_id, ""),
            func.coalesce(space_binding_id, ""),
            "name",
            unique=True,
        ),
    )


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
