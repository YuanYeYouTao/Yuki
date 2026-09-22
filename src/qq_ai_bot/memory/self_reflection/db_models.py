"""Independent receipt cursors for silent SELF initiatives; never chat event IDs."""

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class InitiativeReflectionCursorModel(Base):
    __tablename__ = "memory_initiative_reflection_cursors"
    initiative_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("autonomy_initiative_runs.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    last_receipt_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class InitiativeReflectionWindowModel(Base):
    __tablename__ = "memory_initiative_reflection_windows"
    __table_args__ = (UniqueConstraint("initiative_run_id", "first_receipt_id"),)
    reflection_run_id: Mapped[int] = mapped_column(
        ForeignKey("memory_self_reflection_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    initiative_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("autonomy_initiative_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    first_receipt_id: Mapped[int] = mapped_column(Integer, nullable=False)
    last_receipt_id: Mapped[int] = mapped_column(Integer, nullable=False)
