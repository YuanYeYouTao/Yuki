"""Bounded, replaceable model-input views; the event ledger remains authoritative."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, event, inspect
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import MetaData

from qq_ai_bot.conversation.projection_schema import PROJECTION_TRIGGERS_0054
from qq_ai_bot.persistence.models import Base


class PromptProjectionModel(Base):
    __tablename__ = "prompt_projections"
    __table_args__ = (
        CheckConstraint("revision >= 1 AND byte_size >= 2", name="ck_prompt_projection_size"),
    )

    view_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("canonical_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_after_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    epoch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    context_key: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    rebuild_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    invalidated_reason: Mapped[str | None] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@event.listens_for(Base.metadata, "after_create")
def install_projection_triggers(
    metadata: MetaData, connection: Connection, **kwargs: object
) -> None:
    if connection.dialect.name == "sqlite" and inspect(connection).has_table("prompt_projections"):
        for statement in PROJECTION_TRIGGERS_0054.values():
            connection.exec_driver_sql(
                statement.replace("CREATE TRIGGER", "CREATE TRIGGER IF NOT EXISTS", 1)
            )
