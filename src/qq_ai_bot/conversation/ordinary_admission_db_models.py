"""A committed human admission prevents a second activation of the same event."""

from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class OrdinaryTurnAdmissionModel(Base):
    __tablename__ = "ordinary_turn_admissions"
    __table_args__ = (
        CheckConstraint("generation >= 1 AND coordinator_version >= 0"),
        CheckConstraint("route IN ('ordinary', 'work')"),
    )

    event_id: Mapped[int] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), primary_key=True
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("canonical_conversations.id", ondelete="CASCADE"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    source_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_person_id: Mapped[str] = mapped_column(String(36), nullable=False)
    presence_id: Mapped[str] = mapped_column(String(36), nullable=False)
    activation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    coordinator_version: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_key: Mapped[str | None] = mapped_column(String(256))
    target_hint: Mapped[str | None] = mapped_column(String(256))
    basis_json: Mapped[str] = mapped_column(Text, nullable=False)
    route: Mapped[str] = mapped_column(String(16), nullable=False)
    work_id: Mapped[str | None] = mapped_column(ForeignKey("runtime_work.id", ondelete="SET NULL"))
    input_id: Mapped[int | None] = mapped_column(
        ForeignKey("runtime_work_inputs.id", ondelete="SET NULL")
    )
    created: Mapped[float] = mapped_column(Float, nullable=False)
