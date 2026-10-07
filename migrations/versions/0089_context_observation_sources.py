"""Keep ordered observation sources and protect their original artifact owners."""

import sqlalchemy as sa
from alembic import op

from qq_ai_bot.conversation.observation_schema import (
    CONTEXT_SOURCE_TRIGGERS,
    RETIRED_ROLLUP_TRIGGERS,
)

PROJECTION_TRIGGERS_CURRENT = {
    "prompt_projection_reset": "CREATE TRIGGER prompt_projection_reset AFTER UPDATE OF generation, starts_after_event_id ON canonical_conversations\n    WHEN OLD.generation IS NOT NEW.generation OR OLD.starts_after_event_id IS NOT NEW.starts_after_event_id\n    BEGIN\n        UPDATE canonical_conversations\n        SET prompt_source_revision=prompt_source_revision+1 WHERE id IN (NEW.id);\n        UPDATE prompt_projections\n        SET payload_json='[]', byte_size=2, revision=revision+1,\n            invalidated_reason='reset'\n        WHERE conversation_id IN (NEW.id);\n    END",
    "prompt_projection_event_delete": "CREATE TRIGGER prompt_projection_event_delete AFTER DELETE ON chat_events\n    \n    BEGIN\n        UPDATE canonical_conversations\n        SET prompt_source_revision=prompt_source_revision+1 WHERE id IN (OLD.canonical_conversation_id);\n        UPDATE prompt_projections\n        SET payload_json='[]', byte_size=2, revision=revision+1,\n            invalidated_reason='deleted_event'\n        WHERE conversation_id IN (OLD.canonical_conversation_id);\n    END",
    "prompt_projection_event_update": "CREATE TRIGGER prompt_projection_event_update AFTER UPDATE OF content, segments_json, visual_summary, external_payload_json, suppression_status, canonical_conversation_id ON chat_events\n    WHEN OLD.content IS NOT NEW.content OR OLD.segments_json IS NOT NEW.segments_json OR OLD.visual_summary IS NOT NEW.visual_summary OR OLD.external_payload_json IS NOT NEW.external_payload_json OR OLD.suppression_status IS NOT NEW.suppression_status OR OLD.canonical_conversation_id IS NOT NEW.canonical_conversation_id\n    BEGIN\n        UPDATE canonical_conversations\n        SET prompt_source_revision=prompt_source_revision+1 WHERE id IN (OLD.canonical_conversation_id, NEW.canonical_conversation_id);\n        UPDATE prompt_projections\n        SET payload_json='[]', byte_size=2, revision=revision+1,\n            invalidated_reason='source_changed'\n        WHERE conversation_id IN (OLD.canonical_conversation_id, NEW.canonical_conversation_id);\n    END",
    "prompt_projection_canonical_conversation_rollups_insert": "CREATE TRIGGER prompt_projection_canonical_conversation_rollups_insert AFTER INSERT ON canonical_conversation_rollups\n    \n    BEGIN\n        UPDATE canonical_conversations\n        SET prompt_source_revision=prompt_source_revision+1 WHERE id IN (NEW.conversation_id);\n        UPDATE prompt_projections\n        SET payload_json='[]', byte_size=2, revision=revision+1,\n            invalidated_reason='rollup'\n        WHERE conversation_id IN (NEW.conversation_id);\n    END",
    "prompt_projection_canonical_conversation_rollups_update": "CREATE TRIGGER prompt_projection_canonical_conversation_rollups_update AFTER UPDATE ON canonical_conversation_rollups\n    \n    BEGIN\n        UPDATE canonical_conversations\n        SET prompt_source_revision=prompt_source_revision+1 WHERE id IN (NEW.conversation_id);\n        UPDATE prompt_projections\n        SET payload_json='[]', byte_size=2, revision=revision+1,\n            invalidated_reason='rollup'\n        WHERE conversation_id IN (NEW.conversation_id);\n    END",
    "prompt_projection_canonical_conversation_rollups_delete": "CREATE TRIGGER prompt_projection_canonical_conversation_rollups_delete AFTER DELETE ON canonical_conversation_rollups\n    \n    BEGIN\n        UPDATE canonical_conversations\n        SET prompt_source_revision=prompt_source_revision+1 WHERE id IN (OLD.conversation_id);\n        UPDATE prompt_projections\n        SET payload_json='[]', byte_size=2, revision=revision+1,\n            invalidated_reason='rollup'\n        WHERE conversation_id IN (OLD.conversation_id);\n    END",
    "prompt_projection_canonical_conversation_rollup_emergency_overlays_insert": "CREATE TRIGGER prompt_projection_canonical_conversation_rollup_emergency_overlays_insert AFTER INSERT ON canonical_conversation_rollup_emergency_overlays\n    \n    BEGIN\n        UPDATE canonical_conversations\n        SET prompt_source_revision=prompt_source_revision+1 WHERE id IN (NEW.conversation_id);\n        UPDATE prompt_projections\n        SET payload_json='[]', byte_size=2, revision=revision+1,\n            invalidated_reason='rollup'\n        WHERE conversation_id IN (NEW.conversation_id);\n    END",
    "prompt_projection_canonical_conversation_rollup_emergency_overlays_update": "CREATE TRIGGER prompt_projection_canonical_conversation_rollup_emergency_overlays_update AFTER UPDATE ON canonical_conversation_rollup_emergency_overlays\n    \n    BEGIN\n        UPDATE canonical_conversations\n        SET prompt_source_revision=prompt_source_revision+1 WHERE id IN (NEW.conversation_id);\n        UPDATE prompt_projections\n        SET payload_json='[]', byte_size=2, revision=revision+1,\n            invalidated_reason='rollup'\n        WHERE conversation_id IN (NEW.conversation_id);\n    END",
    "prompt_projection_canonical_conversation_rollup_emergency_overlays_delete": "CREATE TRIGGER prompt_projection_canonical_conversation_rollup_emergency_overlays_delete AFTER DELETE ON canonical_conversation_rollup_emergency_overlays\n    \n    BEGIN\n        UPDATE canonical_conversations\n        SET prompt_source_revision=prompt_source_revision+1 WHERE id IN (OLD.conversation_id);\n        UPDATE prompt_projections\n        SET payload_json='[]', byte_size=2, revision=revision+1,\n            invalidated_reason='rollup'\n        WHERE conversation_id IN (OLD.conversation_id);\n    END",
    "prompt_projection_audio_update": "CREATE TRIGGER prompt_projection_audio_update\n    AFTER UPDATE OF audio_transcript ON chat_events\n    WHEN OLD.audio_transcript IS NOT NEW.audio_transcript\n    BEGIN\n        UPDATE canonical_conversations\n        SET prompt_source_revision=prompt_source_revision+1\n        WHERE id = NEW.canonical_conversation_id;\n        UPDATE prompt_projections\n        SET payload_json='[]', byte_size=2, revision=revision+1, invalidated_reason='source_changed'\n        WHERE conversation_id = NEW.canonical_conversation_id;\n    END",
    "prompt_projection_owner_update": "CREATE TRIGGER prompt_projection_owner_update\n    AFTER UPDATE OF kind, person_id, space_id ON canonical_conversations\n    WHEN OLD.kind IS NOT NEW.kind OR OLD.person_id IS NOT NEW.person_id\n        OR OLD.space_id IS NOT NEW.space_id\n    BEGIN\n        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1\n        WHERE id = NEW.id;\n        UPDATE prompt_projections SET payload_json='[]', byte_size=2, revision=revision+1,\n            invalidated_reason='read_scope_changed'\n        WHERE conversation_id = NEW.id;\n    END",
    "prompt_projection_event_metadata_update": "CREATE TRIGGER\n    prompt_projection_event_metadata_update AFTER UPDATE OF id, bot_user_id, platform_message_id, scope_type, group_id, private_peer_user_id, sender_user_id, sender_nickname, sender_group_card, direction, event_kind, source_plugin_id, external_source, external_event_key, external_event_type, external_resume_wait, external_target_id, reply_to_message_id, reply_to_event_id, origin, automation_id, automation_run_id, occurred_at, observed_at, canonical_event_id, author_kind, author_person_id, author_presence_id, ingress_presence_id, utterance_fingerprint, ingress_provider, ingress_gateway_instance_id, caused_by_event_id ON chat_events\n    WHEN OLD.id IS NOT NEW.id OR OLD.bot_user_id IS NOT NEW.bot_user_id OR OLD.platform_message_id IS NOT NEW.platform_message_id OR OLD.scope_type IS NOT NEW.scope_type OR OLD.group_id IS NOT NEW.group_id OR OLD.private_peer_user_id IS NOT NEW.private_peer_user_id OR OLD.sender_user_id IS NOT NEW.sender_user_id OR OLD.sender_nickname IS NOT NEW.sender_nickname OR OLD.sender_group_card IS NOT NEW.sender_group_card OR OLD.direction IS NOT NEW.direction OR OLD.event_kind IS NOT NEW.event_kind OR OLD.source_plugin_id IS NOT NEW.source_plugin_id OR OLD.external_source IS NOT NEW.external_source OR OLD.external_event_key IS NOT NEW.external_event_key OR OLD.external_event_type IS NOT NEW.external_event_type OR OLD.external_resume_wait IS NOT NEW.external_resume_wait OR OLD.external_target_id IS NOT NEW.external_target_id OR OLD.reply_to_message_id IS NOT NEW.reply_to_message_id OR OLD.reply_to_event_id IS NOT NEW.reply_to_event_id OR OLD.origin IS NOT NEW.origin OR OLD.automation_id IS NOT NEW.automation_id OR OLD.automation_run_id IS NOT NEW.automation_run_id OR OLD.occurred_at IS NOT NEW.occurred_at OR OLD.observed_at IS NOT NEW.observed_at OR OLD.canonical_event_id IS NOT NEW.canonical_event_id OR OLD.author_kind IS NOT NEW.author_kind OR OLD.author_person_id IS NOT NEW.author_person_id OR OLD.author_presence_id IS NOT NEW.author_presence_id OR OLD.ingress_presence_id IS NOT NEW.ingress_presence_id OR OLD.utterance_fingerprint IS NOT NEW.utterance_fingerprint OR OLD.ingress_provider IS NOT NEW.ingress_provider OR OLD.ingress_gateway_instance_id IS NOT NEW.ingress_gateway_instance_id OR OLD.caused_by_event_id IS NOT NEW.caused_by_event_id\n    BEGIN\n        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1\n        WHERE id IN (OLD.canonical_conversation_id, NEW.canonical_conversation_id);\n        UPDATE prompt_projections SET payload_json='[]', byte_size=2, revision=revision+1,\n            invalidated_reason='source_changed'\n        WHERE conversation_id IN (OLD.canonical_conversation_id, NEW.canonical_conversation_id);\n    END",
    "prompt_projection_canonical_conversation_rollups_move": "CREATE TRIGGER prompt_projection_canonical_conversation_rollups_move\n    AFTER UPDATE OF conversation_id ON canonical_conversation_rollups\n    WHEN OLD.conversation_id IS NOT NEW.conversation_id\n    BEGIN\n        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1\n        WHERE id = OLD.conversation_id;\n        UPDATE prompt_projections SET payload_json='[]', byte_size=2, revision=revision+1,\n            invalidated_reason='rollup'\n        WHERE conversation_id = OLD.conversation_id;\n    END",
    "prompt_projection_canonical_conversation_rollup_emergency_overlays_move": "CREATE TRIGGER prompt_projection_canonical_conversation_rollup_emergency_overlays_move\n    AFTER UPDATE OF conversation_id ON canonical_conversation_rollup_emergency_overlays\n    WHEN OLD.conversation_id IS NOT NEW.conversation_id\n    BEGIN\n        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1\n        WHERE id = OLD.conversation_id;\n        UPDATE prompt_projections SET payload_json='[]', byte_size=2, revision=revision+1,\n            invalidated_reason='rollup'\n        WHERE conversation_id = OLD.conversation_id;\n    END",
}

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    for name in RETIRED_ROLLUP_TRIGGERS:
        statement = connection.execute(
            sa.text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
            {"name": name},
        ).scalar()
        if statement is None or _normalize(statement) != _normalize(
            PROJECTION_TRIGGERS_CURRENT[name]
        ):
            raise RuntimeError("unexpected legacy rollup projection trigger")
        op.execute(f"DROP TRIGGER {name}")
    op.add_column(
        "prompt_projections", sa.Column("selected_summary_text", sa.Text(), nullable=True)
    )
    op.add_column(
        "prompt_projections",
        sa.Column("selected_summary_coverage", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("tool_artifacts", sa.Column("access_json", sa.Text(), nullable=True))
    op.create_table(
        "tool_artifact_refs",
        sa.Column("owner_kind", sa.String(24), primary_key=True),
        sa.Column("owner_id", sa.String(128), primary_key=True),
        sa.Column(
            "handle_id",
            sa.String(64),
            sa.ForeignKey("tool_artifacts.handle_id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index("ix_tool_artifact_refs_handle", "tool_artifact_refs", ["handle_id"])
    op.create_table(
        "model_context_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("canonical_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("read_scope", sa.Text(), nullable=False),
        sa.Column("source_key", sa.String(256), nullable=False),
        sa.Column(
            "source_work_id", sa.String(36), sa.ForeignKey("runtime_work.id", ondelete="SET NULL")
        ),
        sa.Column(
            "source_event_id", sa.Integer(), sa.ForeignKey("chat_events.id", ondelete="CASCADE")
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("privacy_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("parent_sources_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("summary_view_key", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_key", name="uq_context_observation_source"),
    )
    op.create_index(
        "ix_context_observation_scope",
        "model_context_observations",
        ["conversation_id", "generation", "actor_id", "read_scope", "created_at", "id"],
    )
    op.create_table(
        "model_context_selections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("view_key", sa.String(64), nullable=False),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("canonical_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("read_scope", sa.Text(), nullable=False),
        sa.Column("source_key", sa.String(256), nullable=False),
        sa.Column("event_ids_json", sa.Text(), nullable=False),
        sa.Column("observation_sources_json", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("view_key", "source_key", name="uq_context_selection_source"),
    )
    op.create_index("ix_context_selection_view", "model_context_selections", ["view_key", "id"])
    op.create_index(
        "ix_context_selection_scope", "model_context_selections", ["conversation_id", "generation"]
    )
    for statement in CONTEXT_SOURCE_TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    if (
        connection.execute(sa.text("SELECT 1 FROM model_context_observations LIMIT 1")).first()
        or connection.execute(sa.text("SELECT 1 FROM model_context_selections LIMIT 1")).first()
        or connection.execute(sa.text("SELECT 1 FROM tool_artifact_refs LIMIT 1")).first()
    ):
        raise RuntimeError("cannot discard owned observation or artifact sources")
    for name in CONTEXT_SOURCE_TRIGGERS:
        op.execute(f"DROP TRIGGER {name}")
    op.drop_table("model_context_selections")
    op.drop_table("model_context_observations")
    op.drop_table("tool_artifact_refs")
    op.drop_column("tool_artifacts", "access_json")
    op.drop_column("prompt_projections", "selected_summary_coverage")
    op.drop_column("prompt_projections", "selected_summary_text")
    for name in RETIRED_ROLLUP_TRIGGERS:
        op.execute(PROJECTION_TRIGGERS_CURRENT[name])


def _normalize(statement: str) -> str:
    return "".join(statement.lower().replace('"', "").split()).rstrip(";")
