"""Frozen local-executor tables retained for original receipt history only.

No live executor service writes or dispatches these records. Migration 0152 and
the original identities remain intact for Work recovery and existing data.
"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from yuki.persistence.models import Base


class ExecutorModel(Base):
    __tablename__ = "client_executors"
    __table_args__ = (Index("ix_client_executors_person", "person_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(128))
    platform: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExecutorGrantModel(Base):
    __tablename__ = "executor_grants"
    executor_id: Mapped[str] = mapped_column(
        ForeignKey("client_executors.id", ondelete="CASCADE"), primary_key=True
    )
    capability: Mapped[str] = mapped_column(String, primary_key=True)
    scope_alias: Mapped[str] = mapped_column(String, primary_key=True, default="")
    scope_digest: Mapped[str] = mapped_column(String, default="")
    revision: Mapped[int] = mapped_column(Integer)


class ExecutorOperationModel(Base):
    __tablename__ = "executor_operations"
    __table_args__ = (
        Index("ix_executor_operations_history", "executor_id", "created_at", "op_id"),
        CheckConstraint(
            "state IN ('registered','dispatched','acknowledged','awaiting_user',"
            "'succeeded','failed','unknown')",
            name="ck_executor_operation_state",
        ),
    )
    op_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    executor_id: Mapped[str | None] = mapped_column(
        ForeignKey("client_executors.id", ondelete="SET NULL")
    )
    person_id: Mapped[str | None] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"))
    work_id: Mapped[str | None] = mapped_column(String)
    tool_call_key: Mapped[str] = mapped_column(String, unique=True)
    capability: Mapped[str | None] = mapped_column(String)
    params_json: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String)
    level: Mapped[str | None] = mapped_column(String)
    result_json: Mapped[str | None] = mapped_column(Text)
    observation_json: Mapped[str | None] = mapped_column(Text)
    receipt_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
