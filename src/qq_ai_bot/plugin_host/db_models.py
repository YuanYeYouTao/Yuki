"""Host-owned SQLAlchemy models for Plugin API 3.0."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
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


class PluginInstallationModel(Base):
    """One discovered plugin and its explicit approval state."""

    __tablename__ = "plugin_installations"
    __table_args__ = (
        CheckConstraint("failure_count >= 0", name="ck_plugin_installations_failure_count"),
        CheckConstraint(
            "status IN ('discovered', 'invalid', 'pending_approval', 'approved', "
            "'registered', 'starting', 'running', 'stopping', 'disabled', 'failed', "
            "'incompatible')",
            name="ck_plugin_installations_status",
        ),
        Index("ix_plugin_installations_status_enabled", "status", "enabled"),
        Index("ix_plugin_installations_updated", "updated_at"),
    )

    plugin_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    plugin_api: Mapped[str] = mapped_column(String(32), nullable=False)
    yuki_requires: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entrypoint: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_permissions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    requested_permissions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PluginConfigValueModel(Base):
    """A validated non-secret plugin configuration value at one exact scope."""

    __tablename__ = "plugin_config_values"
    __table_args__ = (
        Index(
            "uq_plugin_config_values_global_key",
            "plugin_id",
            "key",
            unique=True,
            sqlite_where=text("scope_type = 'global'"),
        ),
        Index(
            "uq_plugin_config_values_person_key",
            "plugin_id",
            "canonical_person_id",
            "key",
            unique=True,
            sqlite_where=text("scope_type = 'user'"),
        ),
        Index(
            "uq_plugin_config_values_space_key",
            "plugin_id",
            "canonical_space_id",
            "key",
            unique=True,
            sqlite_where=text("scope_type = 'group'"),
        ),
        CheckConstraint(
            "scope_type IN ('global', 'group', 'user')",
            name="ck_plugin_config_values_scope_type",
        ),
        CheckConstraint(
            "(scope_type = 'global' AND canonical_person_id IS NULL "
            "AND canonical_space_id IS NULL) OR "
            "(scope_type = 'user' AND canonical_person_id IS NOT NULL "
            "AND canonical_space_id IS NULL) OR "
            "(scope_type = 'group' AND canonical_person_id IS NULL "
            "AND canonical_space_id IS NOT NULL)",
            name="ck_plugin_config_values_scope_owner",
        ),
        CheckConstraint("version >= 1", name="ck_plugin_config_values_version"),
        Index(
            "ix_plugin_config_values_plugin_scope",
            "plugin_id",
            "scope_type",
        ),
        Index("ix_plugin_config_values_canonical_person_id", "canonical_person_id"),
        Index("ix_plugin_config_values_canonical_space_id", "canonical_space_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_id: Mapped[str] = mapped_column(
        ForeignKey("plugin_installations.plugin_id", ondelete="CASCADE"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canonical_person_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("persons.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_space_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("spaces.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )


class PluginStateModel(Base):
    """Namespaced plugin KV data isolated by plugin ID."""

    __tablename__ = "plugin_state"
    __table_args__ = (
        UniqueConstraint("plugin_id", "namespace", "key", name="uq_plugin_state_namespace_key"),
        CheckConstraint("version >= 1", name="ck_plugin_state_version"),
        Index("ix_plugin_state_plugin_namespace", "plugin_id", "namespace"),
        Index("ix_plugin_state_expires", "expires_at"),
        Index("ix_plugin_state_canonical_person_id", "canonical_person_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_id: Mapped[str] = mapped_column(
        ForeignKey("plugin_installations.plugin_id", ondelete="CASCADE"), nullable=False
    )
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canonical_person_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("persons.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )


class PluginAuditEventModel(Base):
    """Redacted append-only audit metadata for a plugin operation."""

    __tablename__ = "plugin_audit_events"
    __table_args__ = (
        Index("ix_plugin_audit_events_plugin_created", "plugin_id", "created_at"),
        Index("ix_plugin_audit_events_actor_created", "actor_user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    permission: Mapped[str | None] = mapped_column(String(128), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PluginAgentSessionModel(Base):
    """An isolated host-managed LLM conversation owned by one plugin."""

    __tablename__ = "plugin_agent_sessions"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('user', 'group', 'plugin')",
            name="ck_plugin_agent_sessions_scope_type",
        ),
        CheckConstraint(
            "(scope_type = 'plugin' AND canonical_owner_person_id IS NULL "
            "AND canonical_space_id IS NULL) OR "
            "(scope_type = 'user' AND canonical_owner_person_id IS NOT NULL "
            "AND canonical_space_id IS NULL) OR "
            "(scope_type = 'group' AND canonical_space_id IS NOT NULL)",
            name="ck_plugin_agent_sessions_scope_owner",
        ),
        CheckConstraint(
            "status IN ('active', 'closed', 'expired', 'blocked')",
            name="ck_plugin_agent_sessions_status",
        ),
        CheckConstraint(
            "persistence IN ('ephemeral', 'durable')",
            name="ck_plugin_agent_sessions_persistence",
        ),
        CheckConstraint(
            "context_profile IN ('none', 'current_user', 'current_group')",
            name="ck_plugin_agent_sessions_context_profile",
        ),
        CheckConstraint(
            "length(instructions) >= 1 AND length(instructions) <= 8000",
            name="ck_plugin_agent_sessions_instructions",
        ),
        CheckConstraint("next_sequence >= 1", name="ck_plugin_agent_sessions_sequence"),
        CheckConstraint("turn_count >= 0", name="ck_plugin_agent_sessions_turn_count"),
        Index(
            "ix_plugin_agent_sessions_plugin_scope",
            "plugin_id",
            "scope_type",
        ),
        Index("ix_plugin_agent_sessions_expires", "expires_at"),
        Index("ix_plugin_agent_sessions_canonical_owner_person_id", "canonical_owner_person_id"),
        Index("ix_plugin_agent_sessions_canonical_space_id", "canonical_space_id"),
    )

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(
        ForeignKey("plugin_installations.plugin_id", ondelete="CASCADE"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    persistence: Mapped[str] = mapped_column(String(16), nullable=False, default="durable")
    context_profile: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    allowed_capabilities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    next_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canonical_owner_person_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("persons.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_space_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("spaces.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )


class PluginAgentMessageModel(Base):
    """One persisted visible message in an isolated plugin Agent session."""

    __tablename__ = "plugin_agent_messages"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "sequence", name="uq_plugin_agent_messages_session_sequence"
        ),
        CheckConstraint(
            "role IN ('user', 'assistant', 'tool')",
            name="ck_plugin_agent_messages_role",
        ),
        CheckConstraint("sequence >= 1", name="ck_plugin_agent_messages_sequence"),
        CheckConstraint(
            "canonical_sender_person_id IS NULL OR role = 'user'",
            name="ck_plugin_agent_messages_sender",
        ),
        Index("ix_plugin_agent_messages_session_created", "session_id", "created_at"),
        Index("ix_plugin_agent_messages_sender", "sender_user_id"),
        Index("ix_plugin_agent_messages_canonical_sender_person_id", "canonical_sender_person_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("plugin_agent_sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    sender_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canonical_sender_person_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("persons.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )


class PluginBackgroundTargetGrantModel(Base):
    """An administrator-approved target for one plugin's background notifications."""

    __tablename__ = "plugin_background_target_grants"
    __table_args__ = (
        Index(
            "uq_plugin_background_target_person",
            "plugin_id",
            "canonical_target_person_id",
            unique=True,
            sqlite_where=text("target_type = 'private'"),
        ),
        Index(
            "uq_plugin_background_target_space",
            "plugin_id",
            "canonical_target_space_id",
            unique=True,
            sqlite_where=text("target_type = 'group'"),
        ),
        CheckConstraint(
            "target_type IN ('group', 'private')",
            name="ck_plugin_background_target_type",
        ),
        CheckConstraint(
            "NOT (canonical_target_person_id IS NOT NULL "
            "AND canonical_target_space_id IS NOT NULL) "
            "AND (canonical_target_person_id IS NULL OR target_type = 'private') "
            "AND (canonical_target_space_id IS NULL OR target_type = 'group') "
            "AND (enabled = 0 OR (canonical_created_by_person_id IS NOT NULL "
            "AND canonical_presence_id IS NOT NULL "
            "AND ((target_type = 'private' "
            "AND canonical_target_person_id IS NOT NULL "
            "AND canonical_target_space_id IS NULL) OR "
            "(target_type = 'group' "
            "AND canonical_target_person_id IS NULL "
            "AND canonical_target_space_id IS NOT NULL))))",
            name="ck_plugin_background_target_owner",
        ),
        Index("ix_plugin_background_target_enabled", "plugin_id", "enabled"),
        Index(
            "ix_plugin_background_target_grants_canonical_target_person_id",
            "canonical_target_person_id",
        ),
        Index(
            "ix_plugin_background_target_grants_canonical_target_space_id",
            "canonical_target_space_id",
        ),
        Index(
            "ix_plugin_background_target_grants_canonical_created_by_person_id",
            "canonical_created_by_person_id",
        ),
        Index("ix_plugin_background_target_grants_canonical_presence_id", "canonical_presence_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_id: Mapped[str] = mapped_column(
        ForeignKey("plugin_installations.plugin_id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bot_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canonical_target_person_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("persons.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_target_space_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("spaces.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_created_by_person_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("persons.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_presence_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("presences.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )


class PluginMediaArtifactModel(Base):
    """Host-owned plugin media; paths never cross the SDK boundary."""

    __tablename__ = "plugin_media_artifacts"
    __table_args__ = (
        CheckConstraint("byte_size > 0", name="ck_plugin_media_artifacts_size"),
        Index("ix_plugin_media_artifacts_plugin_expires", "plugin_id", "expires_at"),
        Index("ix_plugin_media_artifacts_sha", "plugin_id", "sha256"),
    )

    handle_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(
        ForeignKey("plugin_installations.plugin_id", ondelete="CASCADE"), nullable=False
    )
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PluginNotificationOutboxModel(Base):
    """One independently retryable notification part."""

    __tablename__ = "plugin_notification_outbox"
    __table_args__ = (
        UniqueConstraint("notification_id", "part_key", name="uq_plugin_notification_outbox_part"),
        CheckConstraint(
            "target_type IN ('group', 'private')",
            name="ck_plugin_outbox_target_type",
        ),
        CheckConstraint(
            "NOT (canonical_target_person_id IS NOT NULL "
            "AND canonical_target_space_id IS NOT NULL) "
            "AND (canonical_target_person_id IS NULL OR target_type = 'private') "
            "AND (canonical_target_space_id IS NULL OR target_type = 'group') "
            "AND (status NOT IN ('pending', 'processing') OR "
            "(canonical_conversation_id IS NOT NULL "
            "AND canonical_presence_id IS NOT NULL "
            "AND ((target_type = 'private' "
            "AND canonical_target_person_id IS NOT NULL "
            "AND canonical_target_space_id IS NULL) OR "
            "(target_type = 'group' "
            "AND canonical_target_person_id IS NULL "
            "AND canonical_target_space_id IS NOT NULL))))",
            name="ck_plugin_outbox_target_owner",
        ),
        CheckConstraint(
            "part_type IN ('text', 'media', 'agent_reply')",
            name="ck_plugin_notification_outbox_part_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'failed', 'uncertain', 'cancelled')",
            name="ck_plugin_notification_outbox_status",
        ),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_plugin_outbox_attempts"),
        Index("ix_plugin_notification_outbox_due", "status", "next_attempt_at"),
        Index("ix_plugin_notification_outbox_plugin", "plugin_id", "status"),
        Index("ix_plugin_notification_outbox_source", "source_event_id"),
        Index(
            "ix_plugin_notification_outbox_canonical_target_person_id",
            "canonical_target_person_id",
        ),
        Index(
            "ix_plugin_notification_outbox_canonical_target_space_id",
            "canonical_target_space_id",
        ),
        Index(
            "ix_plugin_notification_outbox_canonical_conversation_id",
            "canonical_conversation_id",
        ),
        Index("ix_plugin_notification_outbox_canonical_presence_id", "canonical_presence_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notification_id: Mapped[str] = mapped_column(String(64), nullable=False)
    part_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_event_id: Mapped[int] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), nullable=False
    )
    plugin_id: Mapped[str] = mapped_column(
        ForeignKey("plugin_installations.plugin_id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bot_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    part_type: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    media_handle_id: Mapped[str | None] = mapped_column(
        ForeignKey("plugin_media_artifacts.handle_id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    platform_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canonical_target_person_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("persons.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_target_space_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("spaces.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_conversation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("canonical_conversations.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_presence_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("presences.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )


class PluginBackgroundTurnJobModel(Base):
    """A persistent request to let Yuki react inside the target main conversation."""

    __tablename__ = "plugin_background_turn_jobs"
    __table_args__ = (
        UniqueConstraint("source_event_id", name="uq_plugin_background_turn_source"),
        CheckConstraint(
            "target_type IN ('group', 'private')",
            name="ck_plugin_turn_target_type",
        ),
        CheckConstraint(
            "NOT (canonical_target_person_id IS NOT NULL "
            "AND canonical_target_space_id IS NOT NULL) "
            "AND (canonical_target_person_id IS NULL OR target_type = 'private') "
            "AND (canonical_target_space_id IS NULL OR target_type = 'group') "
            "AND (status NOT IN ('pending', 'processing') OR "
            "(canonical_conversation_id IS NOT NULL "
            "AND canonical_presence_id IS NOT NULL "
            "AND ((target_type = 'private' "
            "AND canonical_target_person_id IS NOT NULL "
            "AND canonical_target_space_id IS NULL) OR "
            "(target_type = 'group' "
            "AND canonical_target_person_id IS NULL "
            "AND canonical_target_space_id IS NOT NULL))))",
            name="ck_plugin_turn_target_owner",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')",
            name="ck_plugin_background_turn_status",
        ),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_plugin_turn_attempts"),
        Index("ix_plugin_background_turn_due", "status", "next_attempt_at"),
        Index("ix_plugin_background_turn_plugin", "plugin_id", "status"),
        Index(
            "ix_plugin_background_turn_jobs_canonical_target_person_id",
            "canonical_target_person_id",
        ),
        Index(
            "ix_plugin_background_turn_jobs_canonical_target_space_id",
            "canonical_target_space_id",
        ),
        Index(
            "ix_plugin_background_turn_jobs_canonical_conversation_id",
            "canonical_conversation_id",
        ),
        Index("ix_plugin_background_turn_jobs_canonical_presence_id", "canonical_presence_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_event_id: Mapped[int] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), nullable=False
    )
    plugin_id: Mapped[str] = mapped_column(
        ForeignKey("plugin_installations.plugin_id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bot_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_intent: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generated_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_calls_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canonical_target_person_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("persons.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_target_space_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("spaces.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_conversation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("canonical_conversations.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_presence_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("presences.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=True,
    )
