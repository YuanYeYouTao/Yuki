"""Bot-owned source anchors and durable sandbox completion inbox."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text
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
    progress_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SandboxTaskContinuationModel(Base):
    """Separate completion receipt from the decision to execute another Agent turn."""

    __tablename__ = "sandbox_task_continuations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('ready','claimed','observed','finished','uncertain','blocked')",
            name="ck_sandbox_continuation_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_sandbox_continuation_attempts"),
    )

    request_id: Mapped[str] = mapped_column(
        ForeignKey("sandbox_task_runs.request_id", ondelete="RESTRICT"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(36))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason: Mapped[str | None] = mapped_column(String(64))
    outcome_json: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
