"""Purpose-bound personal credentials, independent of chat and device transports."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from yuki.persistence.models import Base

APP_PLATFORM = "app"
APP_ACCOUNT_ID = "yuki-app"
ACP_PLATFORM = "acp"
ACP_ACCOUNT_ID = "yuki-acp"


class ClientDeviceModel(Base):
    __tablename__ = "client_devices"
    __table_args__ = (Index("ix_client_devices_person", "person_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[str] = mapped_column(String(16), default="chat", server_default="chat")
    label: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClientLinkCodeModel(Base):
    __tablename__ = "client_link_codes"
    __table_args__ = (Index("ix_client_link_codes_expiry", "expires_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[str] = mapped_column(String(16), default="chat", server_default="chat")
    person_id: Mapped[str] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    issuer_principal_id: Mapped[str] = mapped_column(String(36))
    issuer_source: Mapped[str] = mapped_column(String(32))
    source_binding_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
