"""Personal chat credentials, separate from operators and device executors."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base

APP_PLATFORM = "app"
APP_ACCOUNT_ID = "yuki-app"


class ClientDeviceModel(Base):
    __tablename__ = "client_devices"
    __table_args__ = (Index("ix_client_devices_person", "person_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClientLinkCodeModel(Base):
    __tablename__ = "client_link_codes"
    __table_args__ = (Index("ix_client_link_codes_expiry", "expires_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    issuer_principal_id: Mapped[str] = mapped_column(String(36))
    issuer_source: Mapped[str] = mapped_column(String(32))
    source_binding_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
