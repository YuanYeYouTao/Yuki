"""Content-free durable receipts for non-idempotent social effects."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class SocialOperationModel(Base):
    __tablename__ = "social_operation_receipts"
    __table_args__ = (
        UniqueConstraint("source_turn_id", "tool_call_id", name="uq_social_operation_call"),
        CheckConstraint(
            "status IN ('prepared','executing','succeeded','failed','uncertain')",
            name="ck_social_operation_status",
        ),
        CheckConstraint("target_kind IN ('person','space')", name="ck_social_target_kind"),
        Index("ix_social_operation_target_time", "target_id", "created_at"),
        Index("ix_social_operation_event_id", "event_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_conversation_id: Mapped[str] = mapped_column(
        ForeignKey("canonical_conversations.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    presence_id: Mapped[str | None] = mapped_column(
        ForeignKey("presences.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    platform_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True
    )
