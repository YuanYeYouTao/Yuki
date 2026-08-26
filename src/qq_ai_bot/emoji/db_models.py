"""SQLAlchemy models owned by the emoji domain."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class EmojiAssetModel(Base):
    __tablename__ = "emoji_assets"
    __table_args__ = (
        UniqueConstraint("sha256", name="uq_emoji_assets_sha256"),
        UniqueConstraint("relative_path", name="uq_emoji_assets_relative_path"),
        CheckConstraint(
            "status IN ('candidate', 'recognized', 'adopted', 'rejected', 'banned', 'missing')",
            name="ck_emoji_assets_status",
        ),
        CheckConstraint("byte_size > 0", name="ck_emoji_assets_byte_size"),
        CheckConstraint("width > 0 AND height > 0", name="ck_emoji_assets_dimensions"),
        CheckConstraint("frame_count > 0", name="ck_emoji_assets_frame_count"),
        CheckConstraint("seen_count >= 1", name="ck_emoji_assets_seen_count"),
        CheckConstraint("use_count >= 0", name="ck_emoji_assets_use_count"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_emoji_assets_confidence"),
        CheckConstraint("intensity >= 0 AND intensity <= 1", name="ck_emoji_assets_intensity"),
        Index("ix_emoji_assets_status_updated", "status", "updated_at"),
        Index("ix_emoji_assets_last_seen", "last_seen_at"),
        Index("ix_emoji_assets_perceptual_hash", "perceptual_hash"),
        Index("ix_emoji_assets_canonical_first_seen_person_id", "canonical_first_seen_person_id"),
        Index("ix_emoji_assets_canonical_first_seen_space_id", "canonical_first_seen_space_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    perceptual_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    preview_relative_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_format: Mapped[str] = mapped_column(String(16), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_count: Mapped[int] = mapped_column(Integer, nullable=False)
    animated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    emotion_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    usage_scenarios_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    ocr_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    intensity: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    analysis_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="SET NULL"), nullable=True
    )
    first_seen_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_seen_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_sub_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_emoji_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    source_package_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canonical_first_seen_person_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("persons.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_first_seen_space_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("spaces.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )


class EmojiScopeStateModel(Base):
    __tablename__ = "emoji_scope_states"
    __table_args__ = (
        Index(
            "uq_emoji_scope_state_global",
            "emoji_id",
            unique=True,
            sqlite_where=text("scope_type = 'global'"),
        ),
        Index(
            "uq_emoji_scope_state_space",
            "emoji_id",
            "canonical_space_id",
            unique=True,
            sqlite_where=text("scope_type = 'group'"),
        ),
        CheckConstraint("scope_type IN ('global', 'group')", name="ck_emoji_scope_scope_type"),
        CheckConstraint(
            "(scope_type = 'global' AND canonical_space_id IS NULL) OR "
            "(scope_type = 'group' AND canonical_space_id IS NOT NULL)",
            name="ck_emoji_scope_owner",
        ),
        CheckConstraint("weight >= 0", name="ck_emoji_scope_weight"),
        Index("ix_emoji_scope_lookup", "scope_type", "canonical_space_id", "enabled"),
        Index("ix_emoji_scope_states_canonical_space_id", "canonical_space_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    emoji_id: Mapped[str] = mapped_column(
        ForeignKey("emoji_assets.id", ondelete="CASCADE"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    adopted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canonical_space_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("spaces.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )


class EmojiJobModel(Base):
    __tablename__ = "emoji_jobs"
    __table_args__ = (
        CheckConstraint(
            "job_type IN ('analyze', 'reanalyze', 'rebuild_preview')",
            name="ck_emoji_jobs_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_emoji_jobs_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_emoji_jobs_attempts"),
        Index("ix_emoji_jobs_status_next", "status", "next_attempt_at"),
        Index(
            "uq_emoji_jobs_active",
            "emoji_id",
            "job_type",
            unique=True,
            sqlite_where=text("status IN ('pending', 'processing')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    emoji_id: Mapped[str] = mapped_column(
        ForeignKey("emoji_assets.id", ondelete="CASCADE"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EmojiUsageEventModel(Base):
    __tablename__ = "emoji_usage_events"
    __table_args__ = (
        Index("ix_emoji_usage_asset_created", "emoji_id", "created_at"),
        Index("ix_emoji_usage_scope_created", "group_id", "created_at"),
        Index("ix_emoji_usage_events_canonical_actor_person_id", "canonical_actor_person_id"),
        Index("ix_emoji_usage_events_canonical_space_id", "canonical_space_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    emoji_id: Mapped[str] = mapped_column(
        ForeignKey("emoji_assets.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trigger_message_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canonical_actor_person_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("persons.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_space_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("spaces.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
