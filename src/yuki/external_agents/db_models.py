"""Native resource references and transport receipts, not another Work state machine."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from yuki.persistence.models import Base


class AgentInstanceModel(Base):
    __tablename__ = "external_agent_instances"
    __table_args__ = (UniqueConstraint("device_id", "local_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("client_devices.id", ondelete="CASCADE"), index=True
    )
    local_id: Mapped[str] = mapped_column(Text)
    catalog_json: Mapped[str] = mapped_column(Text)


class AgentSessionModel(Base):
    __tablename__ = "external_agent_sessions"
    __table_args__ = (UniqueConstraint("instance_id", "native_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        ForeignKey("external_agent_instances.id", ondelete="CASCADE"), index=True
    )
    native_id: Mapped[str] = mapped_column(Text)


class AgentExecutionModel(Base):
    """A native execution can exist before Yuki creates a supervision relationship."""

    __tablename__ = "external_agent_executions"
    __table_args__ = (UniqueConstraint("session_id", "native_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("external_agent_sessions.id", ondelete="CASCADE"), index=True
    )
    native_id: Mapped[str] = mapped_column(Text)


class AgentOperationModel(Base):
    """One original dispatch and its native receipt; Work owns business decisions."""

    __tablename__ = "external_agent_operations"
    __table_args__ = (
        Index("ix_external_agent_operations_session", "session_id", "action"),
        Index("ix_external_agent_operations_work", "work_id"),
    )

    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        ForeignKey("external_agent_instances.id", ondelete="CASCADE")
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("external_agent_sessions.id", ondelete="CASCADE"), nullable=True
    )
    actor_person_id: Mapped[str] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    work_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_json: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(16))
    payload_hash: Mapped[str] = mapped_column(String(64))
    command_json: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    native_execution_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentEventModel(Base):
    """Only important facts are durable; token deltas are not a second transcript."""

    __tablename__ = "external_agent_events"
    __table_args__ = (
        UniqueConstraint("device_id", "event_id"),
        Index("ix_external_agent_events_operation", "operation_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("client_devices.id", ondelete="CASCADE"))
    event_id: Mapped[str] = mapped_column(Text)
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("external_agent_operations.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
