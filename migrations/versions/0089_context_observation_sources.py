"""Keep ordered observation sources and protect their original artifact owners."""

import sqlalchemy as sa
from alembic import op

from qq_ai_bot.conversation.observation_schema import (
    CONTEXT_SOURCE_TRIGGERS,
    RETIRED_ROLLUP_TRIGGERS,
)
from qq_ai_bot.conversation.projection_revision_schema import PROJECTION_TRIGGERS_CURRENT

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
