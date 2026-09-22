"""Host-owned participation admission records; no Agent permissions or chat content."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class AutonomyBindingModel(Base):
    __tablename__ = "autonomy_bindings"
    __table_args__ = (
        CheckConstraint("generation >= 1 AND controller_epoch >= 0 AND revision >= 1"),
        CheckConstraint("effective_owner IN ('off', 'legacy', 'semantic')"),
        CheckConstraint(
            "(master_enabled = 0 AND effective_owner = 'off') OR "
            "(master_enabled = 1 AND effective_owner IN ('legacy', 'semantic'))"
        ),
        CheckConstraint("effective_owner != 'semantic' OR external_enabled = 1"),
    )

    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_conversations.id", ondelete="RESTRICT"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    external_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    effective_owner: Mapped[str] = mapped_column(String(16), nullable=False, default="off")
    controller_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    fallback_reason: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InitiativeRunModel(Base):
    __tablename__ = "autonomy_initiative_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "generation"],
            ["autonomy_bindings.conversation_id", "autonomy_bindings.generation"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "conversation_id",
            "generation",
            "owner",
            "controller_epoch",
            "proposal_id",
            name="uq_autonomy_proposal_run",
        ),
        CheckConstraint("generation >= 1 AND controller_epoch >= 0 AND feedback_sequence >= 0"),
        CheckConstraint("owner IN ('legacy', 'semantic')"),
        CheckConstraint(
            "state IN ('accepted', 'running', 'completed', 'no_reply', 'interrupted', 'failed')"
        ),
        Index("ix_autonomy_runs_active", "conversation_id", "generation", "state"),
        Index(
            "uq_autonomy_runs_one_active",
            "conversation_id",
            "generation",
            unique=True,
            sqlite_where=text("state IN ('accepted', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    owner: Mapped[str] = mapped_column(String(16), nullable=False)
    controller_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    space_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("spaces.id", ondelete="RESTRICT"), nullable=False
    )
    presence_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("presences.id", ondelete="RESTRICT"), nullable=False
    )
    target_person_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("persons.id", ondelete="RESTRICT")
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sources_json: Mapped[str] = mapped_column(Text, nullable=False)
    support_refs_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="accepted")
    feedback_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InitiativeSourceClaimModel(Base):
    """Accepted focus sources shared by both proposers, including silent outcomes."""

    __tablename__ = "autonomy_source_claims"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "generation"],
            ["autonomy_bindings.conversation_id", "autonomy_bindings.generation"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("source_kind IN ('event', 'memory')"),
        Index("ix_autonomy_sources_run", "run_id"),
    )

    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    source_revision: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("autonomy_initiative_runs.id", ondelete="RESTRICT"), nullable=False
    )


class InitiativeFeedbackModel(Base):
    __tablename__ = "autonomy_initiative_feedback"
    __table_args__ = (CheckConstraint("sequence >= 1"),)

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("autonomy_initiative_runs.id", ondelete="RESTRICT"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
