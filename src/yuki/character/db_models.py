"""One character per database; owners remain canonical Persons."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column

from yuki.persistence.models import Base


class CharacterProfileModel(Base):
    __tablename__ = "character_profile"
    __table_args__ = (CheckConstraint("singleton = 1", name="ck_character_singleton"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    singleton: Mapped[int] = mapped_column(Integer, unique=True, default=1)
    slug: Mapped[str] = mapped_column(String, default="yuki")
    display_name: Mapped[str] = mapped_column(String, default="Yuki")
    bio: Mapped[str] = mapped_column(Text, default="")
    accent_light: Mapped[str] = mapped_column(String(7), default="#3A6BD6")
    accent_dark: Mapped[str] = mapped_column(String(7), default="#7FA4FF")
    avatar_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    default_avatar_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_revision: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CharacterOwnerGrantModel(Base):
    __tablename__ = "character_owner_grants"

    person_id: Mapped[str] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), primary_key=True
    )
    granted_by_principal: Mapped[str] = mapped_column(String(36))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


@event.listens_for(CharacterProfileModel.__table__, "after_create")
def seed_test_character(target: Any, connection: Any, **_kwargs: Any) -> None:
    """create_schema is test-only; deployment seeding belongs to frozen migrations."""
    now = datetime.now(UTC)
    connection.execute(target.insert().values(id=str(uuid4()), created_at=now, updated_at=now))
