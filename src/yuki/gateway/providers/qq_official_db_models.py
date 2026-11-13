"""Durable QQ official message facts the platform requires for later deliveries.

One row per platform message seen or accepted by one Bot Presence. Inbound rows keep
the REFIDX_ the platform uses for quotes and serve as passive ``msg_id`` anchors;
outbound rows are the delivery records that ``msg_seq`` and own-echo recognition
follow, so neither restarts from memory.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from yuki.persistence.models import Base


class QQOfficialMessageModel(Base):
    __tablename__ = "qq_official_messages"
    __table_args__ = (
        Index(
            "ix_qq_official_messages_ref",
            "presence_id",
            "ref_idx",
            sqlite_where=text("ref_idx IS NOT NULL"),
        ),
        Index(
            "ix_qq_official_messages_reply",
            "presence_id",
            "reply_to",
            "msg_seq",
            sqlite_where=text("reply_to IS NOT NULL"),
        ),
        Index(
            "ix_qq_official_messages_inbound",
            "presence_id",
            "chat_kind",
            "chat_id",
            "created_at",
            sqlite_where=text("direction = 'inbound'"),
        ),
    )

    presence_id: Mapped[str] = mapped_column(
        ForeignKey("account_assignments.id", ondelete="RESTRICT"), primary_key=True
    )
    message_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    chat_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(255), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    ref_idx: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reply_to: Mapped[str | None] = mapped_column(String(255), nullable=True)
    msg_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
