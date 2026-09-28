"""Append-only, independently expiring execution evidence."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, LargeBinary, String, text
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class ExecutionTraceStateModel(Base):
    """Privacy generation fences diagnostics still running during an erasure."""

    __tablename__ = "execution_trace_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    privacy_generation: Mapped[int] = mapped_column(Integer, nullable=False)


class ExecutionTraceEntryModel(Base):
    __tablename__ = "execution_trace_entries"
    __table_args__ = (
        Index("ix_execution_trace_conversation_id", "conversation_id", "id"),
        Index(
            "ix_execution_trace_roots",
            "conversation_id",
            "id",
            sqlite_where=text("kind IN ('chat_processing_start', 'turn_start')"),
        ),
        Index(
            "ix_execution_trace_source_event",
            "source_event_id",
            "id",
            sqlite_where=text("source_event_id IS NOT NULL"),
        ),
        Index("ix_execution_trace_turn_id", "turn_id", "id"),
        Index("ix_execution_trace_work_id", "work_id", "id"),
        Index("ix_execution_trace_operation_id", "operation_id", "id"),
        Index("ix_execution_trace_delivered_event", "delivered_event_id", "id"),
        Index("ix_execution_trace_expires", "expires_at", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("canonical_conversations.id", ondelete="CASCADE"), nullable=True
    )
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    work_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    activation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="SET NULL"), nullable=True
    )
    generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    origin: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload_gzip: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    payload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_event_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "chat_events.id", name="fk_execution_trace_delivered_event", ondelete="SET NULL"
        ),
        nullable=True,
    )
