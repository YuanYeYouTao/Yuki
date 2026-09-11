"""Bot-owned source anchors and durable sandbox completion inbox."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class SandboxTaskRunModel(Base):
    __tablename__ = "sandbox_task_runs"
    __table_args__ = (
        CheckConstraint("status IN ('waiting','completed')", name="ck_sandbox_task_run_status"),
    )

    request_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    source_conversation_id: Mapped[str] = mapped_column(
        ForeignKey("canonical_conversations.id", ondelete="RESTRICT"), nullable=False
    )
    source_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    completion_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
