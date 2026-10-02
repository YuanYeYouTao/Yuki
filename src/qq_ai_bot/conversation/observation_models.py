"""Trusted observation sources and ordered, dispatched model representations."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class ContextObservationModel(Base):
    __tablename__ = "model_context_observations"
    __table_args__ = (
        UniqueConstraint("source_key", name="uq_context_observation_source"),
        Index(
            "ix_context_observation_scope",
            "conversation_id",
            "generation",
            "actor_id",
            "read_scope",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("canonical_conversations.id", ondelete="CASCADE"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    read_scope: Mapped[str] = mapped_column(Text, nullable=False)
    source_key: Mapped[str] = mapped_column(String(256), nullable=False)
    source_work_id: Mapped[str | None] = mapped_column(
        ForeignKey("runtime_work.id", ondelete="SET NULL")
    )
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    privacy_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    parent_sources_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    summary_view_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContextSelectionModel(Base):
    """A selected representation, not a second event or delivery ledger.

    payload_json freezes the model representation (including dynamic envelopes),
    while event_ids_json and observation_sources_json retain its original owners.
    Order is allocated only inside the admitted projection's CAS transaction.
    """

    __tablename__ = "model_context_selections"
    __table_args__ = (
        UniqueConstraint("view_key", "source_key", name="uq_context_selection_source"),
        Index("ix_context_selection_view", "view_key", "id"),
        Index("ix_context_selection_scope", "conversation_id", "generation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    view_key: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("canonical_conversations.id", ondelete="CASCADE"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    read_scope: Mapped[str] = mapped_column(Text, nullable=False)
    source_key: Mapped[str] = mapped_column(String(256), nullable=False)
    event_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    observation_sources_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
